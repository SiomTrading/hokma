"""Fail closed when the emergency switch stopped (SPEC-018: ECT-026). A desk with emergency thresholds that the monitor has read before, and not
for two intervals, does not open: a monitor that died silently would otherwise leave the desk with no emergency switch at all. A closing order passes."""
from __future__ import annotations

from hokma.db.timeutil import from_db_time
from hokma.exec.guards import GuardContext, GuardResult, blocked, passed


class MonitorGuard:
    name = "monitor"
    applies_to_closing = False

    def check(self, ctx: GuardContext) -> GuardResult:
        from hokma.exec import killswitch

        if not killswitch.thresholds_of(dict(ctx.limits)):
            return passed(self.name, "no_thresholds")
        beat = ctx.state.get("killswitch:heartbeat")
        if not beat:
            return blocked(self.name, "monitor_never_measured", "the emergency switch has not measured this desk yet, so new openings are blocked")
        age = (ctx.now - from_db_time(str(beat["at"]))).total_seconds()
        if age > 2 * killswitch.INTERVAL_SECONDS:
            return blocked(self.name, "monitor_stale", "the emergency switch has not read this desk for two intervals, so new openings are blocked", age_seconds=age)
        return passed(self.name, age_seconds=age)
