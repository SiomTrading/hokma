"""The parts of a composite over the real H3 and desk modules (SPEC-020: L-51, L-52, L-54): hand computed sizes, stops and slices."""
from decimal import Decimal as D

import numpy as np
import pytest

from hokma.exec import algos
from hokma.factory import indicator_library as lib
from hokma.factory import parts
from hokma.factory.alphas import EmaCross
from hokma.factory.alphas.channel_breakout import ChannelBreakout
from hokma.factory.orders import Price, Size, SizeType
from hokma.factory.strategy import BadStrategy, NoEntry, replay_intents
from tests.factory.support import minutes, nq

CID = "FUT:NQ:2025-03"


def closes() -> np.ndarray:
    down = 120.0 - np.arange(30)
    return np.concatenate([down, down[-1] + 2.0 * np.arange(1, 31)])


def run(strategy):
    data = closes()
    bars = {"open": data, "high": data + 1, "low": data - 1, "close": data, "volume": np.ones(len(data))}
    return replay_intents(strategy, bars, times=minutes(len(data)), contract_id=CID)


def build(parts_spec):
    return parts.build_composite(EmaCross(fast=3, slow=8), sheet=nq(), desk_id=1, line="DT", parts=parts_spec, horizon="intraday", tag="cross")


ATR_EXIT = {"kind": "atr", "length": 5, "stop_atr": 2.0, "r_multiple": 2.0}


def test_fixed_risk_and_atr_exit_by_hand():
    """Falling closes, high and low one point away: every true range is 2, so the ATR(5) is 2. The first short view is on the bar that closes at
    113: stop 2 ATR = 4 points above (117), target 2 R = 8 points below (105). Risk 0.5 percent of 100000 = 500; one contract risks 4 * 20 = 80;
    500 / 80 = 6.25: six contracts."""
    result = run(build({"sizer": {"kind": "fixed_risk", "risk_pct": 0.5}, "exit": ATR_EXIT}))
    bar, batch = result.intents[0]
    assert bar == 8 and len(batch) == 1
    entry = batch[0]
    assert (entry.side, entry.size, entry.sl, entry.tp, entry.tag) == ("sell", Size(SizeType.CONTRACTS, 6), Price(D("117")), Price(D("105")), "cross")


def test_changing_only_the_risk_changes_the_quantity_and_not_the_signals():
    small = run(build({"sizer": {"kind": "fixed_risk", "risk_pct": 0.5}, "exit": ATR_EXIT}))
    large = run(build({"sizer": {"kind": "fixed_risk", "risk_pct": 1.0}, "exit": ATR_EXIT}))
    key = lambda r: [(bar, i.side, i.sl, i.tp, i.tag) for bar, batch in r.intents for i in batch]
    assert key(small) == key(large) and key(small)
    assert small.intents[0][1][0].size.value == D(6) and large.intents[0][1][0].size.value == D(12)                      # 1000 / 80 = 12.5: twelve


def test_a_budget_that_does_not_buy_one_contract_gives_no_entry_and_never_moves_the_stop():
    tiny = run(build({"sizer": {"kind": "fixed_risk", "risk_pct": 0.05}, "exit": ATR_EXIT}))       # 50 / 80 < 1 on the first bar, 50 / 120 later
    assert tiny.intents == ()


def test_a_size_by_risk_without_a_stop_gives_no_entry():
    assert run(build({"sizer": {"kind": "fixed_risk", "risk_pct": 1.0}, "exit": {"kind": "none"}})).intents == ()


