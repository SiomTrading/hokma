"""Numbers for the home screen: owner and fund, engine, active agents, projects and the weekly allowance.
Everything comes from the database; before it exists the screen gets `ready: false` and no numbers."""
from hokma.db import current_version, get_database


def collect() -> dict:
    if current_version() == 0:
        return {"ready": False}
    with get_database().read() as unit:
        profile = unit.profile.get_or_default()
        week = unit.usage.week()
        return {
            "ready": True,
            "owner_name": profile.owner_name,
            "fund_name": profile.fund_name,
            "engine": profile.engine,
            "claude_status": profile.claude_status,
            "agents": {"active": unit.agents.count("active"), "total": unit.agents.count()},
            "projects": len(unit.projects.list()),
            "usage": {"used": week.used, "cap": week.cap, "pct": week.pct},
        }
