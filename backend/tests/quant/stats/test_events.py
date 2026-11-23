"""Event study, its statistics, types and chart, the earnings preview and the news reaction failure test.

Rows AH-57, AH-58, AH-59, AH-60, FT-089, AFS-027 and CTS-037.
"""
import dataclasses
import json
import math

import numpy as np
import pandas as pd
import pytest

from hokma.quant.errors import BadInput
from hokma.quant.stats import events

from .conftest import call, piece_ids, rng

EST = {"estimation_start": -120, "estimation_end": -21}


@pytest.fixture(scope="module")
def alternating():
    """400 business days of +1% and -1% in turn: the mean of any window of even length is exactly zero."""
    idx = pd.bdate_range("2022-01-03", periods=400)
    return pd.Series(0.01 * np.where(np.arange(400) % 2 == 0, 1.0, -1.0), index=idx)


def _with_bumps(series, bumps):
    out = series.copy()
    for pos, size in bumps.items():
        out.iloc[pos] += size
    return out


def test_event_study_AH_57_timing_rule_and_hand_values(alternating):
    idx = alternating.index
    # a report before the open reacts on its own date; the same report after the close reacts on the next trading day
    returns = _with_bumps(alternating, {250: 0.05, 381: 0.05})
    events_table = [{"event_id": "bmo", "date": idx[250].date().isoformat(), "timing": "BMO"},
                    {"event_id": "amc", "date": idx[380].date().isoformat(), "timing": "AMC"}]
    res = call("stats.event_study", returns=returns, events=events_table, model="mean_adjusted", window_start=0, window_end=0, **EST).value
    bmo, amc = res["records"]
    assert bmo["reaction_date"] == idx[250].date().isoformat() and amc["reaction_date"] == idx[381].date().isoformat()
    sigma = 0.01 * math.sqrt(100 / 99)
    assert bmo["car"] == pytest.approx(0.05 + float(alternating.iloc[250]), abs=1e-12) and amc["car"] == pytest.approx(0.05 + float(alternating.iloc[381]), abs=1e-12)
    assert bmo["sigma"] == pytest.approx(sigma, abs=1e-12) and bmo["n_estimation"] == 100
    assert bmo["t_car"] == pytest.approx(bmo["car"] / sigma, abs=1e-10)
    assert res["car_mean"] == pytest.approx((bmo["car"] + amc["car"]) / 2, abs=1e-12) and res["n_events"] == 2
    # a three day window sums the abnormal returns
    wide = call("stats.event_study", returns=returns, events=events_table[:1], model="mean_adjusted", window_start=-1, window_end=1, **EST).value
    assert wide["records"][0]["car"] == pytest.approx(float(returns.iloc[249:252].sum()), abs=1e-12)
    assert wide["caar"][-1] == pytest.approx(wide["records"][0]["car"], abs=1e-12) and wide["relative_days"] == [-1, 0, 1]
    # an event dated on a Saturday rolls to the next trading day
    saturday = [{"event_id": "sat", "date": (idx[250] + pd.Timedelta(days=(5 - idx[250].weekday()) % 7 or 7)).date().isoformat(), "timing": "BMO"}]
    rolled = call("stats.event_study", returns=returns, events=saturday, model="mean_adjusted", window_start=0, window_end=0, **EST).value
    assert pd.Timestamp(rolled["records"][0]["reaction_date"]).weekday() < 5
    assert {"AH-57", "FT-089"} <= set(piece_ids("stats.event_study"))


