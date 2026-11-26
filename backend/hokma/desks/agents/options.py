"""Caleb, the options analyst (SPEC-019: TA-07 of the plantel A07; skills AFS-005, AFS-027, ECT-031, ECT-042, ECT-098).

Caleb compares implied and realized volatility, reads skew, curvature, the expected move into results and the greeks, and proposes
a structure for the trader to build. Every price and every greek comes from the options module and every realized figure from the
statistics module.

Premium selling is built and measured but closed: the state of the switch comes from the desk (`premium_switch.switch_state`), never
from the answer of the model. While it is closed the argument `premium_selling_open` of the structure matrix is overwritten with
false, a classification of volatility is `uncalibrated` or `unavailable` (never rich or cheap), and an answer that proposes a sale is
rejected. A switch that cannot be read counts as closed.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from hokma.desks.agents.analyst_kit import AnalystAgent, AnalystResult, EvidenceLog, OutputSpec, RunContext
from hokma.desks.core.graph import ChatModel
from hokma.desks.plantel import AnalystReport

CODE = "A07"
KEY = "options"
DEBIT_STRUCTURES = ("long_call", "long_put", "debit_call_spread", "debit_put_spread")
SELLING_STRUCTURES = ("short_put", "short_call", "credit_call_spread", "credit_put_spread", "iron_condor", "short_straddle",
                      "short_strangle", "covered_call", "cash_secured_put", "iron_butterfly")
STRUCTURES = (*DEBIT_STRUCTURES, *SELLING_STRUCTURES)
CLASSIFICATIONS = ("uncalibrated", "unavailable", "rich", "cheap", "fair")
DIRECTIONS = ("bullish", "bearish", "neutral")
VOL_VIEWS = ("rising", "falling", "flat", "unknown")
MATRIX_CALC = "options.structure_matrix"
GREEKS_CALC = "options.position_greeks"
RICHNESS_CALC = "options.vol_richness"
MIN_FACTORS, MAX_FACTORS = 3, 5

_TEXT = {"type": "string", "maxLength": 600}
_NUM = {"type": ["number", "null"]}

SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["summary", "volatility", "skew", "earnings_preview", "greeks", "structure", "data_gaps"],
    "properties": {
        "summary": {"type": "string", "minLength": 1, "maxLength": 1200},
        "volatility": {"type": "object", "required": ["matched_expiration", "implied_over_realized", "classification", "earnings_in_window"], "properties": {
            "matched_expiration": {"type": ["string", "null"]},
            "implied_over_realized": {"type": "array", "items": {"type": "object", "required": ["window_days", "ratio"], "properties": {
                "window_days": {"type": "integer"}, "ratio": _NUM}}},
            "classification": {"type": "string", "enum": list(CLASSIFICATIONS)},
            "earnings_in_window": {"type": ["boolean", "null"]}}},
        "skew": {"type": "object", "required": ["risk_reversal_25d", "butterfly_25d"],
                 "properties": {"risk_reversal_25d": _NUM, "butterfly_25d": _NUM}},
        "earnings_preview": {"type": "object", "required": ["implied_move", "historical_reaction", "scenarios", "factors"], "properties": {
            "implied_move": _NUM, "historical_reaction": _NUM,
            "scenarios": {"type": "array", "items": {"type": "object", "required": ["name", "reaction"], "properties": {
                "name": {"type": "string", "enum": ["bull", "base", "bear"]}, "reaction": _NUM}}},
            "factors": {"type": "array", "items": _TEXT}}},
        "greeks": {"type": "array", "items": {"type": "object", "required": ["label", "delta", "gamma", "vega", "theta"], "properties": {
            "label": _TEXT, "delta": _NUM, "gamma": _NUM, "vega": _NUM, "theta": _NUM}}},
        "structure": {"type": ["object", "null"], "required": ["name", "direction", "vol_view", "legs", "management_plan"], "properties": {
            "name": {"type": "string", "enum": list(STRUCTURES)}, "direction": {"type": "string", "enum": list(DIRECTIONS)},
            "vol_view": {"type": "string", "enum": list(VOL_VIEWS)},
            "legs": {"type": "array", "items": {"type": "object", "required": ["contract_id", "side"], "properties": {
                "contract_id": {"type": "string", "minLength": 3}, "side": {"type": "string", "enum": ["buy", "sell"]}}}},
            "management_plan": {"type": "array", "items": _TEXT}}},
        "data_gaps": {"type": "array", "items": _TEXT},
    },
}


# ---- the switch of premium selling ----------------------------------------------------------------------------------

def default_switch_reader(desk_id: int, underlying: str) -> Any:
    """The state of the switch of the desk for an underlying. Anything that cannot be read is closed."""
    try:
        from hokma.desks.swo.premium_switch import switch_state
    except ImportError:
        return "closed"
    try:
        return switch_state(desk_id, underlying)
    except Exception:                                                       # noqa: BLE001  (a switch that fails stays closed)
        return "closed"


def switch_is_open(desk_id: int, underlying: str, reader: Callable[[int, str], Any] | None = None) -> bool:
    """True only when the desk says the switch is open for this underlying. Any other answer, or a failure, is closed."""
    try:
        state = (reader or default_switch_reader)(desk_id, underlying)
    except Exception:                                                       # noqa: BLE001
        return False
    if isinstance(state, Mapping):
        state = state.get("state")
    else:
        state = getattr(state, "state", state)
    return state == "open"


def _calibration_of(state: Any) -> Mapping[str, Any] | None:
    found = state.get("calibration") if isinstance(state, Mapping) else getattr(state, "calibration", None)
    return dict(found) if isinstance(found, Mapping) else None


def switch_facts(desk_id: int, underlying: str, reader: Callable[[int, str], Any] | None = None) -> dict[str, Any]:
    """What the desk says about premium selling for an underlying: the state of the switch and, only when it is open, the threshold
    the backtest calibrated. The model never writes either of them."""
    open_now = switch_is_open(desk_id, underlying, reader)
    facts: dict[str, Any] = {"premium_switch": {"state": "open" if open_now else "closed", "underlying": underlying.upper()}}
    if open_now:
        try:
            calibration = _calibration_of((reader or default_switch_reader)(desk_id, underlying))
        except Exception:                                                   # noqa: BLE001
            calibration = None
        if calibration:
            facts["calibration"] = calibration
    return facts


def _is_open(ctx: RunContext) -> bool:
    return ((ctx.facts or {}).get("premium_switch") or {}).get("state") == "open"


def structure_guard(ctx: RunContext) -> Callable[[str, dict], dict]:
    """The desk decides the arguments that belong to a control: the switch for the structure matrix and the calibrated threshold
    for the volatility comparison. What the model wrote for them is overwritten."""
    open_now = _is_open(ctx)
    calibration = (ctx.facts or {}).get("calibration") if open_now else None

    def guard(name: str, params: dict) -> dict:
        if name == MATRIX_CALC:
            return {**params, "premium_selling_open": open_now}
        if name == RICHNESS_CALC:
            clean = {k: v for k, v in params.items() if k != "calibration"}
            return {**clean, "calibration": calibration} if calibration else clean
        return params

    return guard


def structure_menu(direction: str, vol_view: str, *, desk_id: int, underlying: str, reader: Callable[[int, str], Any] | None = None) -> Any:
    """The structures the matrix offers for a view, with the switch of the desk deciding whether sales are offered (ECT-098)."""
    from hokma.quant import registry

    registry.load_all()
    return registry.call(MATRIX_CALC, direction=direction, vol_view=vol_view,
                         premium_selling_open=switch_is_open(desk_id, underlying, reader))


# ---- checks ---------------------------------------------------------------------------------------------------------

def _no_sale_while_closed(value: dict, ctx: RunContext, evidence: EvidenceLog) -> list[str]:
    structure = value.get("structure")
    if structure and structure["name"] in SELLING_STRUCTURES and not _is_open(ctx):
        return [f"structure: {structure['name']} sells premium and the sale is closed for this desk and underlying"]
    return []


def _no_rich_or_cheap_without_threshold(value: dict, ctx: RunContext, evidence: EvidenceLog) -> list[str]:
    if value["volatility"]["classification"] in ("rich", "cheap", "fair") and not _is_open(ctx):
        return ["volatility: rich or cheap needs a threshold calibrated in a backtest; the desk has none, so it is uncalibrated"]
    return []


def _classification_is_the_one_of_the_module(value: dict, ctx: RunContext, evidence: EvidenceLog) -> list[str]:
    """The classification is the one `options.vol_richness` returned, and without that call it is unavailable."""
    stated = value["volatility"]["classification"]
    returned = [r["data"]["value"]["classification"] for (name, args), r in zip(evidence.calls, evidence.results)
                if name == "quant_call" and args.get("name") == RICHNESS_CALC and isinstance(r, Mapping) and isinstance(r.get("data"), Mapping)
                and isinstance(r["data"].get("value"), Mapping) and "classification" in r["data"]["value"]]
    if not returned:
        return [] if stated == "unavailable" else [f"volatility: {RICHNESS_CALC} was not read, so the classification is unavailable, not {stated!r}"]
    return [] if stated == returned[-1] else [f"volatility: the module says {returned[-1]!r}, the report says {stated!r}"]


def _factors_of_the_reaction(value: dict, ctx: RunContext, evidence: EvidenceLog) -> list[str]:
    preview = value["earnings_preview"]
    if preview["scenarios"] and not MIN_FACTORS <= len(preview["factors"]) <= MAX_FACTORS:
        return [f"earnings_preview: {MIN_FACTORS} to {MAX_FACTORS} factors that decide the reaction are listed"]
    return []


def _plan_before_entering(value: dict, ctx: RunContext, evidence: EvidenceLog) -> list[str]:
    structure = value.get("structure")
    if structure and not structure["management_plan"]:
        return ["structure: the management plan is written before entering"]
    return []


def _multiplier_from_the_sheet(value: dict, ctx: RunContext, evidence: EvidenceLog) -> list[str]:
    """A multiplier given to the greeks calculation is the one of a contract sheet the contract tool returned in this run."""
    sheets = {float(r["data"]["multiplier"]) for (name, _), r in zip(evidence.calls, evidence.results)
              if name == "contract_get" and isinstance(r, Mapping) and isinstance(r.get("data"), Mapping)
              and isinstance(r["data"].get("multiplier"), (int, float))}
    problems: list[str] = []
    for name, args in evidence.calls:
        if name != "quant_call" or args.get("name") != GREEKS_CALC:
            continue
        given = (args.get("params") or {}).get("multiplier")
        if given is not None and float(given) not in sheets:
            problems.append(f"greeks: the multiplier {given} is not the one of a contract sheet returned by the contract tool")
    return problems


def _classification_matches_the_gaps(value: dict, ctx: RunContext, evidence: EvidenceLog) -> list[str]:
    vol = value["volatility"]
    if vol["classification"] == "unavailable" and not value["data_gaps"]:
        return ["volatility: unavailable has to say in data_gaps what is missing"]
    return []


def render(value: dict) -> AnalystReport:
    vol, skew, prev = value["volatility"], value["skew"], value["earnings_preview"]
    findings = [f"Volatility ({vol['classification']}) for the expiration {vol['matched_expiration'] or 'not matched'}; earnings inside the window: "
                f"{'unknown' if vol['earnings_in_window'] is None else 'yes' if vol['earnings_in_window'] else 'no'}."]
    findings += [f"Implied over realized, {r['window_days']} days: {'unavailable' if r['ratio'] is None else r['ratio']}." for r in vol["implied_over_realized"]]
    findings.append(f"Skew {skew['risk_reversal_25d']}, curvature {skew['butterfly_25d']}.")
    findings.append(f"Implied move {prev['implied_move']}; historical reaction {prev['historical_reaction']}.")
    findings += [f"Scenario {s['name']}: reaction {s['reaction']}." for s in prev["scenarios"]]
    findings += [f"Factor: {f}" for f in prev["factors"]]
    findings += [f"Greeks {g['label']}: delta {g['delta']}, gamma {g['gamma']}, vega {g['vega']}, theta {g['theta']}." for g in value["greeks"]]
    structure = value.get("structure")
    if structure:
        findings.append(f"Structure {structure['name']} for a {structure['direction']} view with volatility {structure['vol_view']}: "
                        + "; ".join(f"{l['side']} {l['contract_id']}" for l in structure["legs"]) + ".")
        findings += [f"Plan: {p}" for p in structure["management_plan"]]
    else:
        findings.append("No structure proposed.")
    return AnalystReport(value["summary"], tuple(findings), tuple(value["data_gaps"]))


SPEC = OutputSpec(SCHEMA, exempt_keys=frozenset({"window_days"}), id_keys=frozenset({"contract_id"}),
                  checks=(_no_sale_while_closed, _no_rich_or_cheap_without_threshold, _classification_is_the_one_of_the_module,
                          _factors_of_the_reaction, _plan_before_entering, _multiplier_from_the_sheet, _classification_matches_the_gaps))
AGENT = AnalystAgent(CODE, KEY, SPEC, render, guard_factory=structure_guard)

system_prompt = AGENT.system_prompt
toolbox = AGENT.toolbox


def run(model: ChatModel, *, desk_id: int, symbol: str, switch_reader: Callable[[int, str], Any] | None = None,
        facts: Mapping[str, Any] | None = None, **kwargs: Any) -> AnalystResult:
    """One report of Caleb. The state of the premium selling switch is read here from the desk and handed to the checks and to the
    guard; nothing the model writes can open it."""
    merged = {**dict(facts or {}), **switch_facts(desk_id, symbol, switch_reader)}
    return AGENT.run(model, desk_id=desk_id, symbol=symbol, facts=merged, **kwargs)
