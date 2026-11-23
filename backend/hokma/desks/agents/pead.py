"""Drew, the earnings drift model of the PEAD variant of a stock options desk (SPEC-019: B8, A23).

The own method of the desk, written as code (an `AlphaModel` of the signal contract, no model call decides the view):

1. The fiscal quarter is read from the text of the name of the call and never from the calendar date (AFS-028). If the name does not
   carry it, the view is an abstention.
2. A published result is needed at the cut-off. A reported figure above the consensus is a BEAT, under it a MISS. Without a published
   result there is no view (AH-10).
3. Window mode: for four sessions from the first session that trades on the results, BEAT gives a long view, MISS a short view and
   equal figures a neutral view. The confidence follows the size of the surprise on the anchored scale and never goes above 69 while the
   weights of the screens are not validated by a backtest (H4). An EXPIRED weekly setup after a beat makes the view neutral.
4. Setup mode: after the four sessions only a beat whose weekly setup (CTS-001: gap, red pullback week, breakout on a closed weekly
   bar, five weeks) is SIGNAL_READY or BREAKOUT keeps a long view; anything else is an abstention. The screen reads closed candles only
   and a live price is never a breakout.
5. The size of the surprise is the only input of the confidence tier; nothing here says how much to commit or where to leave.
6. The reaction score (CTS-002) is context and always carries `weights_unvalidated`. The episodic pivot (CTS-017) is context with a
   catalyst that stays unverified until a tool confirms it. The news reaction test (CTS-037) is context and never a signal.

Every calculation of the screens goes through `registry.call`. The strategy (AH-33) runs on a SWO desk of the PEAD variant with its
mandate: the seat has to be active, the asset class has to be in the mandate (the same method adapts to futures contracts), and the
views of the strategy are mixed with the weights the mandate gives.

Drew sends no order and has no tool that does. The signal has no field for a price, a size or a time to act.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np

from hokma.db import Database, get_database
from hokma.desks.agents import analyst_kit as kit
from hokma.desks.agents import investors as inv
from hokma.desks.core.hashing import content_hash
from hokma.desks.core.prompts import PromptComposer
from hokma.desks.core.signal import Criterion, Signal, SignalContext
from hokma.desks.core.tool_audit import ToolAuditLog
from hokma.desks.core.tools import Toolbox
from hokma.desks.plantel import SEATS
from hokma.quant import registry

CODE = "A23"
LINE = "SWO"
VARIANT = "pead"
STRATEGY = "earnings-drift"
WINDOW_SESSIONS = 4                       # sessions the drift view lives, counted from the first session that trades on the result
PEAD_CONFIDENCE_CAP = 69                  # the weights and tiers are not validated yet: H4 calibrates them
TIERS: tuple[tuple[float, int], ...] = ((0.10, 65), (0.05, 55), (0.0, 40))   # |surprise| over the consensus -> confidence (not calibrated)
NEUTRAL_CONFIDENCE = 40
SIGNAL_READY_CAP = 55                     # the pullback week closed but the breakout has not happened: a lower ceiling
MARKET_ZONE = ZoneInfo("America/New_York")
TIMINGS = ("BMO", "AMC", "intraday")
ASSET_CLASSES = ("stock", "future")
SUPPORTING_STATES = ("SIGNAL_READY", "BREAKOUT")
SCREEN = "indicators.screens.pead"
REACTION = "indicators.screens.earnings_reaction"
EPISODIC = "indicators.screens.episodic_pivot"
FAILURE = "stats.reaction_failure"
QUARTER = "fundamentals.fiscal_quarter"
_EQUAL = 1e-12


class PeadError(ValueError):
    """An input of the method or a desk that cannot run it."""


@dataclass(frozen=True)
class EarningsEvent:
    """One report of results, as the calendar and the data tools give it. The quarter is read from `call_name`, never from a date."""

    call_name: str
    published_at: datetime                     # when the result became public (aware)
    timing: str | None                         # BMO, AMC or intraday
    actual_eps: float | None
    estimate_eps: float | None                 # consensus before the release

    def __post_init__(self) -> None:
        if not isinstance(self.call_name, str) or not self.call_name.strip():
            raise PeadError("an earnings event needs the name of the call")
        if not isinstance(self.published_at, datetime) or self.published_at.tzinfo is None:
            raise PeadError("published_at must be an aware datetime")
        if self.timing is not None and self.timing not in TIMINGS:
            raise PeadError(f"timing must be one of {TIMINGS}")
        for label, value in (("actual_eps", self.actual_eps), ("estimate_eps", self.estimate_eps)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value))):
                raise PeadError(f"{label} must be a finite number or None")

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "EarningsEvent":
        allowed = {"call_name", "published_at", "timing", "actual_eps", "estimate_eps"}
        if extra := set(data) - allowed:
            raise PeadError(f"unknown field(s) of an earnings event: {sorted(extra)}")
        published = data["published_at"]
        if isinstance(published, str):
            published = datetime.fromisoformat(published.replace("Z", "+00:00"))
        return EarningsEvent(str(data["call_name"]), published, data.get("timing"), data.get("actual_eps"), data.get("estimate_eps"))


@dataclass(frozen=True)
class PeadInputs:
    """The real data a run binds for Drew."""

    event: EarningsEvent | None
    bars: Any = None                                   # daily bars of the symbol (or contract)
    asset_class: str = "stock"
    catalyst: str | None = None                        # text of the catalyst of an episodic pivot, unverified
    reaction_failure: Mapping[str, Any] | None = None  # registry arguments of stats.reaction_failure (returns, events, ...)

    def __post_init__(self) -> None:
        if self.asset_class not in ASSET_CLASSES:
            raise PeadError(f"asset_class must be one of {ASSET_CLASSES}")


# ---- the calendar of the window -------------------------------------------------------------------------------------

def reaction_session(event: EarningsEvent) -> date:
    """The first session that trades on the results: the day of the release for BMO or in session, the next session for AMC.
    Sessions are weekdays (the install carries no holiday calendar, and the result says so)."""
    local = event.published_at.astimezone(MARKET_ZONE).date()
    step = 1 if event.timing == "AMC" else 0
    return np.busday_offset(np.datetime64(local), step, roll="forward").astype(object)


def session_index(event: EarningsEvent, as_of: datetime) -> int:
    """Sessions between the first session on the results and the day of the cut-off: 0 is the reaction session, negative is before it."""
    first = np.datetime64(reaction_session(event))
    today = np.datetime64(as_of.astimezone(MARKET_ZONE).date())
    if today >= first:
        return int(np.busday_count(first, today))
    return -int(np.busday_count(today, first))


def outcome(actual: float, estimate: float) -> str:
    gap = actual - estimate
    return "BEAT" if gap > _EQUAL else "MISS" if gap < -_EQUAL else "IN_LINE"


def tier_confidence(surprise_pct: float | None) -> int:
    """Confidence by the size of the surprise on the anchored scale. Without a percentage (estimate of zero) the lowest tier applies."""
    if surprise_pct is None:
        return TIERS[-1][1]
    size = abs(surprise_pct)
    return next(conf for floor, conf in TIERS if size >= floor)


# ---- the facts of one run -----------------------------------------------------------------------------------------

def _ok(result: Any) -> dict[str, Any] | None:
    return dict(result.value) if getattr(result, "value", None) is not None and isinstance(result.value, dict) else None


def _call(name: str, **kwargs: Any) -> Any:
    registry.load_all()
    try:
        return registry.call(name, **kwargs)
    except Exception as exc:                                                    # a failed call is a gap, never a neutral view
        return exc


def _closed(bars: Any, as_of: datetime) -> Any:
    return inv._closed_days_only(bars, as_of)


def abstention(desk_id: int, symbol: str, as_of: datetime, missing: Sequence[str], reason: str | None = None) -> Signal:
    text = reason or "The inputs of the drift method are not complete. No view is formed."
    return Signal.abstain(CODE, desk_id, LINE, symbol, as_of, "days", missing, text,
                          content_hash({"source": CODE, "symbol": symbol, "as_of": as_of.isoformat(), "missing": list(missing)}))


def build_context(*, desk_id: int, symbol: str, as_of: datetime, inputs: PeadInputs) -> SignalContext:
    """The facts and the criteria of the method for one symbol at a cut-off, with every calculation taken from the registry."""
    symbol = symbol.strip().upper()
    gaps: list[str] = []
    warnings: list[str] = []
    facts: dict[str, Any] = {"method": "earnings_drift", "asset_class": inputs.asset_class, "window_sessions": WINDOW_SESSIONS,
                             "calendar": "weekdays only: the install has no holiday calendar", "confidence_cap": PEAD_CONFIDENCE_CAP}
    criteria: list[Criterion] = []
    event = inputs.event
    if event is None:
        gaps.append("earnings_event")
    else:
        quarter = _call(QUARTER, call_name=event.call_name)
        parsed = None if isinstance(quarter, Exception) else _ok(quarter)
        if parsed is None:
            gaps.append("fiscal_quarter")
            warnings.extend(["fiscal_quarter_not_read_from_the_call_name"] if isinstance(quarter, Exception) else list(quarter.warnings))
        else:
            facts["fiscal_quarter"] = parsed["label"]
        if event.published_at > as_of:
            gaps.append("published_result")
        elif event.actual_eps is None or event.estimate_eps is None:
            gaps.append("published_result")
        if event.timing is None:
            gaps.append("timing")
        if "published_result" not in gaps and "timing" not in gaps:
            actual, estimate = float(event.actual_eps), float(event.estimate_eps)
            surprise = actual - estimate
            pct = None if abs(estimate) < _EQUAL else surprise / abs(estimate)
            idx = session_index(event, as_of)
            facts.update({"call_name": event.call_name, "published_at": event.published_at.astimezone(timezone.utc).isoformat(),
                          "timing": event.timing, "reaction_session": reaction_session(event).isoformat(), "actual_eps": actual,
                          "estimate_eps": estimate, "surprise": surprise, "surprise_pct": pct, "outcome": outcome(actual, estimate),
                          "session_index": idx})
            if pct is None:
                warnings.append("estimate_is_zero: the surprise has no percentage and the lowest tier applies")
            criteria.append(Criterion("eps_surprise", surprise, 0.0, surprise > _EQUAL, "earnings_event"))
            facts["mode"] = "window" if idx <= WINDOW_SESSIONS - 1 else "setup"
            criteria.append(Criterion("sessions_since_reaction_session", float(idx), float(WINDOW_SESSIONS - 1), idx <= WINDOW_SESSIONS - 1,
                                      "earnings_event"))
    bars = _closed(inputs.bars, as_of) if inputs.bars is not None else None
    if event is not None and bars is not None and "published_result" not in gaps and "timing" not in gaps:
        day = facts["reaction_session"]
        stamp = as_of.astimezone(timezone.utc).isoformat()
        screen = _call(SCREEN, bars=bars, earnings_dates=[day], as_of=stamp)
        shown = None if isinstance(screen, Exception) else _ok(screen)
        if shown is not None:
            facts["weekly_setup"] = {"state": shown.get("state"), "reason": shown.get("reason"), "gap": shown.get("gap")}
            if shown.get("state") is not None:
                supports = shown["state"] in SUPPORTING_STATES
                criteria.append(Criterion("weekly_setup_supports", 1.0 if supports else 0.0, None, supports, SCREEN))
        else:
            warnings.append("weekly_setup_not_available: " + (str(screen)[:120] if isinstance(screen, Exception) else "; ".join(screen.warnings)[:200]))
        react = _call(REACTION, bars=bars, reaction_date=day, as_of=stamp)
        score = None if isinstance(react, Exception) else _ok(react)
        if score is None:
            warnings.append("reaction_score_not_available: " + (str(react)[:120] if isinstance(react, Exception) else "; ".join(react.warnings)[:200]))
        else:
            facts["reaction_score"] = {k: score.get(k) for k in ("score", "factors") if k in score}
            warn = [w for w in react.warnings]
            facts["reaction_score"]["warnings"] = warn
            value = score.get("score")
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                criteria.append(Criterion("reaction_score", float(value), None, None, REACTION))
        if inputs.catalyst:
            pivot = _call(EPISODIC, bars=bars, catalyst=inputs.catalyst, reaction_date=day, catalyst_verified=False, as_of=stamp)
            shown_pivot = None if isinstance(pivot, Exception) else _ok(pivot)
            if shown_pivot is not None:
                facts["episodic_pivot"] = {"state": shown_pivot.get("state"), "catalyst_verified": False,
                                           "pead_handoff": shown_pivot.get("pead_handoff")}
    if inputs.reaction_failure is not None and facts.get("outcome") in ("BEAT", "MISS"):
        direction = 1 if facts["outcome"] == "BEAT" else -1
        fail = _call(FAILURE, **{**dict(inputs.reaction_failure), "direction": direction})
        shown_fail = None if isinstance(fail, Exception) else _ok(fail)
        if shown_fail is not None:
            facts["reaction_failure"] = {"verdict": shown_fail.get("verdict"), "n_groups": shown_fail.get("n_groups"), "role": "context only"}
    facts["warnings"] = warnings
    facts["gaps"] = list(dict.fromkeys(gaps))
    inputs_hash = content_hash({"symbol": symbol, "as_of": as_of.isoformat(), "facts": facts,
                                "bars": None if bars is None else bars})
    return SignalContext(source=CODE, desk_id=desk_id, line=LINE, symbol=symbol, as_of=as_of, horizon="days", snapshot_hash=inputs_hash,
                         facts=facts, criteria=tuple(criteria), required=("eps_surprise", "sessions_since_reaction_session"),
                         confidence_cap=PEAD_CONFIDENCE_CAP)


# ---- the model (AH-10) ----------------------------------------------------------------------------------------------

class PeadModel:
    """The earnings drift model as an `AlphaModel`: deterministic, no model call, one view per context."""

    code = CODE

    def signal(self, ctx: SignalContext) -> Signal:
        gaps = tuple(ctx.facts.get("gaps", ()))
        if gaps or ctx.missing_required():
            return ctx.abstain(self._abstain_reason(gaps), gaps or ctx.missing_required())
        facts = ctx.facts
        result = facts["outcome"]
        pct = facts["surprise_pct"]
        quarter = facts["fiscal_quarter"]
        setup = (facts.get("weekly_setup") or {}).get("state")
        idx = facts["session_index"]
        size = "with no percentage" if pct is None else f"by {abs(pct) * 100:.1f} percent"
        first = {"BEAT": f"Reported earnings per share beat the consensus {size} in {quarter}.",
                 "MISS": f"Reported earnings per share missed the consensus {size} in {quarter}.",
                 "IN_LINE": f"Reported earnings per share matched the consensus in {quarter}."}[result]
        if facts["mode"] == "setup":
            # The four sessions are over. Only a beat whose weekly setup is alive keeps a (long) view; everything else has no view.
            if result != "BEAT" or setup not in SUPPORTING_STATES:
                return ctx.abstain(self._abstain_reason(("drift_window_over",)), ("drift_window_over",))
            confidence = min(tier_confidence(pct), PEAD_CONFIDENCE_CAP if setup == "BREAKOUT" else SIGNAL_READY_CAP)
            parts = [first, f"The window of {WINDOW_SESSIONS} sessions is over, session {idx + 1} since the results.",
                     f"The weekly setup reads {setup} inside its window of five weeks.",
                     "Weights of the screens are not validated yet, so the confidence stays capped."]
            return self._signal(ctx, 1, confidence, parts)
        sign = {"BEAT": 1, "MISS": -1, "IN_LINE": 0}[result]
        confidence = tier_confidence(pct)
        voided = result == "BEAT" and setup == "EXPIRED"
        if result == "IN_LINE" or voided:
            sign, confidence = 0, NEUTRAL_CONFIDENCE
        confidence = min(confidence, PEAD_CONFIDENCE_CAP)
        shown = max(idx, 0) + 1
        window = (f"The drift window of {WINDOW_SESSIONS} sessions has not started." if idx < 0
                  else f"The drift window of {WINDOW_SESSIONS} sessions is open, session {shown} of {WINDOW_SESSIONS}.")
        parts = [first, window]
        if setup is not None:
            parts.append("The weekly setup is expired, so the drift view is neutral." if voided else f"The weekly setup reads {setup}.")
        parts.append("Weights of the screens are not validated yet, so the confidence stays capped.")
        return self._signal(ctx, sign, confidence, parts)

    @staticmethod
    def _signal(ctx: SignalContext, sign: int, confidence: int, parts: Sequence[str]) -> Signal:
        return Signal(source=CODE, desk_id=ctx.desk_id, line=ctx.line, symbol=ctx.symbol, as_of=ctx.as_of, horizon=ctx.horizon,
                      value=sign * confidence / 100.0, confidence=confidence, abstained=False, criteria=ctx.criteria,
                      data_missing=(), reason=" ".join(parts), snapshot_hash=ctx.snapshot_hash,
                      confidence_cap=ctx.confidence_cap, required=ctx.required, snapshot_at=None)

    @staticmethod
    def _abstain_reason(gaps: Sequence[str]) -> str:
        if "drift_window_over" in gaps:
            return "The drift window of four sessions is over and the weekly setup does not hold. No view is formed."
        if "published_result" in gaps:
            return "No published result exists at the cut-off. No view is formed."
        if "fiscal_quarter" in gaps:
            return "The fiscal quarter is not in the name of the call. No view is formed."
        return "An input of the drift method is missing. No view is formed."


def signal_for(*, desk_id: int, symbol: str, as_of: datetime, inputs: PeadInputs) -> Signal:
    """Drew's view of one symbol: the context from the registry and the model on top."""
    return PeadModel().signal(build_context(desk_id=desk_id, symbol=symbol, as_of=as_of, inputs=inputs))


