"""Drew, the earnings drift model of the PEAD variant (SPEC-019: B8): AH-10, AH-33, CTS-001, CTS-002, CTS-017, CTS-037 and AFS-028.

One test name per piece id. Hand written events give hand written signals; the screens run for real through the registry on built bars and
on the recorded SPY bars. Nothing here touches the network."""
from __future__ import annotations

import dataclasses
import importlib.util
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from b8_support import KIT, ROOT, candles, load_registry
from dbsupport import db, env  # noqa: F401  (fixtures)
from exec_support import AGENT, NQ, desks_db, ready  # noqa: F401  (fixtures)
from quant.indicators.helpers import expand_weekly
from swo_kit_support import build_kit

from hokma.desks.agents import analyst_kit as kit
from hokma.desks.agents import investors as inv
from hokma.desks.agents import pead
from hokma.desks.agents.manager import load_mandate
from hokma.desks.core.signal import AlphaModel, Signal, contains_execution_terms
from hokma.desks.core.skills import split_frontmatter
from hokma.desks.plantel import SEATS, seats_for
from hokma.desks.swo.create import create_swo_desk
from hokma.quant import registry
from account_links_support import link_principal

_spec = importlib.util.spec_from_file_location("check_repo_rules", ROOT / "scripts" / "check_repo_rules.py")
repo_rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repo_rules)

UTC = timezone.utc
CALL = "Acme Corp Q2 FY2024 Earnings Call"
RELEASE = datetime(2024, 6, 11, 11, 0, tzinfo=UTC)          # Tuesday, before the open: that session is the first on the results
FRIDAY = datetime(2024, 6, 14, 20, 0, tzinfo=UTC)           # the fourth session: the last one inside the window
NEXT_MONDAY = datetime(2024, 6, 17, 20, 0, tzinfo=UTC)      # the fifth session: the window is over


@pytest.fixture(autouse=True)
def registry_ready():
    load_registry()


def event(*, call=CALL, published=RELEASE, timing="BMO", actual=1.12, estimate=1.00) -> pead.EarningsEvent:
    return pead.EarningsEvent(call, published, timing, actual, estimate)


def view(ev, *, as_of=FRIDAY, bars=None, symbol="ACME", desk_id=1, **kw) -> Signal:
    return pead.signal_for(desk_id=desk_id, symbol=symbol, as_of=as_of, inputs=pead.PeadInputs(ev, bars=bars, **kw))


def ctx_of(ev, *, as_of=FRIDAY, bars=None, **kw):
    return pead.build_context(desk_id=1, symbol="ACME", as_of=as_of, inputs=pead.PeadInputs(ev, bars=bars, **kw))


# ---- bars of the weekly setup (the same hand built weeks as the screen tests) ------------------------------------------------

PRE = 6
FLAT = (100.0, 100.5, 99.5, 100.0)
GREEN1 = (108.5, 111.0, 108.0, 110.5)
RED = (110.5, 111.0, 108.0, 109.0)
INSIDE = (109.0, 110.5, 108.5, 110.0)
BREAK = (110.0, 113.0, 109.5, 112.0)


def weeks(rows, gap_open=105.0):
    spec = [FLAT] * PRE + [(gap_open, 109.0, min(gap_open, 108.0) - 0.5, 108.0)] + list(rows)
    frame = pd.DataFrame(spec, columns=["open", "high", "low", "close"], index=pd.date_range("2023-01-06", periods=len(spec), freq="W-FRI"))
    frame["volume"] = 10_000_000.0
    return frame


def setup(rows):
    """(daily bars, event, as_of): results on the Monday of the gap week and the cut-off on the Monday after the last closed week."""
    weekly = weeks(rows)
    monday = weekly.index[PRE] - pd.Timedelta(days=4)
    published = datetime(monday.year, monday.month, monday.day, 11, 0, tzinfo=UTC)
    after = weekly.index[-1] + pd.Timedelta(days=3)
    return expand_weekly(weekly), event(published=published, call="Acme Q2 FY2023 results call"), datetime(after.year, after.month, after.day, 20, 0, tzinfo=UTC)


# ---- AH-10: the model ----------------------------------------------------------------------------------------------------------