def test_a_twap_cuts_the_entry_with_the_exact_allocation_and_one_fingerprint():
    """Seven contracts in three equal slices: 7 / 3 = 2.33, the units that are left go to the largest remainder, earlier slice first: 3, 2, 2."""
    result = run(build({"sizer": {"kind": "fixed_contracts", "n": 7}, "execution": {"kind": "twap", "n_slices": 3}}))
    first = [(bar, b[0]) for bar, b in result.intents[:3]]
    assert [bar for bar, _ in first] == [8, 9, 10]
    assert [i.size.value for _, i in first] == [D(3), D(2), D(2)] and sum(i.size.value for _, i in first) == 7
    assert [i.size.value for _, i in first] == list(algos.allocate(D(7), (D(1),) * 3))                      # the same function the desks use
    tags = [i.tag for _, i in first]
    assert [t.rsplit("_", 1)[0] for t in tags] == ["cross:twap_1_of_3", "cross:twap_2_of_3", "cross:twap_3_of_3"] and len({t.rsplit("_", 1)[1] for t in tags}) == 1


def test_a_vwap_follows_the_profile_and_drops_an_empty_slice():
    """Ten contracts on the profile 1, 0, 3, 6: the slice with no volume has nothing and is left out: 1, 3, 6."""
    spec = {"sizer": {"kind": "fixed_contracts", "n": 10}}
    composite = build(spec)
    composite.executor = parts.vwap((1, 0, 3, 6))
    result = run(composite)
    assert [b[0].size.value for _, b in result.intents[:3]] == [D(1), D(3), D(6)]
    assert [b[0].tag.rsplit("_", 1)[0] for _, b in result.intents[:3]] == ["cross:vwap_1_of_3", "cross:vwap_2_of_3", "cross:vwap_3_of_3"]


def test_a_slice_needs_a_size_in_contracts():
    class Risk:
        def size(self, *, signal, ctx):
            return Size(SizeType.RISK_USD, 500)

    composite = build({"sizer": {"kind": "fixed_contracts", "n": 1}, "execution": {"kind": "twap", "n_slices": 2}})
    composite.sizer = Risk()
    with pytest.raises(BadStrategy, match="size in contracts"):
        run(composite)


def test_the_registries_name_their_parts_and_refuse_a_bad_one():
    assert {"fixed_contracts", "fixed_risk"} <= set(parts.SIZERS) and {"none", "atr", "ticks"} <= set(parts.EXITS) and {"market", "twap"} <= set(parts.EXECUTORS)
    with pytest.raises(BadStrategy, match="unknown sizer 'kelly'"):
        build({"sizer": {"kind": "kelly"}})
    with pytest.raises(BadStrategy, match="risk_pct"):
        build({"sizer": {"kind": "fixed_risk"}, "exit": ATR_EXIT})                         # a risk has no default
    with pytest.raises(BadStrategy, match="parts has the keys"):
        build({"stops": {}})
    with pytest.raises(ValueError, match="already registered"):
        parts.register(parts.SIZERS, parts.SIZERS["fixed_contracts"])


def test_a_new_sizer_is_one_registration():
    class Two:
        def size(self, *, signal, ctx):
            return Size(SizeType.CONTRACTS, 2)

    parts.register(parts.SIZERS, parts.PartKind("two", parts.ParamSchema(()), lambda v, env: Two(), "two contracts"))
    try:
        assert run(build({"sizer": {"kind": "two"}})).intents[0][1][0].size.value == D(2)
    finally:
        del parts.SIZERS["two"]


def test_a_ticks_exit_gives_the_stop_and_the_target_from_the_exit_module():
    """A short entry at 113 with a stop of 8 ticks (2 points) and a target of 3 R (6 points): 115 and 107."""
    result = run(build({"exit": {"kind": "ticks", "stop_ticks": 8, "r_multiple": 3.0}}))
    entry = result.intents[0][1][0]
    assert (entry.sl, entry.tp) == (Price(D("115")), Price(D("107")))


