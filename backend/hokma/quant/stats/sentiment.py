"""Sentiment scoring on top of classifier output (ECT-070).

The text classifier runs through the llm service of Hokma (no model is downloaded here): these functions take what it
returns, the probability of a positive and of a negative reading per document, and build the numbers a desk reads.

Rules built in:
* A document counts on the day the system received it, never on its publication time: the publication time was not
  knowable when the document arrived. A document without a reception time is dropped, not dated by its publication.
* Mention volume is compared with the previous days only, so a spike is never part of its own baseline.
* Tone of a call is read against the same company's own earlier calls.
* A sentiment extreme is reported as an extreme. Reading it as a contrarian signal is an untested assumption, so no
  direction is derived from it.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import limits, registry
from hokma.quant.errors import BadInput
from hokma.quant.stats import _common as c
from hokma.quant.types import QuantResult

PIECES = ("ECT-070",)
_SERIES = {"type": "series", "required": True}


def _docs_frame(docs: Any) -> pd.DataFrame:
    if isinstance(docs, pd.DataFrame):
        return docs.copy()
    if isinstance(docs, (list, tuple)):
        if len(docs) == 0:
            return pd.DataFrame(columns=["received_at", "p_positive", "p_negative"])
        if not all(isinstance(d, dict) for d in docs):
            raise BadInput("docs: each item must be a dict")
        return pd.DataFrame(list(docs))
    raise BadInput(f"docs: expected a DataFrame or a list of dicts, got {type(docs).__name__}")


@registry.register("stats.sentiment_score", module="stats",
                   summary="Daily sentiment S = P(positive) - P(negative), averaged over the documents received that day, and the daily document count. Dates use the reception time (received_at), never the publication time. docs need received_at, p_positive and p_negative.",
                   inputs={"docs": {"type": "table or list of dicts", "required": True}, "timezone": {"type": "str", "required": False, "default": "UTC"}},
                   piece_ids=PIECES)
def sentiment_score(docs: Any, timezone: str = "UTC") -> QuantResult:
    name = "stats.sentiment_score"
    if not isinstance(timezone, str):
        raise BadInput("timezone must be text")
    params = {"timezone": timezone}

    def compute() -> Any:
        frame = _docs_frame(docs)
        missing = [col for col in ("received_at", "p_positive", "p_negative") if col not in frame.columns]
        if missing:
            raise BadInput(f"docs: missing column(s) {missing}")
        warns: list[str] = []
        try:
            received = pd.to_datetime(frame["received_at"], utc=True, errors="coerce")
        except (ValueError, TypeError) as exc:
            raise c.Domain(f"received_at could not be read: {exc}") from exc
        no_time = received.isna()
        if no_time.any():
            warns.append(f"{int(no_time.sum())} document(s) dropped: no reception time (the publication time is never used to date a document)")
        try:
            pos = pd.to_numeric(frame["p_positive"], errors="coerce").astype(float)
            neg = pd.to_numeric(frame["p_negative"], errors="coerce").astype(float)
        except (TypeError, ValueError) as exc:
            raise BadInput("p_positive and p_negative must be numeric") from exc
        bad_p = ~((pos >= 0) & (pos <= 1) & (neg >= 0) & (neg <= 1) & (pos + neg <= 1.000001))
        if bad_p.any():
            warns.append(f"{int(bad_p.sum())} document(s) dropped: probabilities outside [0, 1] or summing above 1")
        if "published_at" in frame.columns:
            published = pd.to_datetime(frame["published_at"], utc=True, errors="coerce")
            early = (received < published) & ~no_time & published.notna()
            if early.any():
                warns.append(f"{int(early.sum())} document(s) received before their stated publication time: check the clock of the source")
        keep = ~no_time & ~bad_p
        if not keep.any():
            raise c.Domain("no usable document")
        try:
            local = received[keep].dt.tz_convert(timezone)
        except Exception as exc:  # an unknown timezone name
            raise c.Domain(f"unknown timezone {timezone!r}") from exc
        score = (pos - neg)[keep]
        day = local.dt.strftime("%Y-%m-%d")
        grouped = score.groupby(day.to_numpy())
        daily = pd.DataFrame({"score": grouped.mean(), "n_docs": grouped.size()}).sort_index()
        out = {"dates": list(daily.index), "score": daily["score"].to_numpy(), "n_docs": daily["n_docs"].to_numpy(), "n_documents": int(keep.sum()),
               "latest_score": float(daily["score"].iloc[-1]), "latest_date": str(daily.index[-1]), "date_basis": "received_at"}
        return out, tuple(warns)

    return c.run(name, unit="score", method="p_positive_minus_p_negative", params=params, piece_ids=PIECES, compute=compute)


@registry.register("stats.mention_volume_zscore", module="stats",
                   summary="Abnormal mention volume: z score of each day's mention count against the previous window days (30 by default), the day itself excluded from its own baseline. Needs at least window earlier days.",
                   inputs={"mentions": _SERIES, "window": {"type": "int", "required": False, "default": 30}}, piece_ids=PIECES)
def mention_volume_zscore(mentions: Any, window: int = 30) -> QuantResult:
    name = "stats.mention_volume_zscore"
    limits.check_param("window", window)
    limits.as_int("window", window)
    params = {"window": int(window)}

    def compute() -> Any:
        if window < 5:
            raise c.Domain("window must be at least 5")
        series = c.helpers.as_series(mentions, name="mentions")
        cleaned = c.helpers.clean(series)
        warns = (f"dropped {len(series) - len(cleaned)} NaN or infinite value(s)",) if len(cleaned) != len(series) else ()
        if (cleaned < 0).any():
            raise c.Domain("mention counts cannot be negative")
        if len(cleaned) <= window:
            raise c.Domain(f"need more than {window} days, got {len(cleaned)}")
        prior = cleaned.shift(1)
        mean = prior.rolling(window).mean()
        std = prior.rolling(window).std(ddof=1)
        z = (cleaned - mean) / std.where(std > 0)
        valid = z.dropna()
        if valid.empty:
            raise c.Domain("the baseline has no variation: the z score is undefined")
        return ({"z": z.to_numpy(), "index": [str(i) for i in cleaned.index], "latest_z": float(valid.iloc[-1]), "latest_is_last_day": bool(not math.isnan(z.iloc[-1])),
                 "n_valid": int(len(valid))}, warns)

    return c.run(name, unit="zscore", method="trailing_window_z", params=params, piece_ids=PIECES, compute=compute,
                 inputs=(mentions,) if isinstance(mentions, pd.Series) else ())


@registry.register("stats.call_tone_pead", module="stats",
                   summary="Earnings call tone for post earnings drift: tone gap (questions minus prepared remarks) per call, its change against the last 4 calls, and a combined score with the standardized earnings surprise (SUE). The hold period must be 5 to 20 days. It is a signal input, never an order.",
                   inputs={"calls": {"type": "table or list of dicts {call_date, tone_prepared, tone_questions}", "required": True},
                           "sue": {"type": "float", "required": True}, "hold_days": {"type": "int", "required": False, "default": 10},
                           "weight_sue": {"type": "float", "required": False, "default": 0.5}}, piece_ids=PIECES)
def call_tone_pead(calls: Any, sue: float, hold_days: int = 10, weight_sue: float = 0.5) -> QuantResult:
    name = "stats.call_tone_pead"
    limits.as_int("hold_days", hold_days)
    params = {"sue": sue, "hold_days": int(hold_days), "weight_sue": weight_sue}

    def compute() -> Any:
        s = c.number("sue", sue)
        w = c.number("weight_sue", weight_sue, low=0.0, high=1.0)
        if not 5 <= hold_days <= 20:
            raise c.Domain("hold_days must be between 5 and 20 for post earnings drift")
        frame = _docs_frame(calls)
        missing = [col for col in ("call_date", "tone_prepared", "tone_questions") if col not in frame.columns]
        if missing:
            raise BadInput(f"calls: missing column(s) {missing}")
        warns: list[str] = []
        frame["call_date"] = pd.to_datetime(frame["call_date"], errors="coerce")
        for col in ("tone_prepared", "tone_questions"):
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
        cleaned = frame.replace([np.inf, -np.inf], np.nan).dropna(subset=["call_date", "tone_prepared", "tone_questions"]).sort_values("call_date")
        if len(cleaned) != len(frame):
            warns.append(f"{len(frame) - len(cleaned)} call(s) dropped: missing date or tone")
        if len(cleaned) < 6:
            raise c.Domain(f"need at least 6 calls (the latest and 4 earlier for the change, plus history for the scale), got {len(cleaned)}")
        gap = (cleaned["tone_questions"] - cleaned["tone_prepared"]).to_numpy()
        change = np.full(len(gap), np.nan)
        for i in range(4, len(gap)):
            change[i] = gap[i] - float(np.mean(gap[i - 4:i]))
        hist = change[4:-1]
        hist = hist[np.isfinite(hist)]
        latest = float(change[-1])
        if len(hist) >= 2 and float(np.std(hist, ddof=1)) > 0:
            tone_z = latest / float(np.std(hist, ddof=1))
        else:
            raise c.Domain("not enough earlier tone changes to scale the latest one")
        combined = w * s + (1.0 - w) * tone_z
        if s > 0 and tone_z > 0:
            direction = "long"
        elif s < 0 and tone_z < 0:
            direction = "short"
        else:
            direction = "conflict"
        warns.append("equal weights by default: calibrate the weights and the hold period in backtest before trading on the combined score")
        return ({"tone_gap": gap, "tone_gap_latest": float(gap[-1]), "change_vs_last4": latest, "tone_change_z": tone_z, "sue": s, "combined_score": combined,
                 "agreement": direction, "hold_days": int(hold_days), "n_calls": int(len(gap)), "call_dates": [d.date().isoformat() for d in cleaned["call_date"]]}, tuple(warns))

    return c.run(name, unit="score", method="tone_gap_and_sue", params=params, piece_ids=PIECES, compute=compute)


@registry.register("stats.sentiment_index_zscore", module="stats",
                   summary="Z score of a market sentiment index (survey, put/call, fear and greed) against its previous window observations (52 weekly values by default), with an extreme flag at 2. The function derives no trade direction from an extreme.",
                   inputs={"values": _SERIES, "window": {"type": "int", "required": False, "default": 52}}, piece_ids=PIECES)
def sentiment_index_zscore(values: Any, window: int = 52) -> QuantResult:
    name = "stats.sentiment_index_zscore"
    limits.check_param("window", window)
    limits.as_int("window", window)
    params = {"window": int(window)}

    def compute() -> Any:
        if window < 10:
            raise c.Domain("window must be at least 10")
        series = c.helpers.as_series(values, name="values")
        cleaned = c.helpers.clean(series)
        warns = (f"dropped {len(series) - len(cleaned)} NaN or infinite value(s)",) if len(cleaned) != len(series) else ()
        if len(cleaned) <= window:
            raise c.Domain(f"need more than {window} observations, got {len(cleaned)}")
        prior = cleaned.shift(1)
        std = prior.rolling(window).std(ddof=1)
        z = (cleaned - prior.rolling(window).mean()) / std.where(std > 0)
        valid = z.dropna()
        if valid.empty:
            raise c.Domain("the baseline has no variation: the z score is undefined")
        latest = float(valid.iloc[-1])
        flag = "extreme_high" if latest >= 2.0 else "extreme_low" if latest <= -2.0 else "normal"
        return ({"z": z.to_numpy(), "latest_z": latest, "flag": flag, "index": [str(i) for i in cleaned.index]},
                warns + ("a contrarian reading of an extreme is an untested assumption: it is not turned into a direction here",))

    return c.run(name, unit="zscore", method="trailing_window_z", params=params, piece_ids=PIECES, compute=compute,
                 inputs=(values,) if isinstance(values, pd.Series) else ())