def test_AH_10_pead_model_is_an_alpha_model_that_gives_the_hand_written_signals_for_the_fixture_events():
    assert isinstance(pead.PeadModel(), AlphaModel) and not hasattr(pead.PeadModel(), "_model")
    beat = view(event(actual=1.12, estimate=1.00))
    assert (beat.source, beat.line, beat.horizon, beat.value, beat.confidence, beat.abstained) == ("A23", "SWO", "days", 0.65, 65, False)
    mild = view(event(actual=1.06, estimate=1.00))
    assert (mild.value, mild.confidence) == (0.55, 55)
    miss = view(event(actual=0.97, estimate=1.00))
    assert (miss.value, miss.confidence) == (-0.4, 40)
    big_miss = view(event(actual=0.80, estimate=1.00))
    assert (big_miss.value, big_miss.confidence) == (-0.65, 65)
    flat = view(event(actual=1.00, estimate=1.00))
    assert flat.value == 0.0 and not flat.abstained
    for signal in (beat, miss, flat):
        assert signal.confidence <= pead.PEAD_CONFIDENCE_CAP and contains_execution_terms(signal.reason) is None
        assert "Q2 FY2024" in signal.reason and "Weights of the screens are not validated" in signal.reason
    assert "beat" in beat.reason and "missed" in miss.reason and "matched" in flat.reason
    assert [c.name for c in beat.criteria][:2] == ["eps_surprise", "sessions_since_reaction_session"]
    assert Signal.from_json(beat.to_json()) == beat


def test_AH_10_without_a_published_result_the_model_abstains_and_names_the_gap():
    for ev, gap in ((event(actual=None), "published_result"), (event(estimate=None), "published_result"),
                    (event(published=FRIDAY + timedelta(days=1)), "published_result")):
        signal = view(ev)
        assert signal.abstained and signal.value is None and gap in signal.data_missing
    assert view(None).data_missing == ("earnings_event",)
    assert view(event(timing=None)).data_missing == ("timing",)
    assert "No published result" in view(event(actual=None)).reason


def test_AH_10_the_view_lives_four_sessions_from_the_first_session_on_the_results():
    assert pead.reaction_session(event()).isoformat() == "2024-06-11"
    assert pead.reaction_session(event(timing="AMC")).isoformat() == "2024-06-12"
    assert pead.reaction_session(event(published=datetime(2024, 6, 15, 12, tzinfo=UTC))).isoformat() == "2024-06-17"      # a Saturday rolls forward
    assert [pead.session_index(event(), datetime(2024, 6, d, 20, tzinfo=UTC)) for d in (10, 11, 12, 13, 14, 17)] == [-1, 0, 1, 2, 3, 4]
    for day in (11, 12, 13, 14):
        assert not view(event(), as_of=datetime(2024, 6, day, 20, tzinfo=UTC)).abstained
    assert view(event(), as_of=NEXT_MONDAY).data_missing == ("drift_window_over",)
    after_close = event(timing="AMC", published=datetime(2024, 6, 11, 21, 0, tzinfo=UTC))
    early = view(after_close, as_of=datetime(2024, 6, 11, 22, 0, tzinfo=UTC))
    assert early.value == 0.65 and "has not started" in early.reason
    assert "session 4 of 4" in view(event(), as_of=FRIDAY).reason


def test_AH_10_an_estimate_of_zero_has_no_percentage_and_gets_the_lowest_tier():
    signal = view(event(actual=0.10, estimate=0.0))
    assert signal.confidence == 40 and "with no percentage" in signal.reason
    assert ctx_of(event(actual=0.10, estimate=0.0)).facts["surprise_pct"] is None
    assert pead.tier_confidence(None) == 40 and pead.tier_confidence(0.0499) == 40 and pead.tier_confidence(-0.05) == 55 and pead.tier_confidence(0.10) == 65


def test_AH_10_the_event_refuses_a_naive_time_a_bad_timing_or_an_unknown_field():
    with pytest.raises(pead.PeadError):
        pead.EarningsEvent(CALL, datetime(2024, 6, 11, 11, 0), "BMO", 1.0, 1.0)
    with pytest.raises(pead.PeadError):
        pead.EarningsEvent(CALL, RELEASE, "pre", 1.0, 1.0)
    with pytest.raises(pead.PeadError):
        pead.EarningsEvent.from_dict({"call_name": CALL, "published_at": "2024-06-11T11:00:00Z", "price_target": 4})
    ok = pead.EarningsEvent.from_dict({"call_name": CALL, "published_at": "2024-06-11T11:00:00Z", "timing": "BMO", "actual_eps": 1.1, "estimate_eps": 1.0})
    assert ok.published_at == RELEASE
    with pytest.raises(pead.PeadError):
        pead.PeadInputs(event(), asset_class="crypto")