def test_the_library_is_the_indicator_module_and_a_warm_up_is_nan():
    from hokma.quant import registry
    import hokma.quant.indicators  # noqa: F401
    import pandas as pd

    c = closes()
    for kind, fn, kwargs, name, column in (("ema", lib.ema, {"length": 5}, "indicators.ema", "ema"), ("rsi", lib.rsi, {"length": 14}, "indicators.rsi", "rsi")):
        want = registry.describe(name).fn(pd.DataFrame({"close": c}), window=kwargs["length"], last_n=None).value["values"][column]
        got = lib.LIBRARY[kind](c, **kwargs)
        assert got.shape == c.shape and np.allclose(got, np.array(want, dtype=float), equal_nan=True)
    assert np.isnan(lib.ema(c, 5)[:4]).all() and not np.isnan(lib.ema(c, 5)[4:]).any()
    assert lib.ema(c[:3], 5).shape == (3,) and np.isnan(lib.ema(c[:3], 5)).all()           # fewer bars than the window: the warm-up, not an error
    bars = {"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": np.ones(len(c))}
    assert lib.atr(bars, 5)[4:30] == pytest.approx(2.0)
    with pytest.raises(BadStrategy):
        lib.ema(c, -3)                                                                       # a bad parameter is an error, never a column of NaN


def test_the_adx_the_channel_alpha_asks_for_is_the_one_of_the_indicator_module():
    """A choppy market, then a straight climb. The plain breakout enters on the first bar above the channel (cursor 61); with an ADX floor the entry
    waits for the first bar whose ADX of the indicator module reaches it."""
    i = np.arange(120)
    zigzag = 100 + (i % 2) * 3 + 0.05 * i
    close = np.concatenate([zigzag[:60], zigzag[59] + 1.5 * np.arange(1, 61)])
    bars = {"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": np.ones(120)}

    def first_entry(adx_min):
        alpha = ChannelBreakout(entry_n=3, exit_m=2, history=30, adx_min=adx_min)
        composite = parts.build_composite(alpha, sheet=nq(), desk_id=1, line="DT")
        return replay_intents(composite, bars, times=minutes(120), contract_id=CID).intents[0][0]

    adx = lib.adx(bars, 14)
    expected = next(cursor for cursor in range(61, 121) if adx[cursor - 1] >= 25.0)
    assert first_entry(0.0) == 61 and first_entry(25.0) == expected and expected > 61
    assert ChannelBreakout(adx_min=25.0).requests()[0].kind == "adx" and ChannelBreakout().requests() == ()


def test_the_half_life_fact_is_the_trailing_half_life_of_h3_and_a_window_that_does_not_revert_is_nan():
    """Every 20 bar window of x(t) = 10 + 0.8 (x(t-1) - 10) has phi = 0.8: ln 2 / -ln 0.8 = 3.1063 bars. A window of a straight climb has none."""
    import math

    x = [20.0]
    for _ in range(40):
        x.append(10.0 + 0.8 * (x[-1] - 10.0))
    got = lib.half_life(x, 20)
    assert np.isnan(got[:19]).all() and got[19:] == pytest.approx(math.log(2.0) / -math.log(0.8))
    assert np.isnan(lib.half_life([1.5 ** t for t in range(40)], 20)).all()
    from hokma.factory.alphas.mean_reversion import MeanReversion

    assert MeanReversion(history=40).requests()[0].kind == "half_life" and dict(MeanReversion(history=40).requests()[0].params) == {"length": 40}


def test_a_volatility_target_by_hand_through_the_seven_steps_of_the_sizing_module():
    """Equity 100000, target 10 percent: 10000 a year, 10000 / sqrt(252) = 629.94 a day. One contract at 100 with a daily volatility of 1 percent and a
    multiplier of 20 carries 100 x 0.01 x 20 = 20 a day: 629.94 / 20 = 31.5 contracts, cut to 31."""
    from types import SimpleNamespace

    from hokma.factory.strategy import build_context

    sizer = parts.build_part(parts.SIZERS, "sizer", "vol_target", {"target_vol": 0.10, "decay_pct": 94, "periods_per_year": 252}, parts.Env(nq(), "SWC"))
    n = 30
    arrays = {k: np.full(n, 100.0) for k in ("open", "high", "low", "close")} | {"volume": np.ones(n)}
    ctx = build_context(cursor=n, times=minutes(n), contract_id=CID, bars=arrays, indicators={"sizer_vol": np.full(n, 0.01)}, params={}, position=None, equity=D(100000))
    long = SimpleNamespace(value=1.0)
    got = sizer.size(signal=long, ctx=ctx)
    assert got == Size(SizeType.CONTRACTS, 31)
    assert sizer.requests()[0].kind == "ewma_vol" and dict(sizer.requests()[0].params) == {"decay_pct": 94}
    quiet = build_context(cursor=n, times=minutes(n), contract_id=CID, bars=arrays, indicators={"sizer_vol": np.full(n, float("nan"))}, params={}, position=None,
                          equity=D(100000))
    with pytest.raises(NoEntry):
        sizer.size(signal=long, ctx=quiet)                                                        # no volatility yet: no entry, nothing made up