def test_event_study_FT_089_market_model_matches_a_plain_regression():
    g = rng(111)
    idx = pd.bdate_range("2021-01-04", periods=500)
    market = pd.Series(g.normal(0.0004, 0.01, 500), index=idx)
    stock = 0.0005 + 1.2 * market + g.normal(0, 0.005, 500)
    stock.iloc[300] += 0.04
    res = call("stats.event_study", returns=stock, market_returns=market, events=[{"date": idx[300].date().isoformat(), "timing": "BMO"}],
               window_start=0, window_end=0, estimation_start=-250, estimation_end=-30).value
    rec = res["records"][0]
    est = slice(300 - 250, 300 - 30 + 1)
    design = np.column_stack([np.ones(221), market.iloc[est].to_numpy()])
    beta = np.linalg.lstsq(design, stock.iloc[est].to_numpy(), rcond=None)[0]
    assert rec["alpha"] == pytest.approx(beta[0], abs=1e-10) and rec["beta"] == pytest.approx(beta[1], abs=1e-10)
    resid = stock.iloc[est].to_numpy() - design @ beta
    assert rec["sigma"] == pytest.approx(math.sqrt(resid @ resid / 219), abs=1e-10)
    assert rec["car"] == pytest.approx(stock.iloc[300] - (beta[0] + beta[1] * market.iloc[300]), abs=1e-10)
    assert rec["car"] == pytest.approx(0.04, abs=0.015)
    adjusted = call("stats.event_study", returns=stock, market_returns=market, model="market_adjusted", window_start=0, window_end=0,
                    events=[{"date": idx[300].date().isoformat(), "timing": "BMO"}], estimation_start=-250, estimation_end=-30).value["records"][0]
    assert adjusted["car"] == pytest.approx(stock.iloc[300] - market.iloc[300], abs=1e-12)
    assert call("stats.event_study", returns=stock, events=[{"date": idx[300].date().isoformat(), "timing": "BMO"}]).value is None   # the market model needs a market


def test_event_study_AH_57_pead_on_the_fixture_feeds_the_blocking_window(closes, earnings_events):
    returns = closes.pct_change().dropna()
    good, bad = earnings_events[earnings_events["surprise_z"] > 0], earnings_events[earnings_events["surprise_z"] < 0]
    up = call("stats.event_study", returns=returns, events=good, model="mean_adjusted", window_start=0, window_end=5).value
    down = call("stats.event_study", returns=returns, events=bad, model="mean_adjusted", window_start=0, window_end=5).value
    assert up["car_mean"] > 0.02 and up["car_t"] > 3.0 and down["car_mean"] < -0.02 and down["car_t"] < -2.0
    assert up["n_events"] == len(good) and up["share_positive"] > 0.7
    assert up["blocking_window"][0] == 0                                    # the reaction day itself is significant
    # the reaction day of an after the close report is the next trading day
    first = earnings_events.iloc[0]
    assert first["timing"] == "AMC"
    rec = call("stats.event_study", returns=returns, events=earnings_events.iloc[:1], model="mean_adjusted", window_start=0, window_end=0).value["records"][0]
    assert pd.Timestamp(rec["reaction_date"]) == returns.index[returns.index.get_loc(pd.Timestamp(first["date"])) + 1]
    assert up["patell_z"] is not None


def test_event_study_AH_57_empty_nan_and_out_of_domain(alternating):
    idx = alternating.index
    ok = [{"date": idx[250].date().isoformat(), "timing": "BMO"}]
    assert call("stats.event_study", returns=pd.Series([], dtype=float, index=pd.DatetimeIndex([])), events=ok, model="mean_adjusted").value is None
    assert call("stats.event_study", returns=alternating, events=[], model="mean_adjusted").value is None
    no_timing = call("stats.event_study", returns=alternating, events=[{"date": idx[250].date().isoformat()}], model="mean_adjusted", **EST)
    assert no_timing.value is None and any("timing_missing" in w for w in no_timing.warnings)        # a timing is never guessed
    default = call("stats.event_study", returns=alternating, events=[{"date": idx[250].date().isoformat()}], model="mean_adjusted", default_timing="BMO", **EST)
    assert default.value["n_events"] == 1
    early = call("stats.event_study", returns=alternating, events=[{"date": idx[10].date().isoformat(), "timing": "BMO"}], model="mean_adjusted", **EST)
    assert early.value is None and any("outside_data" in w for w in early.warnings)
    assert call("stats.event_study", returns=alternating, events=ok, model="garch").value is None
    assert call("stats.event_study", returns=alternating, events=ok, model="mean_adjusted", estimation_end=5).value is None
    assert call("stats.event_study", returns=alternating, events=ok, model="mean_adjusted", default_timing="NOON", **EST).value is None
    assert call("stats.event_study", returns=alternating, events=[{"date": "not a date", "timing": "BMO"}], model="mean_adjusted", **EST).value is None
    nan = alternating.copy()
    nan.iloc[100] = np.nan
    assert any("dropped" in w for w in call("stats.event_study", returns=nan, events=ok, model="mean_adjusted", **EST).warnings)
    with pytest.raises(BadInput):
        call("stats.event_study", returns=alternating.reset_index(drop=True), events=ok, model="mean_adjusted")
    with pytest.raises(BadInput):
        call("stats.event_study", returns=alternating, events=[{"timing": "BMO"}], model="mean_adjusted")
    with pytest.raises(BadInput):
        call("stats.event_study", returns=alternating, events="2022-01-03", model="mean_adjusted")
    with pytest.raises(BadInput):
        call("stats.event_study", returns=alternating, events=ok, model="mean_adjusted", window_end=1.5)