# ---- AFS-028: the fiscal quarter is read from the name of the call ------------------------------------------------------------

def test_AFS_028_the_fiscal_quarter_comes_from_the_name_of_the_call_and_never_from_the_calendar_date():
    late = datetime(2024, 10, 29, 12, 0, tzinfo=UTC)               # a calendar Q4 date
    ev = event(call="Acme Corp Q1 FY2025 Earnings Call", published=late)
    ctx = ctx_of(ev, as_of=late + timedelta(days=1))
    assert ctx.facts["fiscal_quarter"] == "Q1 FY2025" and "Q1 FY2025" in view(ev, as_of=late + timedelta(days=1)).reason
    assert registry.call("fundamentals.fiscal_quarter", call_name=ev.call_name, calendar_date="2024-10-29").value["label"] == "Q1 FY2025"
    for name in ("Acme Corp Earnings Call", "Acme Q1 FY2025 and Q2 FY2025 call"):
        signal = view(event(call=name))
        assert signal.abstained and signal.data_missing == ("fiscal_quarter",) and "not in the name of the call" in signal.reason
    assert "INV" not in " ".join(c.name for c in view(ev, as_of=late + timedelta(days=1)).criteria)


def test_AFS_028_drew_carries_the_data_discipline_skill_and_every_multiple_needs_a_basis():
    prompt = pead.system_prompt(line="SWO", desk_id=1, as_of=FRIDAY)
    assert "## Skill: earnings-data-discipline" in prompt
    skill = (KIT / "skills" / "earnings-data-discipline" / "SKILL.md").read_text(encoding="utf-8")
    assert "LTM" in skill and "NTM" in skill
    from hokma.quant.errors import BadInput

    with pytest.raises(BadInput):
        registry.call("fundamentals.valuation_multiple", price=100.0, quarterly_values=[1.0, 1.0, 1.0, 1.0], basis="TTM")
    assert registry.call("fundamentals.valuation_multiple", price=100.0, quarterly_values=[1.0, 1.0, 1.0, 1.0], basis="LTM").value is not None


# ---- CTS-001: the weekly setup on closed candles --------------------------------------------------------------------------------

def test_CTS_001_a_breakout_on_a_closed_weekly_bar_keeps_a_long_view_after_the_four_sessions_and_a_pullback_alone_caps_it():
    daily, ev, as_of = setup([GREEN1, RED, INSIDE, BREAK, (112.4, 114.0, 112.0, 113.0)])
    ctx = ctx_of(ev, as_of=as_of, bars=daily)
    assert ctx.facts["mode"] == "setup" and ctx.facts["weekly_setup"]["state"] == "BREAKOUT"
    signal = pead.PeadModel().signal(ctx)
    assert (signal.value, signal.confidence) == (0.65, 65)
    named = {c.name: c for c in signal.criteria}
    assert named["weekly_setup_supports"].passed is True and named["weekly_setup_supports"].source == "indicators.screens.pead"
    assert "BREAKOUT" in signal.reason and "session" in signal.reason
    ready_daily, ready_ev, ready_at = setup([GREEN1, RED])
    ready = pead.PeadModel().signal(ctx_of(ready_ev, as_of=ready_at, bars=ready_daily))
    assert ready.confidence == 55 and ready.value == 0.55                    # the pullback closed, the breakout did not happen
    text = ready.to_json() + signal.to_json()
    assert not re.search(r"\b(stop|entry|target)\b", text, re.I)             # the view carries no price and no exit
    assert "CTS-001" in registry.describe(pead.SCREEN).piece_ids and pead.SCREEN in [s.name for s in registry.list("indicators")]


