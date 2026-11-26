"""What the wiring tests share: the real NQ bars of H4 and the probe that decides whether an H3 indicator can be wired into the factory library
(SPEC-032: OPT-17). The probe calls the same `_call` the library uses and calculates nothing."""
from __future__ import annotations

import itertools
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

import hokma.quant.indicators  # noqa: F401  (importing the package registers the indicators)
from hokma.factory import indicator_library as lib
from hokma.quant import registry

FIXTURE = Path(__file__).resolve().parents[1] / "backtest" / "fixtures" / "nq_session.json"
FIELDS = ("open", "high", "low", "close", "volume")
ORIGINALS = frozenset({("ema", "ema"), ("sma", "sma"), ("rsi", "rsi"), ("adx", "adx"), ("atr", "atr"), ("macd", "macd"), ("macd", "signal")})
RENAMES = {"window": "length", "short_window": "fast", "long_window": "slow", "signal_window": "signal"}     # the names the first nine already use


def nq_day() -> dict[str, np.ndarray]:
    """The 390 one minute bars of the regular session of NQ 12-25 on 2025-11-10 (real bars of Trading Core), as arrays by field."""
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))["day"]["bars"]
    return {k: np.array([float(r[k]) for r in rows]) for k in FIELDS}


def frame(bars: Mapping[str, np.ndarray], fields: tuple[str, ...] = FIELDS) -> pd.DataFrame:
    return pd.DataFrame({k: bars[k] for k in FIELDS if k in fields})


def wired_name(h3: str, column: str) -> str:
    base = h3.removeprefix("indicators.")
    return base if column == base else f"{base}_{column}"


@dataclass
class Probe:
    h3: str
    columns: dict[str, str] = field(default_factory=dict)      # column -> "" when it passes, else why not
    reason: str = ""                                           # the reason the whole function is out
    fields: tuple[str, ...] = ()
    int_params: tuple[str, ...] = ()


def _same(a: np.ndarray, b: np.ndarray) -> bool:
    return a.shape == b.shape and bool(np.allclose(a, b, equal_nan=True))


def probe(h3: str, bars: Mapping[str, np.ndarray]) -> Probe:
    """Run one `indicators.*` function of H3 on the bars and say, column by column, whether the value of a bar changes when later bars are removed
    (the rule of `strategy._check_causal`). Nothing is calculated here: every value is the one H3 gives through the library's `_call`."""
    out = Probe(h3)
    spec = registry.describe(h3)
    short = h3.removeprefix("indicators.")
    if "bars" not in spec.inputs:
        out.reason = "reads a series or a table of symbols (" + ", ".join(k for k in spec.inputs if k not in ("line", "last_n")) + "), not the bars of one instrument"
        return out
    if "last_n" not in spec.inputs:
        out.reason = "gives one answer for the last bar, not a value for each bar (no last_n)"
        return out
    full_frame = frame(bars)
    try:
        result = spec.fn(full_frame, last_n=None)                               # type: ignore[misc]
    except Exception as exc:                                                    # noqa: BLE001  (the reason is the point)
        out.reason = f"{type(exc).__name__}: {str(exc)[:110]}"
        return out
    if not result.ok:
        out.reason = "not ok on the bars: " + "; ".join(result.warnings)[:110]
        return out
    values = result.value.get("values") if isinstance(result.value, dict) else None
    if not isinstance(values, dict) or not values:
        out.reason = "no columns in result.value['values']"
        return out
    out.int_params = tuple(k for k, v in spec.inputs.items() if isinstance(v, dict) and v.get("type") == "int" and k != "last_n")
    n = len(full_frame)
    for column in values:
        try:
            full = lib._call(short, full_frame, column)
        except Exception as exc:                                                # noqa: BLE001
            out.columns[column] = f"{type(exc).__name__}: {str(exc)[:110]}"
            continue
        if not np.isfinite(full).any():
            out.columns[column] = f"no value on {n} bars"
            continue
        bad = ""
        for cut in sorted({n // 4, n // 2, n - 1}):
            part = lib._call(short, full_frame.iloc[:cut], column)
            if not _same(part, full[:cut]):
                bar = int(np.flatnonzero(~np.isclose(part, full[:cut], equal_nan=True))[0])
                bad = f"look ahead: the value of bar {bar} changes when the bars after bar {cut - 1} are removed"
                break
        out.columns[column] = bad
    passing = [c for c, why in out.columns.items() if not why]
    if passing:
        out.fields = _fields(short, passing[0], bars, full_frame)
    return out


def _fields(short: str, column: str, bars: Mapping[str, np.ndarray], full_frame: pd.DataFrame) -> tuple[str, ...]:
    """The smallest set of bar fields that gives the same column as all of them."""
    want = lib._call(short, full_frame, column)
    for size in range(1, len(FIELDS) + 1):
        for subset in itertools.combinations(FIELDS, size):
            try:
                got = lib._call(short, frame(bars, subset), column)
            except Exception:                                                   # noqa: BLE001
                continue
            if _same(got, want):
                return subset
    return FIELDS


def all_h3() -> list[str]:
    return [s.name for s in registry.list(module="indicators")]