def test_event_stats_AH_58_hand_values_and_seeded_bootstrap():
    values = pd.Series([0.01, 0.02, 0.03, 0.04, 0.05])
    res = call("stats.event_stats", values=values, seed=7, n_boot=1000).value
    std = math.sqrt(0.00025)                                           # variance of 1 to 5 hundredths with n - 1 is 0.00025
    assert res["mean"] == pytest.approx(0.03, abs=1e-12) and res["std"] == pytest.approx(std, abs=1e-12)
    assert res["t_stat"] == pytest.approx(0.03 / (std / math.sqrt(5)), abs=1e-10) and res["t_stat"] == pytest.approx(4.2426407, abs=1e-6)
    assert 0.01 < res["p_value"] < 0.02 and res["share_positive"] == 1.0 and res["median"] == pytest.approx(0.03)
    draws = np.random.default_rng(7).integers(0, 5, (1000, 5))
    boot = values.to_numpy()[draws].mean(axis=1)
    assert res["ci_low"] == pytest.approx(np.quantile(boot, 0.025), abs=1e-12) and res["ci_high"] == pytest.approx(np.quantile(boot, 0.975), abs=1e-12)
    assert call("stats.event_stats", values=values, seed=7, n_boot=1000).value == res           # same seed, same interval
    wide = pd.Series(rng(5).normal(0.01, 0.03, 40))
    assert call("stats.event_stats", values=wide, seed=1).value["ci_low"] != call("stats.event_stats", values=wide, seed=2).value["ci_low"]
    assert res["ci_low"] <= 0.03 <= res["ci_high"]
    strict = call("stats.event_stats", values=values, threshold_t=5.0).value
    loose = call("stats.event_stats", values=values, threshold_t=2.0).value
    assert strict["passes_threshold"] is False and loose["passes_threshold"] is True              # each desk fixes its own threshold
    assert call("stats.event_stats", values=values).value["passes_threshold"] is None
    assert call("stats.event_stats", values=pd.Series([], dtype=float)).value is None
    assert call("stats.event_stats", values=values.iloc[:2]).value is None
    assert call("stats.event_stats", values=pd.Series(np.ones(10))).value is None
    assert call("stats.event_stats", values=values, ci=1.0).value is None
    assert call("stats.event_stats", values=values, n_boot=10).value is None
    assert call("stats.event_stats", values=values, threshold_t=-1.0).value is None
    assert any("dropped" in w for w in call("stats.event_stats", values=pd.Series([0.01, 0.02, np.nan, 0.04, 0.05])).warnings)
    with pytest.raises(BadInput):
        call("stats.event_stats", values=values, n_boot=500000)
    with pytest.raises(BadInput):
        call("stats.event_stats", values=values, seed=1.5)
    assert "AH-58" in piece_ids("stats.event_stats")