def test_CTS_001_the_week_in_progress_and_a_live_price_never_change_the_view():
    daily, ev, as_of = setup([GREEN1, RED, INSIDE])
    last_friday = daily.index[-1]
    live = pd.DataFrame({"open": [110.0, 112.5], "high": [112.6, 114.0], "low": [109.9, 112.4], "close": [112.5, 113.9], "volume": [4e6, 4e6]},
                        index=[last_friday + pd.Timedelta(days=3), last_friday + pd.Timedelta(days=4)])      # Monday and Tuesday close above the red high
    tuesday = datetime(last_friday.year, last_friday.month, last_friday.day, 20, tzinfo=UTC) + timedelta(days=4)
    base = pead.PeadModel().signal(ctx_of(ev, as_of=as_of, bars=daily))
    extended = pead.PeadModel().signal(ctx_of(ev, as_of=tuesday, bars=pd.concat([daily, live])))
    weekly = lambda s: [c for c in s.criteria if c.name == "weekly_setup_supports"]
    assert weekly(base) == weekly(extended) and base.value == extended.value == 0.55
    assert ctx_of(ev, as_of=tuesday, bars=pd.concat([daily, live])).facts["weekly_setup"]["state"] == "SIGNAL_READY"
    cutday = pd.concat([daily, pd.DataFrame({"open": [100.0], "high": [130.0], "low": [99.0], "close": [129.0], "volume": [9e6]}, index=[pd.Timestamp(as_of.date())])])
    assert pead.PeadModel().signal(ctx_of(ev, as_of=as_of, bars=cutday)).value == base.value          # the bar of the cut-off day is never read


def test_CTS_001_after_the_window_only_a_beat_with_a_living_setup_keeps_a_view_and_a_voided_setup_is_neutral():
    daily, ev, as_of = setup([GREEN1, RED, INSIDE, BREAK])
    miss = dataclasses.replace(ev, actual_eps=0.8)
    assert pead.PeadModel().signal(ctx_of(miss, as_of=as_of, bars=daily)).data_missing == ("drift_window_over",)       # the screen is long only
    expired_daily, expired_ev, expired_at = setup([GREEN1, RED, (109.0, 109.5, 106.0, 107.0), (107.0, 107.5, 105.0, 106.0), (106.0, 106.5, 104.0, 105.0),
                                                   (105.0, 105.5, 103.0, 104.0), (104.0, 104.5, 102.0, 103.0)])
    expired = ctx_of(expired_ev, as_of=expired_at, bars=expired_daily)
    assert expired.facts["weekly_setup"]["state"] == "EXPIRED"
    assert pead.PeadModel().signal(expired).abstained
    inside = ctx_of(event(), as_of=FRIDAY)
    voided_ctx = dataclasses.replace(inside, facts={**inside.facts, "weekly_setup": {"state": "EXPIRED"}})
    voided = pead.PeadModel().signal(voided_ctx)
    assert voided.value == 0.0 and "expired" in voided.reason


# ---- CTS-002: the reaction score ---------------------------------------------------------------------------------------------

def test_CTS_002_the_reaction_score_is_context_with_the_unvalidated_weights_warning_and_never_changes_the_view():
    bars = candles()
    day = bars.index[600]
    published = datetime(day.year, day.month, day.day, 11, 0, tzinfo=UTC)
    ev = event(published=published)
    as_of = datetime(day.year, day.month, day.day, 20, 0, tzinfo=UTC) + timedelta(days=2)
    ctx = ctx_of(ev, as_of=as_of, bars=bars)
    score = ctx.facts["reaction_score"]
    assert 0.0 <= score["score"] <= 100.0 and any(w.startswith("weights_unvalidated") for w in score["warnings"])
    named = {c.name: c for c in ctx.criteria}
    assert named["reaction_score"].value == score["score"] and named["reaction_score"].passed is None
    with_score = pead.PeadModel().signal(ctx)
    without = view(ev, as_of=as_of)
    assert with_score.value == without.value and with_score.confidence == without.confidence
    assert "CTS-002" in registry.describe(pead.REACTION).piece_ids and pead.REACTION in [s.name for s in registry.list("indicators")]
    assert "Weights of the screens are not validated" in with_score.reason


def test_CTS_002_the_score_needs_closed_sessions_and_without_them_it_is_a_gap_not_a_number():
    daily, ev, as_of = setup([GREEN1, RED])
    ctx = ctx_of(ev, as_of=as_of, bars=daily)
    assert "reaction_score" not in ctx.facts and any(w.startswith("reaction_score_not_available") for w in ctx.facts["warnings"])
    assert "reaction_score" not in {c.name for c in ctx.criteria}