def test_the_ewma_volatility_of_the_library_is_the_function_of_h3_at_each_bar():
    from hokma.quant import registry
    import hokma.quant.risk  # noqa: F401

    c = 100 * np.cumprod(1 + np.array([0.01, -0.02, 0.015, 0.0, -0.01, 0.02, -0.005, 0.01, 0.0, 0.012] * 6))
    got = lib.ewma_vol(c, 94, 20)
    returns = c[1:] / c[:-1] - 1.0
    assert np.isnan(got[:20]).all()
    for t in (20, 33, 59):
        assert got[t] == pytest.approx(registry.describe("risk.ewma_volatility").fn(returns=returns[:t], lambda_=0.94).value)


def test_an_atr_trailing_stop_follows_the_best_price_never_backs_off_and_leaves_at_the_next_open():
    """A short from the bar that closes at 113 (cursor 8). The ATR is 2, so the stop is 1.5 x 2 = 3 above the lowest close: at the low of 91 (cursor 30) it
    is 94. The climb makes the ATR 2.2, but a short stop never goes up: it stays 94. A close of 93 (cursor 31) is inside; 95 (cursor 32) touches it:
    the composite leaves, with the tag trail."""
    from hokma.factory.strategy import Position

    composite = build({"sizer": {"kind": "fixed_contracts", "n": 1}, "exit": {"kind": "atr_trailing", "length": 5, "atr_multiple": 1.5}})
    data = closes()
    bars = {"open": data, "high": data + 1, "low": data - 1, "close": data, "volume": np.ones(len(data))}
    held = lambda cursor: Position(CID, D(-1), D("113")) if cursor > 8 else None
    result = replay_intents(composite, bars, times=minutes(len(data)), contract_id=CID, position_at=held)
    exits_at = [(bar, i) for bar, batch in result.intents for i in batch if i.tag == "trail"]
    first_bar, first = exits_at[0]
    assert first_bar == 32 and first.reduce_only and first.side == "buy" and first.size.value == 1
    entry = result.intents[0][1][0]
    assert entry.side == "sell" and entry.sl == Price(D("116.00")) and entry.tp is None          # the first stop: 113 + 3 = 116, a stop of the order


def test_a_percent_trailing_stop_is_the_exit_module_and_a_flat_account_drops_its_state():
    from hokma.factory.strategy import build_context

    rule = parts.build_part(parts.EXITS, "exit rule", "trailing_pct", {"pct": 5.0}, parts.Env(nq(), "SWC"))
    n = 10
    arrays = {k: np.full(n, 100.0) for k in ("open", "high", "low", "close")} | {"volume": np.ones(n)}
    ctx = build_context(cursor=n, times=minutes(n), contract_id=CID, bars=arrays, indicators={}, params={}, position=None, equity=D(100000))
    assert rule.levels(side="buy", ctx=ctx) == (Price(D("95.00")), None)                         # 5 percent under 100
    stopped, state = rule.manage(side="buy", ctx=ctx, state=None)
    assert not stopped and state.stop == D("95.00") and state.extreme == D(100)
    low = build_context(cursor=n, times=minutes(n), contract_id=CID, bars={k: np.full(n, 94.0) for k in ("open", "high", "low", "close", "volume")},
                        indicators={}, params={}, position=None, equity=D(100000))
    stopped, state = rule.manage(side="buy", ctx=low, state=state)
    assert stopped and state.stop == D("95.00")                                                  # the stop did not move down with the price


