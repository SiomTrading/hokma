"""Sentiment scoring. Row ECT-070."""
import numpy as np
import pandas as pd
import pytest

from hokma.quant.errors import BadInput

from .conftest import call, piece_ids, rng


def _docs(rows):
    return pd.DataFrame(rows, columns=["received_at", "published_at", "p_positive", "p_negative"])


def test_sentiment_score_ECT_070_hand_values_and_reception_time():
    docs = _docs([
        ("2026-08-03T10:00:00Z", "2026-08-03T09:59:00Z", 0.7, 0.1),
        ("2026-08-03T12:00:00Z", "2026-08-03T11:00:00Z", 0.5, 0.3),
        ("2026-08-04T00:10:00Z", "2026-08-03T23:50:00Z", 0.2, 0.6),            # published on the 3rd, received on the 4th
    ])
    res = call("stats.sentiment_score", docs=docs)
    assert res.value["dates"] == ["2026-08-03", "2026-08-04"] and res.value["n_docs"] == [2, 1]
    assert res.value["score"] == pytest.approx([(0.6 + 0.2) / 2, -0.4], abs=1e-12)      # S = P(positive) - P(negative), averaged per day
    assert res.value["date_basis"] == "received_at" and res.value["latest_score"] == pytest.approx(-0.4)
    ny = call("stats.sentiment_score", docs=_docs([("2026-08-04T02:00:00Z", "2026-08-04T01:00:00Z", 0.6, 0.2)]), timezone="America/New_York")
    assert ny.value["dates"] == ["2026-08-03"]                                              # 22:00 the evening before in New York
    assert "ECT-070" in piece_ids("stats.sentiment_score")


def test_sentiment_score_ECT_070_never_dates_by_publication_and_drops_bad_documents():
    docs = _docs([
        ("2026-08-03T10:00:00Z", "2026-08-03T09:00:00Z", 0.7, 0.1),
        (None, "2026-08-03T11:00:00Z", 0.9, 0.0),                                  # no reception time: dropped, not dated by publication
        ("2026-08-03T12:00:00Z", "2026-08-03T11:00:00Z", 0.8, 0.5),                # probabilities add up to more than 1
        ("2026-08-03T13:00:00Z", "2026-08-03T14:00:00Z", 0.4, 0.2),                # received before it was published
    ])
    res = call("stats.sentiment_score", docs=docs)
    assert res.value["n_documents"] == 2 and res.value["n_docs"] == [2]
    assert res.value["score"] == pytest.approx([(0.6 + 0.2) / 2], abs=1e-12)
    text = " ".join(res.warnings)
    assert "no reception time" in text and "summing above 1" in text and "received before" in text
    assert call("stats.sentiment_score", docs=_docs([(None, "2026-08-03T11:00:00Z", 0.9, 0.0)])).value is None
    assert call("stats.sentiment_score", docs=_docs([("2026-08-03T10:00:00Z", None, 1.5, 0.0)])).value is None
    assert call("stats.sentiment_score", docs=_docs([("2026-08-03T10:00:00Z", None, 0.5, 0.2)]), timezone="Mars/Olympus").value is None
    assert call("stats.sentiment_score", docs=[]).value is None
    assert call("stats.sentiment_score", docs=pd.DataFrame(columns=["received_at", "p_positive", "p_negative"])).value is None
    with pytest.raises(BadInput):
        call("stats.sentiment_score", docs=pd.DataFrame({"received_at": ["2026-08-03T10:00:00Z"]}))
    with pytest.raises(BadInput):
        call("stats.sentiment_score", docs="text")
    with pytest.raises(BadInput):
        call("stats.sentiment_score", docs=[1, 2])
    with pytest.raises(BadInput):
        call("stats.sentiment_score", docs=docs, timezone=5)