# ---- CTS-017: the episodic pivot ------------------------------------------------------------------------------------------------

def test_CTS_017_the_catalyst_of_an_episodic_pivot_stays_unverified_and_the_pivot_is_context_only():
    bars = candles()
    day = bars.index[600]
    ev = event(published=datetime(day.year, day.month, day.day, 11, 0, tzinfo=UTC))
    as_of = datetime(day.year, day.month, day.day, 20, 0, tzinfo=UTC) + timedelta(days=2)
    base = view(ev, as_of=as_of, bars=bars)
    ctx = ctx_of(ev, as_of=as_of, bars=bars, catalyst="guidance raise confirmed by a person")
    pivot = ctx.facts["episodic_pivot"]
    assert pivot["catalyst_verified"] is False and pivot["state"] in {"ACTIONABLE_DAY1", "DAY1_WATCH", "DELAYED_EP_WATCH", "CATALYST_WATCH", "REJECT"}
    assert pead.PeadModel().signal(ctx).value == base.value
    assert "catalyst_verified" not in {f.name for f in dataclasses.fields(pead.PeadInputs)}      # no caller can mark it verified
    assert "episodic_pivot" not in ctx_of(ev, as_of=as_of, bars=bars).facts
    assert "CTS-017" in registry.describe(pead.EPISODIC).piece_ids and pead.EPISODIC in [s.name for s in registry.list("indicators")]


# ---- CTS-037: the news reaction test --------------------------------------------------------------------------------------------

def _noise(n=900, seed=131):
    return pd.Series(np.random.default_rng(seed).normal(0, 0.01, n), index=pd.bdate_range("2020-01-01", periods=n))


def test_CTS_037_the_reaction_test_is_a_context_by_significance_and_never_a_signal():
    returns = _noise()
    positions = list(range(300, 800, 15))
    table = [{"date": returns.index[p].date().isoformat(), "timing": "BMO"} for p in positions]
    sold = returns.copy()
    for p in positions:
        sold.iloc[p:p + 3] -= 0.02
    args = {"returns": sold, "events": table, "model": "mean_adjusted", "estimation_start": -120, "estimation_end": -21}
    plain = view(event())
    ctx = ctx_of(event(), reaction_failure=args)
    assert ctx.facts["reaction_failure"] == {"verdict": "CONFIRMED", "n_groups": len(positions), "role": "context only"}
    assert pead.PeadModel().signal(ctx).value == plain.value                   # a confirmed failure does not move the view
    few = ctx_of(event(), reaction_failure={**args, "events": table[:2]})
    assert few.facts["reaction_failure"]["verdict"] == "INSUFFICIENT_EVIDENCE"
    noise = ctx_of(event(), reaction_failure={"returns": _noise(seed=121), "events": table, "model": "mean_adjusted", "estimation_start": -120,
                                                "estimation_end": -21})
    assert noise.facts["reaction_failure"]["verdict"] != "CONFIRMED"               # noise is not read as a failure of the market
    assert ctx_of(event(actual=1.0, estimate=1.0), reaction_failure=args).facts.get("reaction_failure") is None     # equal figures: nothing to test
    assert "CTS-037" in registry.describe(pead.FAILURE).piece_ids and pead.FAILURE in [s.name for s in registry.list("stats")]


# ---- the seat file, its prompt and its tools ---------------------------------------------------------------------------------------