# ---- the strategy on a SWO desk with its mandate (AH-33) -----------------------------------------------------------

@dataclass(frozen=True)
class PeadRun:
    signal: Signal
    signal_id: int | None
    filter: inv.FilterResult
    asset_class: str


def _desk_setup(database: Database, desk_id: int) -> tuple[Any, str | None, str | None, bool]:
    from hokma.desks.agents.manager import Mandate

    with database.read() as u:
        profile = u.session.one("SELECT line, variant FROM desk_profile WHERE desk_id = :d", {"d": desk_id})
        row = u.session.one("SELECT spec_json FROM desk_mandates WHERE desk_id = :d", {"d": desk_id})
        seat = u.session.one("SELECT active FROM desk_seats WHERE desk_id = :d AND seat_code = :s", {"d": desk_id, "s": CODE})
    mandate = Mandate.from_json(row["spec_json"]) if row else None
    return mandate, (profile or {}).get("line"), (profile or {}).get("variant"), bool(seat and seat["active"])


def run_pead_strategy(desk_id: int, *, symbol: str, as_of: datetime, inputs: PeadInputs, others: Sequence[Signal] = (),
                      run_id: int | None = None, db: Database | None = None, persist: bool = True,
                      reject_below: float = inv.DEFAULT_REJECT_BELOW) -> PeadRun:
    """The earnings drift strategy on its desk: Drew's view, the views of the other models of the strategy and their mix.

    The desk has to be a SWO desk of the PEAD variant with Drew on, and the asset class has to be in the mandate: the check reads the
    desk, never the caller. `others` are the signals of the other models the mandate names (Ray, Owen, Gabe)."""
    database = db or get_database()
    mandate, line, variant, active = _desk_setup(database, desk_id)
    if line != LINE or variant != VARIANT or mandate is None:
        raise PeadError("the earnings drift strategy runs only on a SWO desk of the pead variant with a mandate")
    if not active:
        raise PeadError("seat A23 is not active on this desk")
    if inputs.asset_class not in mandate.asset_classes:
        raise PeadError(f"the mandate of this desk does not allow the asset class {inputs.asset_class}")
    strategy = next((s for s in mandate.strategies if s.name == STRATEGY), None) or mandate.strategies[0]
    allowed = {m.name for m in strategy.models}
    if CODE not in allowed:
        raise PeadError("the strategy of the mandate does not name A23")
    signal = signal_for(desk_id=desk_id, symbol=symbol, as_of=as_of, inputs=inputs)
    signal_id = inv.persist_signal(signal, run_id=run_id, db=database) if persist else None
    views = [signal, *(s for s in others if s.source in allowed and s.source != CODE)]
    history = inv.load_signal_history(desk_id, as_of, db=database)
    result = inv.company_filter(views, history=history if len(history) else None,
                                weights={m.name: float(m.weight) for m in strategy.models}, veto=inv.veto_enabled(mandate),
                                reject_below=reject_below)
    return PeadRun(signal, signal_id, result, inputs.asset_class)