def test_event_types_AH_59_are_frozen_and_serializable(alternating):
    idx = alternating.index
    res = events.compute_event_study(_with_bumps(alternating, {250: 0.05}), None, events._events_frame([{"date": idx[250].date().isoformat(), "timing": "BMO"}]),
                                     model="mean_adjusted", estimation=(-120, -21), window=(-1, 1), min_estimation=30, default_timing=None)
    assert isinstance(res, events.EventStudyResult) and isinstance(res.records[0], events.EventRecord)
    with pytest.raises(dataclasses.FrozenInstanceError):
        res.model = "other"                                                    # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        res.records[0].car = 1.0                                               # type: ignore[misc]
    data = res.to_dict()
    assert json.loads(json.dumps(data)) == data
    assert data["records"][0]["event_id"] == "E001" and data["relative_days"] == [-1, 0, 1] and isinstance(data["records"][0]["ar"], list)
    stats = events.compute_event_stats(np.array([0.01, 0.02, 0.03, 0.04]), seed=1, n_boot=200, ci=0.9, threshold_t=2.0)
    assert isinstance(stats, events.EventStats) and json.loads(json.dumps(stats.to_dict())) == stats.to_dict()
    with pytest.raises(dataclasses.FrozenInstanceError):
        stats.mean = 0.0                                                       # type: ignore[misc]
    result = call("stats.event_study", returns=_with_bumps(alternating, {250: 0.05}), model="mean_adjusted",
                  events=[{"date": idx[250].date().isoformat(), "timing": "BMO"}], **EST)
    assert json.loads(json.dumps(result.to_dict()))["value"]["n_events"] == 1


def test_event_plot_AH_60_is_one_file_without_external_resources():
    days = list(range(-3, 4))
    caar = [0.0, 0.001, 0.002, 0.03, 0.032, 0.033, 0.035]
    res = call("stats.event_plot", relative_days=days, caar=caar, aar=[0.0, 0.001, 0.001, 0.028, 0.002, 0.001, 0.002],
               ci_low=[-0.01] * 7, ci_high=[0.04] * 7, title="Drift <b>after</b> results & more")
    page = res.value["html"]
    assert page.startswith("<!DOCTYPE html>") and "<svg" in page and "</svg>" in page and page.count("<polyline") == 1
    for forbidden in ("http", "<script", "<link", "src=", "@import", "url("):
        assert forbidden not in page, forbidden
    assert "&lt;b&gt;after&lt;/b&gt;" in page and "<b>" not in page                       # the title is escaped
    assert res.value["n_days"] == 7 and "days relative to the reaction day" in page
    assert call("stats.event_plot", relative_days=days, caar=caar).value["html"].count("<rect") == 1      # only the background without bars
    assert call("stats.event_plot", relative_days=[1], caar=[0.1]).value is None
    assert call("stats.event_plot", relative_days=[], caar=[]).value is None
    assert call("stats.event_plot", relative_days=days, caar=caar[:6] + [float("inf")]).value is None
    assert call("stats.event_plot", relative_days=days, caar=caar, aar=[0.1]).value is None
    assert call("stats.event_plot", relative_days=days, caar=caar, ci_low=[0.0] * 6, ci_high=[0.1] * 7).value is None
    with pytest.raises(BadInput):
        call("stats.event_plot", relative_days=days, caar=caar[:3])
    with pytest.raises(BadInput):
        call("stats.event_plot", relative_days=days, caar=caar, title=5)
    assert "AH-60" in piece_ids("stats.event_plot")


def _bar(frame, key):
    return frame[key]