# ---------------------------------------------------------------------------------------------------- filters and controls (SPEC-030, A1-3)
from datetime import datetime, timedelta, timezone  # noqa: E402

from hokma.contracts.identity import ContractSheet  # noqa: E402,F401
from hokma.factory.strategy import Position  # noqa: E402
from hokma.market import tz as marketzone  # noqa: E402

UTC_ = timezone.utc


def rising(n: int) -> np.ndarray:
    return 100.0 + np.arange(n, dtype=float)


def run_at(strategy, start: datetime, n: int, *, data=None, position_at=None, step: int = 1):
    """Replay minute bars (step minutes apart) that start at `start`; return the result and the times."""
    data = rising(n) if data is None else data
    times = [start + timedelta(minutes=step * i) for i in range(n)]
    bars = {"open": data, "high": data + 1, "low": data - 1, "close": data, "volume": np.ones(len(data))}
    return replay_intents(strategy, bars, times=times, contract_id=CID, position_at=position_at), times


def entries_at(result, times, zone=marketzone.NEW_YORK):
    """The local times of the bars on which an entry (not a reduce only order) was emitted."""
    return [times[bar - 1].astimezone(zone).strftime("%H:%M") for bar, batch in result.intents if any(not i.reduce_only for i in batch)]


def with_filters(*filters, **extra):
    return build({"filters": list(filters), **extra})


def minutes_between(first: str, last: str) -> list[str]:
    """Every minute from `first` to `last` inclusive, as HH:MM."""
    a, b = (int(first[:2]) * 60 + int(first[3:])), (int(last[:2]) * 60 + int(last[3:]))
    return [f"{m // 60:02d}:{m % 60:02d}" for m in range(a, b + 1)]


MON_WINTER = datetime(2025, 1, 6, 14, 0, tzinfo=UTC_)       # 09:00 in New York (EST)
MON_SUMMER = datetime(2025, 7, 7, 13, 0, tzinfo=UTC_)       # 09:00 in New York (EDT)


def test_a_session_filter_opens_only_inside_the_window_and_the_end_is_exclusive():
    result, times = run_at(with_filters({"kind": "session", "start": "09:45", "end": "11:30"}), MON_WINTER, 150)
    assert entries_at(result, times) == minutes_between("09:45", "11:29")


def test_the_session_window_follows_the_new_york_clock_through_daylight_saving_time():
    flt = {"kind": "session", "start": "09:45", "end": "11:30"}
    winter, tw = run_at(with_filters(flt), MON_WINTER, 150)
    summer, ts = run_at(with_filters(flt), MON_SUMMER, 150)
    assert entries_at(winter, tw) == entries_at(summer, ts) == minutes_between("09:45", "11:29")
    assert tw[winter.intents[0][0] - 1].hour == 14 and ts[summer.intents[0][0] - 1].hour == 13       # 09:45 is 14:45 UTC in winter and 13:45 in summer


def test_a_window_that_crosses_midnight_is_valid():
    start = datetime(2025, 1, 6, 20, 0, tzinfo=UTC_)
    result, times = run_at(with_filters({"kind": "session", "tz": "UTC", "start": "22:00", "end": "02:00"}), start, 8 * 60)
    got = entries_at(result, times, UTC_)
    assert got == minutes_between("22:00", "23:59") + minutes_between("00:00", "01:59")


def test_a_day_not_in_the_list_never_opens_and_the_default_is_monday_to_friday():
    friday = datetime(2025, 1, 3, 14, 0, tzinfo=UTC_)
    saturday = datetime(2025, 1, 4, 14, 0, tzinfo=UTC_)
    window = {"kind": "session", "start": "09:45", "end": "11:30"}
    assert run_at(with_filters({**window, "days": ["mon"]}), friday, 150)[0].intents == ()
    assert entries_at(*run_at(with_filters({**window, "days": ["fri"]}), friday, 150))
    assert entries_at(*run_at(with_filters(window), friday, 150))
    assert run_at(with_filters(window), saturday, 150)[0].intents == ()