# ---- the prompt and the tools of the seat (the agent file with its skills) ------------------------------------------

def system_prompt(*, line: str, desk_id: int, as_of: datetime, kit_root: Path | None = None, composer: PromptComposer | None = None,
                  mode: str = "live", language: str | None = None) -> str:
    """The prompt of Drew when he explains his view: the profile of his file and, one by one, the skills his file declares.
    Asked for a line other than SWO it is refused, so no skill of another line reaches it."""
    seat_file = kit.load_seat_file(CODE, kit_root)
    if line not in seat_file.seat.lines or line not in seat_file.lines:
        raise kit.AnalystKitError(f"{seat_file.seat.name} does not work on a {line} desk")
    comp = composer or PromptComposer()
    base = comp.compose(role="investor", line=line, desk_id=desk_id, as_of=as_of, mode=mode, language=language,
                        lacks="the candle in progress, a live price as a base for a view, and any holiday calendar", persona=seat_file.body)
    blocks = [base.rstrip("\n"), *(kit.skill_block(s) for s in seat_file.skills), f"## Numbers\n{kit.NUMBERS_RULE}"]
    return "\n\n".join(blocks) + "\n"


def toolbox(*, audit: ToolAuditLog | None = None, handlers: Mapping[str, Any] | None = None) -> Toolbox:
    """The tools of the seat: quant_call over the indicators and stats modules and data_filings. No tool sends an order."""
    return kit.build_toolbox(CODE, handlers=handlers, audit=audit)


def seat_tools() -> tuple[str, ...]:
    return SEATS[CODE].tools
