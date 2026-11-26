"""No openings around a macro event (SPEC-018: CTS-045). A calendar that could not be read is `UNVERIFIED_EVENT_RISK`: the desks
that have an `event_windows` rule do not open while the risk is unverified, because an empty answer from a failed query is not
a calendar without events."""
from __future__ import annotations

from hokma.econ_calendar.windows import active
from hokma.exec.guards import GuardContext, GuardResult, blocked, passed


class EventWindowGuard:
    name = "event_window"
    applies_to_closing = False

    def check(self, ctx: GuardContext) -> GuardResult:
        hits = active(ctx.windows, ctx.now)
        if not hits:
            return passed(self.name, "clear")
        if any(w.unverified for w in hits):
            return blocked(self.name, "unverified_event_risk", "the economic calendar could not be read, so the event risk is unverified")
        first = min(hits, key=lambda w: w.starts_at)
        why = f"{first.event} is inside its window ({first.starts_at:%H:%M} to {first.ends_at:%H:%M} UTC)"
        return GuardResult(self.name, "block", "event_window", why, flags=(f"rule:events:{first.match}",))     # the rule id, so a notice on that event can soften it
