"""CTS-017: episodic pivot classifier. Catalyst quality, four confirmations, the states and the fill correction."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.indicators.screens import episodic_pivot as ep

from .helpers import stamp

DAY1 = 30


def bars(tail: tuple[tuple[float, float, float, float, float, float], ...] = (), *, day1_low: float = 107.5) -> pd.DataFrame:
    """30 flat sessions (range 1.0, 1 million shares) and a day 1 at position 30: open 108, close 111.5, range 4.5, 4 million shares.
    tail: further sessions as (open, high, low, close, volume) tuples (the sixth element is ignored)."""
    rows = [(100.0, 100.5, 99.5, 100.0, 1_000_000.0)] * DAY1
    rows.append((108.0, 112.0, day1_low, 111.5, 4_000_000.0))
    rows += [t[:5] for t in tail]
    frame = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=pd.bdate_range("2024-01-02", periods=len(rows)))
    return frame


def run(frame, catalyst="earnings", verified=True, **kw):
    if "reaction_date" not in kw:
        kw["reaction_date"] = str(frame.index[DAY1].date())
    return ep.episodic_pivot(frame, catalyst=catalyst, catalyst_verified=verified, as_of=stamp(frame, len(frame) - 1), **kw)


def test_actionable_day1_by_hand():
    res = run(bars())
    v = res.value
    assert v["state"] == "ACTIONABLE_DAY1" and v["checks"] == {"range": True, "volume": True, "close": True, "risk": True}
    assert v["range_ratio"] == pytest.approx(4.5 / 1.0) and v["volume_ratio"] == pytest.approx(4.0)
    assert v["close_position"] == pytest.approx(4.0 / 4.5)
    assert v["stop"] == 107.5 and v["pead_handoff"] is True and v["catalyst_quality"] == "high"
    assert not any("catalyst_unverified" in w for w in res.warnings)


def test_the_risk_uses_the_next_open_plus_one_tick_when_that_bar_exists():
    est = run(bars()).value
    assert est["entry_is_estimate"] is True and est["entry"] == pytest.approx(111.51)               # close + 1 tick, shown as an estimate
    assert est["risk_pct"] == pytest.approx((111.51 - 107.5) / 111.51)
    nxt = bars(((111.2, 112.0, 110.9, 111.8, 2e6, 0),))
    real = run(nxt, reaction_date=str(nxt.index[DAY1].date())).value
    assert real["entry_is_estimate"] is False and real["entry"] == pytest.approx(111.21)           # the next open plus 1 tick, not the signal close
    assert real["risk_pct"] == pytest.approx((111.21 - 107.5) / 111.21)
    assert real["same_bar_priority"] == "stop_first" and real["entry_rule"] == "next_open_plus_1_tick"


def test_an_unverified_catalyst_keeps_the_candidate_out_of_actionable():
    res = run(bars(), verified=False)
    assert res.value["state"] == "DAY1_WATCH" and any(w.startswith("catalyst_unverified") for w in res.warnings)
    assert res.value["pead_handoff"] is True


def test_a_failed_confirmation_is_a_watch_not_an_action():
    wide = run(bars(day1_low=95.0)).value                  # risk to the low is 14.8 percent: three of four hold
    assert wide["state"] == "DAY1_WATCH" and wide["checks"]["risk"] is False and wide["checks_passed"] == 3
    weak = bars()
    weak.iloc[DAY1] = (100.4, 100.9, 99.9, 100.0, 1_100_000.0)
    res = run(weak).value
    assert res["state"] == "REJECT" and res["checks_passed"] < 2


def test_catalyst_quality_decides_reject_and_the_handoff():
    assert run(bars(), catalyst="mna").value["state"] == "REJECT"
    assert run(bars(), catalyst="mna").value["reason"] == "catalyst_quality_not_enough"
    rumor = run(bars(), catalyst="rumor")
    assert rumor.value["state"] == "REJECT" and any(w.startswith("unknown_catalyst") for w in rumor.warnings)
    fda = run(bars(), catalyst="fda").value
    assert fda["state"] == "ACTIONABLE_DAY1" and fda["pead_handoff"] is False
    assert run(bars(), catalyst="guidance").value["pead_handoff"] is True
    assert run(bars(), catalyst="contract").value["catalyst_quality"] == "medium"


def test_a_good_catalyst_without_a_reaction_session_is_catalyst_watch():
    frame = bars()
    none = run(frame, reaction_date=None)
    assert none.value["state"] == "CATALYST_WATCH" and none.value["pead_handoff"] is False
    assert run(frame, reaction_date="2030-01-01").value["state"] == "CATALYST_WATCH"


def test_delayed_ep_watch_and_its_exits():
    held = bars(((111.2, 112.0, 110.9, 111.8, 2e6, 0), (111.8, 113.0, 111.5, 112.5, 2e6, 0), (112.5, 113.4, 112.0, 113.0, 2e6, 0)))
    v = run(held).value
    assert v["state"] == "DELAYED_EP_WATCH" and v["age_sessions"] == 3 and v["pead_handoff"] is True
    broke = bars(((111.2, 112.0, 110.9, 111.8, 2e6, 0), (111.8, 112.0, 106.0, 107.0, 2e6, 0)))
    assert run(broke).value["state"] == "REJECT" and run(broke).value["reason"] == "day1_low_broken"
    old = bars(tuple((112.0, 113.0, 111.5, 112.5, 2e6, 0) for _ in range(6)))
    assert run(old).value["state"] == "REJECT" and run(old).value["reason"] == "reaction_too_old"
    assert run(old, delayed_max=8).value["state"] == "DELAYED_EP_WATCH"


def test_only_closed_bars_are_used():
    frame = bars()
    open_bar = pd.concat([frame, pd.DataFrame({"open": [90.0], "high": [91.0], "low": [80.0], "close": [81.0], "volume": [9e7]}, index=[frame.index[-1] + pd.offsets.BDay(1)])])
    at = stamp(frame, len(frame) - 1)
    a = ep.episodic_pivot(open_bar, catalyst="earnings", catalyst_verified=True, reaction_date=str(frame.index[DAY1].date()), as_of=at)
    b = ep.episodic_pivot(frame, catalyst="earnings", catalyst_verified=True, reaction_date=str(frame.index[DAY1].date()), as_of=at)
    assert a.value == b.value and a.value["state"] == "ACTIONABLE_DAY1"


def test_empty_nan_short_and_bad_parameters():
    frame = bars()
    assert ep.episodic_pivot(frame.iloc[:0], catalyst="earnings", as_of="2024-03-01").value is None
    nan = frame.copy()
    nan.iloc[5, nan.columns.get_loc("volume")] = np.nan
    assert run(nan).value is None
    assert run(frame.iloc[:20], reaction_date=None).value is None
    early = run(frame, reaction_date=str(frame.index[10].date()))
    assert early.value is None and "21 sessions" in early.warnings[0]
    assert run(frame, range_multiple=-1.0).value is None
    assert run(frame, close_position_min=1.5).value is None
    with pytest.raises(BadInput):
        run(frame, catalyst=5)
    with pytest.raises(BadInput):
        run(frame, verified="yes")
    with pytest.raises(BadInput):
        run(frame, volume_multiple="x")
    with pytest.raises(BadInput):
        run(frame, delayed_max=2.5)
    with pytest.raises(BadInput):
        ep.episodic_pivot(frame.reset_index(drop=True), catalyst="earnings")


def test_registered_and_called_through_the_registry():
    spec = registry.describe("indicators.screens.episodic_pivot")
    assert spec.piece_ids == ("CTS-017",)
    frame = bars()
    res = registry.call("indicators.screens.episodic_pivot", bars=frame, catalyst="earnings", catalyst_verified=True,
                        reaction_date=str(frame.index[DAY1].date()), as_of=stamp(frame, len(frame) - 1))
    assert res.value["state"] == "ACTIONABLE_DAY1"


def test_on_the_fixed_daily_bars_a_random_day_is_classified(spy):
    day = str(spy.index[-1].date())
    res = ep.episodic_pivot(spy, catalyst="earnings", catalyst_verified=False, reaction_date=day, as_of=stamp(spy, len(spy) - 1))
    assert res.ok and res.value["state"] in {"DAY1_WATCH", "REJECT"} and res.value["age_sessions"] == 0