def test_earnings_reaction_AFS_027_hand_values(ohlcv, earnings_events, option_chain):
    res = call("stats.earnings_reaction", open_=ohlcv["open"], close=ohlcv["close"], events=earnings_events, chain=option_chain,
               next_event={"date": "2026-11-24", "timing": "AMC"})
    v = res.value
    # reaction of an after the close report (E01, 2022-01-31): the last close of that day to the next open plus one tick
    idx = ohlcv.index
    amc_day = idx.get_loc(pd.Timestamp("2022-01-31"))
    first = next(e for e in v["events"] if e["event_id"] == "E01")
    assert first["reaction_date"] == idx[amc_day + 1].date().isoformat()
    assert first["reaction"] == pytest.approx((ohlcv["open"].iloc[amc_day + 1] + 0.01) / ohlcv["close"].iloc[amc_day] - 1, abs=1e-12)
    # a before the open report (E02, 2022-04-28): the previous close to that day's open plus one tick
    bmo_day = idx.get_loc(pd.Timestamp("2022-04-28"))
    second = next(e for e in v["events"] if e["event_id"] == "E02")
    assert second["reaction"] == pytest.approx((ohlcv["open"].iloc[bmo_day] + 0.01) / ohlcv["close"].iloc[bmo_day - 1] - 1, abs=1e-12)
    assert second["close_to_close"] == pytest.approx(ohlcv["close"].iloc[bmo_day] / ohlcv["close"].iloc[bmo_day - 1] - 1, abs=1e-12)
    # implied move: the at the money straddle of the first expiration after the reaction day (2026-11-25)
    rows = [r for r in option_chain["options"] if r["expiration"] == "2026-12-04" and r["strike"] == 450.0]
    mids = {r["type"]: (r["bid"] + r["ask"]) / 2 for r in rows}
    assert v["implied"]["expiration"] == "2026-12-04" and v["implied"]["atm_strike"] == 450.0
    assert v["implied"]["straddle"] == pytest.approx(mids["call"] + mids["put"], abs=1e-12)
    assert v["implied"]["implied_move"] == pytest.approx((mids["call"] + mids["put"]) / 450.0, abs=1e-12)
    assert v["implied"]["upper_breakeven"] == pytest.approx(450.0 + v["implied"]["straddle"], abs=1e-12)
    reactions = np.array([e["reaction"] for e in v["events"]])
    assert v["history"]["mean_abs"] == pytest.approx(np.mean(np.abs(reactions)), abs=1e-12) and v["history"]["n_events"] == 12
    q = np.quantile(reactions, [0.25, 0.5, 0.75])
    assert [v["scenarios"][k]["move"] for k in ("bear", "base", "bull")] == pytest.approx(list(q), abs=1e-12)
    assert v["scenarios"]["bull"]["multiple_of_implied"] == pytest.approx(abs(q[2]) / v["implied"]["implied_move"], abs=1e-12)
    assert 3 <= len(v["factors"]) <= 5
    assert any("no calibrated threshold" in f for f in v["factors"])
    assert any("more than 7 calendar days" in w for w in res.warnings)                  # the expiration is 9 days after the reaction
    assert "AFS-027" in piece_ids("stats.earnings_reaction")