def test_several_session_filters_are_united():
    one, two = {"kind": "session", "start": "09:45", "end": "10:00"}, {"kind": "session", "start": "11:00", "end": "11:15"}
    result, times = run_at(with_filters(one, two), MON_WINTER, 150)
    assert entries_at(result, times) == minutes_between("09:45", "09:59") + minutes_between("11:00", "11:14")


def test_max_trades_per_day_stops_at_n_and_starts_again_when_the_new_york_day_changes():
    start = datetime(2025, 1, 7, 4, 40, tzinfo=UTC_)                       # 23:40 in New York on the 6th
    result, times = run_at(with_filters({"kind": "max_trades_per_day", "n": 2}), start, 60)
    got = [times[bar - 1].astimezone(marketzone.NEW_YORK) for bar, _ in result.intents]
    assert [(t.day, t.strftime("%H:%M")) for t in got] == [(6, "23:47"), (6, "23:48"), (7, "00:00"), (7, "00:01")]


def test_a_reversal_counts_as_one_entry_and_its_exit_is_never_blocked():
    held = lambda cursor: Position(CID, D(1), D(100))                      # long, while the falling closes ask for a short
    down = 200.0 - np.arange(60, dtype=float)
    result, times = run_at(with_filters({"kind": "max_trades_per_day", "n": 2}), MON_WINTER, 60, data=down, position_at=held)
    sells = [bar for bar, batch in result.intents if any(not i.reduce_only for i in batch)]
    exits = [bar for bar, batch in result.intents if any(i.reduce_only for i in batch)]
    assert len(sells) == 2 and len(exits) == len(result.intents) > 2        # two reversals, then only exits
    assert all(len(batch) == 2 for bar, batch in result.intents if bar in sells)


def test_no_filter_blocks_an_exit():
    held = lambda cursor: Position(CID, D(1), D(100))
    down = 200.0 - np.arange(60, dtype=float)
    shut = {"kind": "session", "start": "03:00", "end": "03:01"}              # a window that none of the bars is in
    result, _ = run_at(with_filters(shut), MON_WINTER, 60, data=down, position_at=held)
    assert result.intents and all(i.reduce_only for _, batch in result.intents for i in batch)


def trending(n: int) -> np.ndarray:
    return 100.0 + 3.0 * np.arange(n)


def test_the_adx_regime_filter_opens_in_a_trend_and_not_before_the_value_exists():
    flt = {"kind": "regime_adx", "length": 5, "min": 25}
    result, _ = run_at(with_filters(flt), MON_WINTER, 80, data=trending(80))
    plain, _ = run_at(with_filters(), MON_WINTER, 80, data=trending(80))
    assert plain.intents[0][0] < result.intents[0][0] and len(result.intents) > 40          # no value yet: no entry on the first bars
    flat, _ = run_at(with_filters(flt), MON_WINTER, 80, data=100.0 + (np.arange(80) % 2) * 0.5 + np.arange(80) * 1e-6)
    assert flat.intents == ()                                                                 # a market with no trend is outside the range


def test_the_adx_regime_filter_can_ask_for_a_ceiling_only():
    calm, _ = run_at(with_filters({"kind": "regime_adx", "length": 5, "max": 5}), MON_WINTER, 80, data=trending(80))
    assert calm.intents == ()                                                                 # a strong trend is above a ceiling of 5


def test_the_volatility_regime_filter_uses_the_annualized_ewma_volatility():
    data = 100.0 * np.cumprod(1.0 + 0.001 * np.where(np.arange(120) % 2 == 0, 1.0, 0.2))     # a steady rise with small returns
    ok = {"kind": "regime_vol", "decay_pct": 94, "periods_per_year": 252, "min": 0.0001, "max": 5.0}
    result, _ = run_at(with_filters(ok), MON_WINTER, 120, data=data)
    assert result.intents and result.intents[0][0] > 20                                       # NaN until 20 returns exist: no entry before
    low = {"kind": "regime_vol", "decay_pct": 94, "periods_per_year": 252, "max": 0.0001}
    assert run_at(with_filters(low), MON_WINTER, 120, data=data)[0].intents == ()


