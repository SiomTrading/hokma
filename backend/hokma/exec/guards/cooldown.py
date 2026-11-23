"""A pause between orders of the same symbol (SPEC-018: OA-45). The clock is in `exec_guard_state`, so it survives a restart,
and only an order that was SENT starts it: a refused one does not reset the clock."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from hokma.db import Database, get_database
from hokma.db.timeutil import from_db_time, utcnow
from hokma.exec.guards import GuardContext, GuardResult, blocked, passed, root_of

GUARD = "cooldown"


def state_key(root: str) -> str:
    return f"{GUARD}:{root}"


def _key(contract_id: str) -> str:
    return root_of(contract_id)[:70]


class CooldownGuard:
    name = GUARD
    applies_to_closing = False

    def check(self, ctx: GuardContext) -> GuardResult:
        seconds = ctx.limits.get("cooldown_seconds")
        if not seconds:
            return passed(self.name, "off")
        worst = 0.0
        for leg in ctx.proposal.legs:
            last = ctx.state.get(state_key(_key(leg.contract_id)))
            if not last:
                continue
            then = from_db_time(str(last["last_sent_at"]))
            elapsed = (ctx.now - then).total_seconds()
            if elapsed < seconds:
                worst = max(worst, seconds - elapsed)
        if worst > 0:
            return blocked(self.name, "cooldown", f"the last order of this symbol was sent less than {int(seconds)} seconds ago",
                           wait_seconds=round(worst, 3), cooldown_seconds=seconds)
        return passed(self.name, cooldown_seconds=seconds)


def load_state(desk_id: int, *, db: Database | None = None) -> dict[str, Any]:
    import json

    with (db or get_database()).read() as u:
        rows = u.session.query("SELECT key, state_json FROM exec_guard_state WHERE desk_id = :d AND guard = :g", {"d": desk_id, "g": GUARD})
    out: dict[str, Any] = {}
    for row in rows:
        try:
            out[row["key"]] = json.loads(row["state_json"])
        except ValueError:
            continue
    return out


def record_sent(desk_id: int, contract_ids: list[str], *, at: datetime | None = None, db: Database | None = None) -> None:
    """Start the clock of every symbol of an order that was sent."""
    import json

    stamp = (at or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S")
    with (db or get_database()).write() as u:
        for cid in dict.fromkeys(contract_ids):
            key = state_key(_key(cid))
            body = json.dumps({"last_sent_at": stamp})
            if u.session.one("SELECT 1 AS x FROM exec_guard_state WHERE desk_id = :d AND guard = :g AND key = :k",
                             {"d": desk_id, "g": GUARD, "k": key}) is None:
                u.session.insert_row("exec_guard_state", {"desk_id": desk_id, "guard": GUARD, "key": key, "state_json": body,
                                                          "updated_at": utcnow()})
            else:
                u.session.update("exec_guard_state", {"desk_id": desk_id, "guard": GUARD, "key": key},
                                 {"state_json": body, "updated_at": utcnow()})