def test_earnings_reaction_AFS_027_options_and_scenarios(ohlcv, earnings_events, option_chain):
    base = dict(open_=ohlcv["open"], close=ohlcv["close"], events=earnings_events)
    given = call("stats.earnings_reaction", **base, chain=option_chain, expiration="2026-12-18", scenarios={"bull": 0.05, "base": 0.01, "bear": -0.04}).value
    assert given["implied"]["expiration"] == "2026-12-18" and given["scenario_source"] == "given" and given["scenarios"]["bear"]["move"] == -0.04
    assert given["scenarios"]["bear"]["historical_share_at_least_as_extreme"] == pytest.approx(np.mean([e["reaction"] <= -0.04 for e in given["events"]]))
    few = call("stats.earnings_reaction", open_=ohlcv["open"], close=ohlcv["close"], events=earnings_events.iloc[:6])
    assert any("small sample" in w for w in few.warnings)
    without = call("stats.earnings_reaction", **base)
    assert without.value["implied"] is None and any("no option chain" in w for w in without.warnings) and 3 <= len(without.value["factors"]) <= 5
    seller = call("stats.earnings_reaction", **base, side=-1).value
    buyer = call("stats.earnings_reaction", **base, side=1).value
    assert seller["events"][0]["fill"] == pytest.approx(buyer["events"][0]["first_open"] - 0.01) and buyer["events"][0]["fill"] == pytest.approx(buyer["events"][0]["first_open"] + 0.01)
    assert call("stats.earnings_reaction", **base, chain=option_chain, scenarios={"bull": -0.05, "base": 0.0, "bear": 0.05}).value is None
    assert call("stats.earnings_reaction", **base, chain=option_chain, expiration="2030-01-01").value is None
    assert call("stats.earnings_reaction", **base, chain=option_chain).value is None                       # a chain needs the next event or an expiration
    crossed = json.loads(json.dumps(option_chain))
    for r in crossed["options"]:
        r["bid"], r["ask"] = r["ask"] + 1, r["bid"]
    assert call("stats.earnings_reaction", **base, chain=crossed, expiration="2026-12-04").value is None
    assert call("stats.earnings_reaction", **base, chain={"spot": 0.0, "options": option_chain["options"]}, expiration="2026-12-04").value is None
    assert call("stats.earnings_reaction", **base, chain=option_chain, next_event={"date": "2026-11-24"}).value is None
    assert call("stats.earnings_reaction", **base, tick_size=-1.0).value is None
    assert call("stats.earnings_reaction", **base, side=0).value is None
    assert call("stats.earnings_reaction", open_=ohlcv["open"], close=ohlcv["close"], events=earnings_events.drop(columns="timing")).value is None
    assert call("stats.earnings_reaction", open_=ohlcv["open"], close=ohlcv["close"], events=earnings_events.drop(columns="timing"), default_timing="BMO").value is not None
    assert call("stats.earnings_reaction", open_=ohlcv["open"].iloc[:10], close=ohlcv["close"].iloc[:10], events=earnings_events).value is None
    assert call("stats.earnings_reaction", open_=pd.Series([], dtype=float, index=pd.DatetimeIndex([])), close=pd.Series([], dtype=float, index=pd.DatetimeIndex([])), events=earnings_events).value is None
    with pytest.raises(BadInput):
        call("stats.earnings_reaction", open_=ohlcv["open"].reset_index(drop=True), close=ohlcv["close"], events=earnings_events)
    with pytest.raises(BadInput):
        call("stats.earnings_reaction", **base, chain={"spot": 450})
    with pytest.raises(BadInput):
        call("stats.earnings_reaction", **base, scenarios={"bull": 1})


def _noise(seed, n=900):
    return pd.Series(rng(seed).normal(0, 0.01, n), index=pd.bdate_range("2020-01-01", periods=n))


def test_group_overlapping_CTS_037_hand_values():
    assert events.group_overlapping([100, 101, 102, 150, 200, 250], 3) == [[0, 1, 2], [3], [4], [5]]          # four independent groups
    assert len(events.group_overlapping([100, 102, 104], 3)) == 1                                              # chained overlaps are one group
    assert len(events.group_overlapping([100, 103], 3)) == 2                                                   # windows 100-102 and 103-105 do not overlap
    assert events.group_overlapping([], 3) == []
    assert events.group_overlapping([250, 100], 3) == [[1], [0]]


def test_reaction_failure_CTS_037_noise_is_not_confirmed():
    returns = _noise(121)
    positions = rng(122).choice(np.arange(300, 880, 12), 40, replace=False)
    table = [{"date": returns.index[p].date().isoformat(), "timing": "BMO"} for p in sorted(positions)]
    res = call("stats.reaction_failure", returns=returns, events=table, model="mean_adjusted", estimation_start=-120, estimation_end=-21).value
    assert res["verdict"] != "CONFIRMED" and res["n_groups"] == 40
    assert 0.3 < res["naive_failure_share"] < 0.7                  # the plain ratio of failures sits near half with pure noise
    false_positives = 0
    for seed in range(200, 260):
        r = _noise(seed)
        tbl = [{"date": r.index[p].date().isoformat(), "timing": "BMO"} for p in range(300, 880, 15)]
        false_positives += call("stats.reaction_failure", returns=r, events=tbl, model="mean_adjusted", estimation_start=-120, estimation_end=-21).value["verdict"] == "CONFIRMED"
    assert false_positives <= 9                                    # about 5% of 60 runs
    assert "CTS-037" in piece_ids("stats.reaction_failure")