def test_sentiment_and_mentions_on_the_fixture(mentions):
    res = call("stats.sentiment_score", docs=mentions)
    assert res.value["n_documents"] == len(mentions) == 634 and len(res.value["dates"]) in (70, 71)
    scores = np.array(res.value["score"])
    assert scores[50:].mean() > scores[:50].mean() + 0.1                                   # the planted shift in the last 20 days
    counts = pd.Series(res.value["n_docs"])
    z = call("stats.mention_volume_zscore", mentions=counts).value
    spike = int(counts.idxmax())
    assert z["z"][spike] > 5.0 and max(v for i, v in enumerate(z["z"]) if v is not None and i != spike) < 4.0     # the planted spike
    day = pd.to_datetime(mentions["received_at"], utc=True).dt.strftime("%Y-%m-%d").value_counts()
    assert res.value["n_docs"] == [int(day[d]) for d in res.value["dates"]]


def test_mention_volume_zscore_ECT_070_hand_value_excludes_the_day_itself():
    counts = pd.Series(list(range(1, 31)) + [100])
    res = call("stats.mention_volume_zscore", mentions=counts).value
    baseline = np.arange(1, 31)
    expected = (100 - baseline.mean()) / baseline.std(ddof=1)                               # 15.5 and 8.803: z = 9.599
    assert res["z"][30] == pytest.approx(expected, abs=1e-10) and res["latest_z"] == pytest.approx(9.5993, abs=1e-3)
    assert res["z"][29] is None or res["z"][29] != res["z"][30]
    assert res["n_valid"] == 1 and res["latest_is_last_day"] is True
    short = call("stats.mention_volume_zscore", mentions=counts, window=10).value
    assert short["z"][30] == pytest.approx((100 - np.arange(21, 31).mean()) / np.arange(21, 31).std(ddof=1), abs=1e-10)
    assert call("stats.mention_volume_zscore", mentions=pd.Series([], dtype=float)).value is None
    assert call("stats.mention_volume_zscore", mentions=pd.Series(range(20))).value is None               # not enough days for the baseline
    assert call("stats.mention_volume_zscore", mentions=pd.Series([5.0] * 40)).value is None             # the baseline has no variation
    assert call("stats.mention_volume_zscore", mentions=pd.Series([-1.0] * 40)).value is None
    assert call("stats.mention_volume_zscore", mentions=counts, window=3).value is None
    nan = counts.astype(float)
    nan.iloc[4] = np.nan
    assert any("dropped" in w for w in call("stats.mention_volume_zscore", mentions=nan, window=20).warnings)
    with pytest.raises(BadInput):
        call("stats.mention_volume_zscore", mentions=counts, window=2.5)
    with pytest.raises(BadInput):
        call("stats.mention_volume_zscore", mentions=counts, window=500000)


def _calls(questions):
    dates = pd.date_range("2023-01-01", periods=len(questions), freq="QS")
    return pd.DataFrame({"call_date": dates, "tone_prepared": [0.5] * len(questions), "tone_questions": questions})


def test_call_tone_pead_ECT_070_hand_values():
    questions = [0.2, 0.1, 0.3, 0.2, 0.1, 0.25, 0.15, 0.2, 0.3, -0.1]
    gap = np.array(questions) - 0.5
    change = np.array([gap[i] - gap[i - 4:i].mean() for i in range(4, 10)])               # against the last 4 calls of the company
    z = change[-1] / change[:-1].std(ddof=1)
    res = call("stats.call_tone_pead", calls=_calls(questions), sue=-1.5).value
    assert res["tone_gap"] == pytest.approx(list(gap), abs=1e-12) and res["change_vs_last4"] == pytest.approx(change[-1], abs=1e-12)
    assert res["tone_change_z"] == pytest.approx(z, abs=1e-10) and res["combined_score"] == pytest.approx(0.5 * -1.5 + 0.5 * z, abs=1e-10)
    assert res["agreement"] == "short" and res["hold_days"] == 10 and res["n_calls"] == 10
    assert call("stats.call_tone_pead", calls=_calls(questions), sue=1.5).value["agreement"] == "conflict"          # good surprise, worse tone
    weighted = call("stats.call_tone_pead", calls=_calls(questions), sue=-1.5, weight_sue=1.0).value
    assert weighted["combined_score"] == pytest.approx(-1.5, abs=1e-12)
    up = call("stats.call_tone_pead", calls=_calls(questions[:-1] + [0.9]), sue=2.0).value
    assert up["agreement"] == "long"
    assert any("backtest" in w for w in call("stats.call_tone_pead", calls=_calls(questions), sue=1.0).warnings)
    assert "ECT-070" in piece_ids("stats.call_tone_pead")