def test_flat_by_time_closes_the_position_once_and_blocks_new_entries_for_the_rest_of_the_day():
    flt = {"kind": "flat_by_time", "time": "15:55"}
    start = datetime(2025, 1, 6, 20, 40, tzinfo=UTC_)                       # 15:40 in New York
    held = lambda cursor: Position(CID, D(2), D(100))
    result, times = run_at(with_filters(flt), start, 40, position_at=held)
    first = result.intents[0]
    assert times[first[0] - 1].astimezone(marketzone.NEW_YORK).strftime("%H:%M") == "15:55"
    assert [(i.tag, i.reduce_only, i.side, i.size.value) for i in first[1]] == [("flat_by_time", True, "sell", D(2))]
    flat, tf = run_at(with_filters(flt), start, 40)
    assert entries_at(flat, tf) == minutes_between("15:47", "15:54")         # the alpha warms up on bar 8; from 15:55 nothing opens


def test_a_filter_with_a_wrong_parameter_or_an_unknown_zone_fails_when_the_spec_is_checked():
    bad = [{"kind": "session", "start": "9:45", "end": "11:30"}, {"kind": "session", "start": "09:45"},
           {"kind": "session", "start": "09:45", "end": "09:45"}, {"kind": "session", "start": "09:45", "end": "11:30", "tz": "Mars/Base"},
           {"kind": "session", "start": "09:45", "end": "11:30", "days": ["funday"]}, {"kind": "session", "start": "09:45", "end": "11:30", "extra": 1},
           {"kind": "max_trades_per_day", "n": 0}, {"kind": "max_trades_per_day", "n": 101}, {"kind": "max_trades_per_day"},
           {"kind": "regime_adx", "length": 1, "min": 10}, {"kind": "regime_adx", "length": 14}, {"kind": "regime_adx", "length": 14, "min": 50, "max": 40},
           {"kind": "regime_adx", "length": 14, "min": 101}, {"kind": "regime_vol", "decay_pct": 94, "periods_per_year": 252},
           {"kind": "regime_vol", "decay_pct": 40, "periods_per_year": 252, "min": 0.1}, {"kind": "flat_by_time", "time": "25:00"},
           {"kind": "nothing"}, {"start": "09:45"}]
    for item in bad:
        with pytest.raises(BadStrategy):
            parts.validate_parts({"filters": [item]})
        with pytest.raises(BadStrategy):
            build({"filters": [item]})


def test_the_list_of_filters_is_checked_as_a_whole():
    session = {"kind": "session", "start": "09:45", "end": "11:30"}
    for bad in ("session", {"kind": "session"}, [session] * 11, [{"kind": "max_trades_per_day", "n": 1}] * 2, [{"kind": "flat_by_time", "time": "15:55"}] * 2):
        with pytest.raises(BadStrategy):
            parts.validate_parts({"filters": bad})
    assert parts.validate_parts({"filters": [session, {"kind": "session", "start": "13:00", "end": "14:00"}]})["filters"][1]["start"] == "13:00"
    assert parts.validate_parts({"filters": []}) == {}                           # no filters is the same as none


def test_the_validated_filters_carry_their_defaults_so_two_spellings_alike():
    a = parts.validate_parts({"filters": [{"kind": "session", "start": "09:45", "end": "11:30"}]})
    b = parts.validate_parts({"filters": [{"end": "11:30", "kind": "session", "days": ["mon", "tue", "wed", "thu", "fri"], "start": "09:45",
                                           "tz": "America/New_York"}]})
    assert a == b and a["filters"][0]["tz"] == "America/New_York" and a["filters"][0]["days"] == ["mon", "tue", "wed", "thu", "fri"]


def test_a_composite_without_filters_behaves_as_before():
    plain = run(build({"sizer": {"kind": "fixed_contracts", "n": 3}}))
    same = run(build({"sizer": {"kind": "fixed_contracts", "n": 3}, "filters": []}))
    assert plain.intents == same.intents and plain.intents