def test_reaction_failure_CTS_037_verdicts():
    returns = _noise(131)
    positions = list(range(300, 800, 15))
    table = [{"date": returns.index[p].date().isoformat(), "timing": "BMO"} for p in positions]
    sold = returns.copy()
    reacted = returns.copy()
    for p in positions:
        sold.iloc[p:p + 3] -= 0.02
        reacted.iloc[p:p + 3] += 0.02
    kw = dict(events=table, model="mean_adjusted", estimation_start=-120, estimation_end=-21)
    failed = call("stats.reaction_failure", returns=sold, **kw).value
    assert failed["verdict"] == "CONFIRMED" and failed["p_failure"] < 0.001 and failed["mean_directional_car"] < -0.04 and failed["t_stat"] < 0
    ok = call("stats.reaction_failure", returns=reacted, **kw).value
    assert ok["verdict"] == "NOT_CONFIRMED" and ok["p_reaction"] < 0.001 and "reacted" in ok["reason"]
    flipped = call("stats.reaction_failure", returns=reacted, direction=-1, **kw).value                   # bad news that kept rising is a failure to fall
    assert flipped["verdict"] == "CONFIRMED"
    two = call("stats.reaction_failure", returns=sold, events=table[:2], model="mean_adjusted", estimation_start=-120, estimation_end=-21).value
    assert two["verdict"] == "INSUFFICIENT_EVIDENCE" and two["t_stat"] is None and two["n_groups"] == 2    # fails closed under 3 groups
    crowded = [{"date": returns.index[p].date().isoformat(), "timing": "BMO"} for p in range(400, 410)]       # ten events on consecutive days
    merged = call("stats.reaction_failure", returns=sold, events=crowded, model="mean_adjusted", estimation_start=-120, estimation_end=-21).value
    assert merged["n_events"] == 10 and merged["n_groups"] == 1 and merged["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert merged["group_sizes"] == [10]


def test_reaction_failure_CTS_037_empty_nan_and_out_of_domain():
    returns = _noise(141)
    table = [{"date": returns.index[p].date().isoformat(), "timing": "BMO"} for p in range(300, 700, 20)]
    kw = dict(model="mean_adjusted", estimation_start=-120, estimation_end=-21)
    assert call("stats.reaction_failure", returns=pd.Series([], dtype=float, index=pd.DatetimeIndex([])), events=table, **kw).value is None
    assert call("stats.reaction_failure", returns=returns, events=[], **kw).value is None
    assert call("stats.reaction_failure", returns=returns, events=table, direction=0, **kw).value is None
    assert call("stats.reaction_failure", returns=returns, events=table, window=0, **kw).value is None
    assert call("stats.reaction_failure", returns=returns, events=table, alpha=0.9, **kw).value is None
    assert call("stats.reaction_failure", returns=returns, events=table, min_groups=2, **kw).value is None
    assert call("stats.reaction_failure", returns=returns, events=table, window=40, **kw).value is None
    assert call("stats.reaction_failure", returns=returns, events=table, model="market", **{k: v for k, v in kw.items() if k != "model"}).value is None
    no_timing = call("stats.reaction_failure", returns=returns, events=[{"date": e["date"]} for e in table], **kw)
    assert no_timing.value is None and any("timing_missing" in w for w in no_timing.warnings)
    nan = returns.copy()
    nan.iloc[50] = np.nan
    assert any("dropped" in w for w in call("stats.reaction_failure", returns=nan, events=table, **kw).warnings)
    with pytest.raises(BadInput):
        call("stats.reaction_failure", returns=returns.reset_index(drop=True), events=table, **kw)
    with pytest.raises(BadInput):
        call("stats.reaction_failure", returns=returns, events=table, window=1.5, **kw)
    with pytest.raises(BadInput):
        call("stats.reaction_failure", returns=returns, events=table, window=500000, **kw)