def test_call_tone_pead_ECT_070_empty_nan_and_out_of_domain():
    questions = [0.2, 0.1, 0.3, 0.2, 0.1, 0.25, 0.15, 0.2, 0.3, -0.1]
    calls = _calls(questions)
    assert call("stats.call_tone_pead", calls=calls.iloc[:0], sue=1.0).value is None
    assert call("stats.call_tone_pead", calls=calls.iloc[:5], sue=1.0).value is None
    assert call("stats.call_tone_pead", calls=calls, sue=1.0, hold_days=4).value is None                  # 5 to 20 days only
    assert call("stats.call_tone_pead", calls=calls, sue=1.0, hold_days=21).value is None
    assert call("stats.call_tone_pead", calls=calls, sue=float("nan")).value is None
    assert call("stats.call_tone_pead", calls=calls, sue=1.0, weight_sue=1.5).value is None
    assert call("stats.call_tone_pead", calls=_calls([0.2] * 10), sue=1.0).value is None                  # no variation in the earlier changes
    nan = calls.copy()
    nan.loc[0, "tone_questions"] = np.nan
    assert any("dropped" in w for w in call("stats.call_tone_pead", calls=nan, sue=1.0).warnings)
    with pytest.raises(BadInput):
        call("stats.call_tone_pead", calls=calls.drop(columns="tone_questions"), sue=1.0)
    with pytest.raises(BadInput):
        call("stats.call_tone_pead", calls=calls, sue="1.0")
    with pytest.raises(BadInput):
        call("stats.call_tone_pead", calls=calls, sue=1.0, hold_days=10.5)


def test_sentiment_index_zscore_ECT_070_flags_extremes_without_a_direction():
    values = pd.Series(list(range(52)) + [100])
    res = call("stats.sentiment_index_zscore", values=values).value
    baseline = np.arange(52)
    assert res["latest_z"] == pytest.approx((100 - baseline.mean()) / baseline.std(ddof=1), abs=1e-10) and res["latest_z"] == pytest.approx(4.916, abs=1e-3)
    assert res["flag"] == "extreme_high" and not {"direction", "signal", "side"} & set(res)
    low = call("stats.sentiment_index_zscore", values=pd.Series(list(range(52)) + [-50])).value
    assert low["flag"] == "extreme_low"
    mid = call("stats.sentiment_index_zscore", values=pd.Series(list(range(52)) + [26])).value
    assert mid["flag"] == "normal"
    assert any("untested assumption" in w for w in call("stats.sentiment_index_zscore", values=values).warnings)
    assert call("stats.sentiment_index_zscore", values=pd.Series([], dtype=float)).value is None
    assert call("stats.sentiment_index_zscore", values=pd.Series(range(30))).value is None
    assert call("stats.sentiment_index_zscore", values=pd.Series([3.0] * 60)).value is None
    assert call("stats.sentiment_index_zscore", values=values, window=5).value is None
    nan = pd.Series(list(range(55)) + [100], dtype=float)
    nan.iloc[3] = np.nan
    assert any("dropped" in w for w in call("stats.sentiment_index_zscore", values=nan).warnings)
    with pytest.raises(BadInput):
        call("stats.sentiment_index_zscore", values=values, window=500000)
    with pytest.raises(BadInput):
        call("stats.sentiment_index_zscore", values=values, window=10.5)
    assert rng(1) is not None