def test_AH_33_the_agent_file_of_drew_declares_swo_and_its_five_skills_and_scopes_the_prompt_to_its_line():
    seat = SEATS["A23"]
    path = KIT / "agents" / f"{seat.slug}.md"
    text = path.read_text(encoding="utf-8")
    meta, body = split_frontmatter(text, str(path))
    assert meta["lines"] == ["SWO"] and meta["skills"] == ["pead-screen", "earnings-reaction-score", "episodic-pivot", "news-reaction-failure",
                                                           "earnings-data-discipline"]
    assert repo_rules.check(ROOT, [path.relative_to(ROOT).as_posix()]) == [] and chr(0x2014) not in text and not re.search(r"[^\x00-\x7f]", text)
    assert "never sends an order" in body and "Futures" in body
    prompt = pead.system_prompt(line="SWO", desk_id=1, as_of=FRIDAY)
    assert body.strip()[:60] in prompt and all(f"## Skill: {n}" in prompt for n in meta["skills"])
    for line in ("SWC", "LP", "DT", "RF"):
        with pytest.raises(kit.AnalystKitError):
            pead.system_prompt(line=line, desk_id=1, as_of=FRIDAY)
    for other in ("DT", "SWC", "LP", "RF"):
        assert "A23" not in {s.code for s in seats_for(other, None)}
    assert "A23" not in {s.code for s in seats_for("SWO", "traditional")} and "A23" in {s.code for s in seats_for("SWO", "pead")}
    box = pead.toolbox()
    assert box.names() == ("quant_call", "data_filings") == pead.seat_tools() and not any("order" in n for n in box.names())


def test_AH_33_the_tools_of_drew_reach_only_the_indicators_and_stats_modules():
    box = pead.toolbox()
    blocked = box.call("quant_call", {"name": "fundamentals.owner_earnings", "params": {}})
    assert not blocked.ok and blocked.error_kind == "blocked"
    ok = box.call("quant_call", {"name": "fundamentals.fiscal_quarter", "params": {"call_name": CALL}})
    assert not ok.ok                                       # fundamentals are not in his modules: the quarter is read by code before he speaks


# ---- the strategy on a desk with its mandate (AH-33) -----------------------------------------------------------------------------

@pytest.fixture
def world(db, tmp_path):
    with db.write() as u:
        u.profile.save(owner_name="Ana", fund_name="Test Fund")
    return db, build_kit(tmp_path / "kit")


def other(seat, value, conf=70):
    return Signal(source=seat, desk_id=1, line="SWO", symbol="ACME", as_of=FRIDAY, horizon="weeks", value=value, confidence=conf, abstained=False,
                  criteria=(), data_missing=(), reason="A view of the strategy. Read from the criteria.", snapshot_hash=f"o{seat}")


def test_AH_33_the_default_mandate_of_the_variant_names_drew_declares_futures_and_the_strategy_runs_on_its_desk(world):
    db, kit_root = world
    mandate = load_mandate(KIT / "mandates" / "swo-pead.yaml")
    assert "future" in mandate.asset_classes and "future" in (KIT / "mandates" / "swo-pead.yaml").read_text(encoding="utf-8")
    strategy = next(s for s in mandate.strategies if s.name == pead.STRATEGY)
    assert [(m.name, m.weight) for m in strategy.models] == [("A23", 3.0), ("A20", 1.0), ("A16", 1.0)]
    desk = create_swo_desk("Drift", 50_000, variant="pead", kit_root=kit_root, db=db)["id"]
    run = pead.run_pead_strategy(desk, symbol="ACME", as_of=FRIDAY, inputs=pead.PeadInputs(event()),
                                 others=[other("A20", 0.2), other("A16", 0.4), other("A18", -0.9)], db=db)
    assert run.signal.value == 0.65 and run.signal_id and run.asset_class == "stock"
    stored = inv.load_signals(desk, symbol="ACME", db=db)
    assert [s.source for s in stored] == ["A23"] and stored[0] == run.signal
    assert run.filter.n_used == 3 and run.filter.passes                         # A18 is not a model of this strategy: its view does not count
    expected = (3 * 0.65 * 0.65 + 0.7 * 0.2 + 0.7 * 0.4) / (3 * 0.65 + 0.7 + 0.7)
    assert run.filter.blended == pytest.approx(expected, abs=1e-9)
    again = pead.run_pead_strategy(desk, symbol="ACME", as_of=FRIDAY, inputs=pead.PeadInputs(event()), others=[other("A20", 0.2)], db=db)
    assert again.signal_id == run.signal_id                                      # the same view is stored once
    nothing = pead.run_pead_strategy(desk, symbol="ACME", as_of=FRIDAY, inputs=pead.PeadInputs(event(actual=None)), db=db, persist=False)
    assert nothing.signal.abstained and nothing.filter.reason_code == "no_view" and nothing.signal_id is None


