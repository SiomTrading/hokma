"""The freeze at the door (SPEC-026 v2, sections 4.4 b and 4.5). It reads what the loader worked out from the LAST reading of the broker (`live:evals`, `live:stop`; the
reading itself is `hokma.broker.pnl.latest_pnl`) and blocks an OPENING in two cases, nothing else:

* the loss of the day (or the drawdown) the broker reported already reached the cap of a rule in restriction: `day_loss_reached` (`drawdown_reached`), with the value, the
  hour of the reading and the cap in the message;
* the rule cannot be checked because the broker gave no data (no reading, or no start of the session that the owner declared either): `broker_data_missing`. The guard
  fails closed: a missing number is never read as zero.

The monitor (hokma.exec.live) is what closes the day in the event that reaches the cap; this guard is the door of the same data, so an order that arrives between two
events meets the same answer. Hokma values nothing here: it compares what the broker said with what the rule allows. A closing order never reaches it."""
from __future__ import annotations

from hokma.exec.guards import GuardContext, GuardResult, blocked, passed
from hokma.i18n import t


class LiveLossGuard:
    name = "live_loss"
    applies_to_closing = False

    def check(self, ctx: GuardContext) -> GuardResult:
        stop = ctx.state.get("live:stop")
        if stop:
            return blocked(self.name, "desk_stopped", f"this desk is stopped for the day by {stop.get('rule_id')} (loss {stop.get('loss')})")
        numbers: dict[str, object] = {}
        reached: GuardResult | None = None
        for ev in ctx.state.get("live:evals") or ():
            rule = ev.rule
            what = t("live.freeze.what.drawdown") if rule.metric == "drawdown" else t("live.freeze.what.loss")
            if ev.status == "unmeasured":
                return blocked(self.name, "broker_data_missing", t("live.data_missing", what=what.lower(), rule=rule.rule_id, why=_why(ev.why)))
            if ev.status == "reached":
                reached = reached or blocked(self.name, "drawdown_reached" if rule.metric == "drawdown" else "day_loss_reached",
                                             t("live.loss_reached", what=what, value=f"{ev.value:,.2f}", at=ev.at or "?", cap=f"{ev.cap:,.2f}", rule=rule.rule_id),
                                             value=ev.value, cap=ev.cap)
            elif ev.value is not None:
                numbers[f"{rule.metric}_{rule.scope}"] = ev.value
        return reached or passed(self.name, **numbers)


def _why(code: str) -> str:
    """The sentence for the `why` of an unmeasured evaluation (the constants NO_DATA, START_UNKNOWN and SHARED of hokma.exec.live). One literal t(...) per message, for the catalog check."""
    if code == "no_broker_data":
        return t("live.why.no_data")
    if code == "start_unknown":
        return t("live.why.start_unknown")
    if code == "shared_account":
        return t("live.why.shared_account")
    return t("live.why.other")
