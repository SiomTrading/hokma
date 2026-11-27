"""A strategy in pandas as a strategy of the factory (1.3.0, task 3433).

The backtest and the optimizer reveal the bars one by one. A strategy in pandas gives its signals for the whole table at once, and that is sound because the dry run proved that the
signal of a bar does not change when the later bars are cut away. So `prepare` asks the provider (loader.py, in this process) for the signals once, and `on_bar` reads the row
of the bar that just closed: the order goes at the open of the next bar, with the stop that the strategy fixed on that row as an absolute price. The size comes from the sizer of the factory
that the parameter `sizer` names, like any other generated strategy; the strategy gives the signal and the stop, never the quantity.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import numpy as np

from hokma.factory.indicator_library import LIBRARY
from hokma.factory.orders import FixedPrice, Price, buy, protective_price, reduce_intent, sell
from hokma.factory.params import ParamSchema
from hokma.factory.pandas_strategy import contract
from hokma.factory.pandas_strategy.contract import ParamDef, Signals
from hokma.factory.strategy import BadStrategy, BarContext, InitContext, NoEntry, apply_requests
from hokma.factory.strategy import IndicatorRequest as RealRequest

# provider(source, params, open_times_ns, columns, bar_seconds) -> the columns of the answer (see contract.SIGNAL_COLUMNS)
Provider = Callable[[str, Mapping[str, Any], np.ndarray, Mapping[str, np.ndarray], int], Mapping[str, np.ndarray]]


class PandasStrategy:
    """The strategy of the factory for one version of a strategy in pandas. A class is made for each version with `build_class`."""

    registry_id: str = ""
    DEFS: tuple[ParamDef, ...] = ()
    SOURCE: str = ""
    PROVIDER: Provider | None = None
    SCHEMA: ParamSchema = ParamSchema(())
    _user_names: frozenset[str] = frozenset()

    def __init__(self, **values: Any) -> None:
        self.values = dict(values)
        self.protective_orders = True                     # every entry has its stop
        self._user_values = {k: v for k, v in values.items() if k in self._user_names}
        self.signals: Signals | None = None
        self._sheet: Any = None
        self._sizer: Any = None
        from hokma.factory.user_rules import _Levels

        self._levels = _Levels()

    # ---- the contract of the factory
    def init(self, ctx: InitContext) -> None:
        if self.values.get("sizer") == "vol_target":
            apply_requests(ctx, [RealRequest("sizer_vol", "ewma_vol", "close", (("decay_pct", int(self.values["sizer_decay_pct"])),), 21)], LIBRARY)
        ctx.set_warmup(1)
        self._sheet = self._sizer = None

    def prepare(self, bars: Sequence[Any], span_ns: int) -> None:
        """Ask for the signals of the whole table, once, and check them against the format."""
        provider = type(self).PROVIDER
        if provider is None:
            raise BadStrategy(f"{self.registry_id}: no provider of signals: the loader is not set")
        open_ns = np.array([int(b.ts.timestamp()) * 10**9 + b.ts.microsecond * 1_000 for b in bars], dtype="int64")
        columns = {name: np.array([float(getattr(b, name)) for b in bars], dtype="float64") for name in ("open", "high", "low", "close", "volume")}
        answer = provider(type(self).SOURCE, contract.check_params(type(self).DEFS, self._user_values), open_ns, columns, max(1, int(span_ns // 1_000_000_000)))
        signals, problems = contract.build_signals(dict(answer), columns["close"])
        if problems:
            raise BadStrategy(f"{self.registry_id}: " + " | ".join(p.text() for p in problems[:3]))
        self.signals = signals

    # ---- the decision of a bar
    def _sizer_for(self, ctx: BarContext) -> Any:
        if self._sizer is None:
            from hokma.backtest.runner import sheet_of
            from hokma.factory.parts import SIZERS, Env, build_part

            self._sheet = sheet_of(ctx.contract_id, ctx.as_of.date())
            kind = self.values.get("sizer", "fixed_contracts")
            params = {"fixed_contracts": {"n": self.values["sizer_n"]}, "fixed_risk": {"risk_pct": self.values["sizer_risk_pct"]},
                      "vol_target": {"target_vol": self.values["sizer_target_vol"], "decay_pct": self.values["sizer_decay_pct"], "periods_per_year": self.values["sizer_periods"]}}[kind]
            self._sizer = build_part(SIZERS, "sizer", kind, params, Env(self._sheet, "DT", self._levels))
        return self._sizer

    def _level(self, value: float, kind: str, side: str, ctx: BarContext, entry: Decimal) -> FixedPrice:
        """A level as a price on the grid of the contract, kept as the strategy put it. A stop rounds away from the entry, a target toward it."""
        level = protective_price(Price(Decimal(repr(float(value)))), side=side, kind=kind, entry=entry, sheet=self._sheet)       # type: ignore[arg-type]
        return FixedPrice(level)

    def on_bar(self, ctx: BarContext) -> tuple[Any, ...]:
        signals = self.signals
        if signals is None:
            raise BadStrategy(f"{self.registry_id}: the signals were not prepared")
        i = ctx.index - 1
        if i >= len(signals):
            return ()
        held = 0 if ctx.position is None or ctx.position.qty == 0 else (1 if ctx.position.qty > 0 else -1)
        out: list[Any] = []
        go_long, go_short = bool(signals.entry_long[i]), bool(signals.entry_short[i])
        if held == 1 and (signals.exit_long[i] or go_short) or held == -1 and (signals.exit_short[i] or go_long):
            if ctx.position is not None and ctx.position.side is not None:
                out.append(reduce_intent(ctx.contract_id, ctx.position.side, abs(ctx.position.qty), tag="exit"))
            held = 0
        if held != 0 or not (go_long or go_short):
            return tuple(out)
        side = "buy" if go_long else "sell"
        try:
            sizer = self._sizer_for(ctx)
            limit = None if signals.limit_price is None or not np.isfinite(signals.limit_price[i]) else Decimal(repr(float(signals.limit_price[i])))
            entry = Decimal(repr(float(ctx.series("close").last))) if limit is None else limit
            stop = self._level(float(signals.stop[i]), "sl", side, ctx, entry)
            target = None if signals.target is None or not np.isfinite(signals.target[i]) else self._level(float(signals.target[i]), "tp", side, ctx, entry)
            self._levels.stop = stop
            size = sizer.size(signal=SimpleNamespace(value=1.0 if go_long else -1.0), ctx=ctx)
            if size is None:
                return tuple(out)
        except (NoEntry, ValueError):                  # no size for this bar, or a level the grid of the contract puts on the wrong side: no entry, the bar goes on
            return tuple(out)
        make = buy if side == "buy" else sell
        out.append(make(ctx.contract_id, size, sl=stop, tp=target, limit=limit, tag=self.registry_id[:64]))
        return tuple(out)


def adapter_schema(defs: tuple[ParamDef, ...]) -> ParamSchema:
    """The parameters of the strategy plus the ones of the sizer: what a backtest and the optimizer see."""
    from hokma.factory.user_rules import adapter_schema as with_sizer

    return with_sizer(contract.schema_of(defs))


def build_class(registry_id: str, defs: tuple[ParamDef, ...], source: str, provider: Provider | None) -> type:
    """The class that the registry holds for one version of a strategy in pandas."""
    return type(f"Pandas_{registry_id}", (PandasStrategy,), {
        "registry_id": registry_id, "DEFS": defs, "SOURCE": source, "PROVIDER": staticmethod(provider) if provider is not None else None, "SCHEMA": adapter_schema(defs),
        "_user_names": frozenset(d.name for d in defs), "__doc__": f"Strategy in pandas ({registry_id}); its signals become orders through the sizer of the factory."})