def test_AH_33_the_strategy_refuses_a_desk_that_is_not_a_pead_desk_a_seat_that_is_off_or_an_asset_class_outside_the_mandate(world):
    db, kit_root = world
    traditional = create_swo_desk("Plain", 50_000, kit_root=kit_root, db=db)["id"]
    with pytest.raises(pead.PeadError):
        pead.run_pead_strategy(traditional, symbol="ACME", as_of=FRIDAY, inputs=pead.PeadInputs(event()), db=db)
    stock_only = {"name": "Drift stocks", "line": "SWO", "variant": "pead", "asset_classes": ["stock", "option"],
                  "strategies": [{"name": "earnings-drift", "horizon": "weeks", "models": [{"name": "A23"}]}]}
    desk = create_swo_desk("Drift stocks", 50_000, variant="pead", mandate=stock_only, kit_root=kit_root, db=db)["id"]
    with pytest.raises(pead.PeadError):
        pead.run_pead_strategy(desk, symbol="NQ", as_of=FRIDAY, inputs=pead.PeadInputs(event(), asset_class="future"), db=db)
    assert pead.run_pead_strategy(desk, symbol="ACME", as_of=FRIDAY, inputs=pead.PeadInputs(event()), db=db).signal.value == 0.65
    with db.write() as u:
        u.session.execute("UPDATE desk_seats SET active = 0 WHERE desk_id = :d AND seat_code = 'A23'", {"d": desk})
    with pytest.raises(pead.PeadError):
        pead.run_pead_strategy(desk, symbol="ACME", as_of=FRIDAY, inputs=pead.PeadInputs(event()), db=db)
    no_drew = {"name": "No Drew", "line": "SWO", "variant": "pead", "strategies": [{"name": "s", "horizon": "weeks", "models": [{"name": "A16"}]}]}
    other_desk = create_swo_desk("No Drew", 50_000, variant="pead", mandate=no_drew, kit_root=kit_root, db=db)["id"]
    with pytest.raises(pead.PeadError):
        pead.run_pead_strategy(other_desk, symbol="ACME", as_of=FRIDAY, inputs=pead.PeadInputs(event()), db=db)


# ---- the futures case in the simulated broker (AH-33, K24) ------------------------------------------------------------------------

def test_AH_33_the_same_method_adapts_to_a_future_and_its_order_goes_through_the_single_send_point_to_the_simulated_broker(ready, tmp_path):
    from hokma import exec as hexec
    from hokma.exec import levels
    from hokma.exec.proposal import Leg, OrderProposal

    desk = create_swo_desk("Drift futures", 1_000_000, variant="pead", kit_root=build_kit(tmp_path / "kit"), db=ready.db)["id"]
    with ready.db.write() as u:
        account = u.broker.create_account(ready.broker_id, "Sim B", 1_000_000)
        link_principal(u, desk, account.id)
    levels.set_level(desk, 2, who=ready.owner, db=ready.db)
    run = pead.run_pead_strategy(desk, symbol="NQ", as_of=FRIDAY, inputs=pead.PeadInputs(event(), asset_class="future"), db=ready.db)
    assert run.asset_class == "future" and run.signal.value == 0.65 and run.signal.symbol == "NQ"
    proposal = OrderProposal(desk_id=desk, line="SWO", origin="desk", legs=(Leg(NQ, "buy", "open", Decimal("1")),), order_type="market",
                             thesis="Long drift after a beat on the future of the index.", created_by="desk-chief",
                             stop_loss=Decimal("20950"), risk_usd=Decimal("1000"))
    out = hexec.submit(proposal, who=AGENT, db=ready.db)
    assert out.status == "awaiting_owner", (out.status, out.code)                     # level 2: the owner decides, the code sends
    done = hexec.approve(out.proposal_id, who=ready.owner, db=ready.db)
    assert done.status == "filled"
    with ready.db.read() as u:
        fills = u.session.query("SELECT price, multiplier FROM sim_fills ORDER BY id")
        placed = u.session.query("SELECT approved_by FROM broker_orders WHERE action = 'place' AND rule_result = 'auto' AND desk_id = :d", {"d": desk})
    assert fills and fills[0]["multiplier"] == "20" and Decimal(fills[0]["price"]) >= Decimal("21000.25")
    assert placed and placed[0]["approved_by"] == "owner"
    assert not any("order" in tool for tool in SEATS["A23"].tools)                      # Drew has no tool that sends anything
