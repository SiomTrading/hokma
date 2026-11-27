"""One agent per seat of a desk (task 3427): the desks made before 1.2.6 move to the shared agent of the kit.

Until 1.2.5 a SWO desk had its own copy of every seat agent, named after the seat and the desk (`market-analyst-12`). Nothing in those copies was different from
desk to desk (only the name, the slug and the model of the frontmatter), and what is per desk is state that lives in rows keyed by the desk. So each seat of an
existing desk is moved to the agent of the kit (`market-analyst`):

- the desk keeps its model, its switch and its role (`desk_agents`, `desk_seats`), its conversations and their messages, the routines of the desk and its open tasks;
- the history that cannot be rewritten (the journal, the guard log, the approvals, the finished tasks) keeps the old slug, so the old agent row stays, off and without a
  file, and the Agents screen does not list it;
- the copy of the old agent (the file in `agents/`, the loaded copy and the copy in the folder of the desk) is removed only when the owner never changed it. A seat whose
  copy was changed stays on its own agent, exactly as it was, and `desk_agent_slug` reads that from `desk_seats`.

It is idempotent and never raises: a seat that cannot be moved now is left as it is and tried again at the next start.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from hokma.core.config import hokma_home
from hokma.db.backend import Session
from hokma.db.repos.agents import AgentsRepo

log = logging.getLogger(__name__)

_OWN_SLUG = re.compile(r"^(?P<seat>[a-z][a-z0-9-]*)-(?P<desk>\d+)$")
_PER_DESK_KEYS = ("name", "slug", "display_name", "model", "hokma-tier")      # what the copy of a desk set; the rest of the file is the one of the kit
_OPEN_TASK = ("triage", "todo", "ready", "blocked", "review", "awaiting_approval")


def _body(text: str) -> str:
    """The file without the lines a desk copy changes (name, slug, display_name, model and tier) and without trailing blanks."""
    kept = []
    in_front, seen = False, 0
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.strip() == "---" and seen < 2:
            seen += 1
            in_front = seen == 1
            kept.append(line)
            continue
        if in_front and any(line.startswith(f"{key}:") for key in _PER_DESK_KEYS):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _read(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _kit_text(seat_slug: str) -> str | None:
    from hokma.db.seeds import _substitute, kit_agents_dir

    raw = _read(kit_agents_dir() / f"{seat_slug}.md")
    return None if raw is None else _substitute(raw)


def _unchanged(path: Path, kit: str) -> bool:
    """True when the file is not there or is the kit file with only the per desk lines different: nothing of the owner is in it."""
    text = _read(path)
    return text is None or _body(text) == _body(kit)


def _register(repo: AgentsRepo, seat_slug: str) -> bool:
    """The shared agent is in the table; when the kit was not synced yet it is registered from the kit file."""
    if repo.find(seat_slug) is not None:
        return True
    from hokma.db.seeds import _read_agents, kit_agents_dir

    meta = _read_agents(kit_agents_dir()).get(seat_slug)
    if meta is None:
        return False
    repo.upsert_kit(seat_slug, meta["display_name"], meta["description"], meta["model_tier"], f"agents/{meta['file']}", is_custom=False)
    return True


def _move(session: Session, desk_id: int, old: str, new: str) -> None:
    """The rows that point at the old agent and are state of the desk, to the shared agent. No statement here can fail on a foreign key: the new agent exists."""
    p = {"d": desk_id, "o": old, "n": new}
    if session.one("SELECT 1 AS x FROM desk_agents WHERE desk_id = :d AND agent_slug = :n", p) is None:
        held = session.one("SELECT role, model, active, added_at FROM desk_agents WHERE desk_id = :d AND agent_slug = :o", p)
        session.execute("UPDATE desk_agents SET role = 'custom' WHERE desk_id = :d AND agent_slug = :o", p)         # a desk has one chief: the old row steps aside before the new one takes the role
        session.execute("INSERT INTO desk_agents (desk_id, agent_slug, role, model, active, added_at) VALUES (:d, :n, :r, :m, :x, :t)",
                        {**p, "r": held["role"], "m": held["model"], "x": held["active"], "t": held["added_at"]})
    session.execute("UPDATE desk_seats SET agent_slug = :n WHERE desk_id = :d AND agent_slug = :o", p)
    session.execute("DELETE FROM desk_agents WHERE desk_id = :d AND agent_slug = :o", p)
    session.execute("UPDATE conversations SET agent_slug = :n WHERE agent_slug = :o", p)
    session.execute("UPDATE messages SET agent_slug = :n WHERE agent_slug = :o", p)
    session.execute("UPDATE scheduled_jobs SET agent_slug = :n WHERE agent_slug = :o", p)
    session.execute("UPDATE tasks SET assigned_agent = :n WHERE assigned_agent = :o AND status IN (" + ", ".join(f"'{s}'" for s in _OPEN_TASK) + ")", p)
    session.execute("UPDATE agents SET status = 'off', file_path = NULL WHERE slug = :o", p)       # kept for the history that points at it, hidden from the list


def _remove_copies(old: str, desk_folder: Path | None, seat_slug: str, kit: str) -> None:
    home = hokma_home()
    for rel in (f"agents/{old}.md", f".claude/agents/{old}.md", f".claude/agents.off/{old}.md"):
        try:
            (home / rel).unlink()
        except OSError:
            pass
    if desk_folder is not None:
        team = desk_folder / "team" / "agents"
        if _unchanged(team / f"{seat_slug}.md", kit):
            for target in (team / f"{seat_slug}.md",):
                try:
                    target.unlink()
                except OSError:
                    pass
            try:
                team.rmdir()
            except OSError:
                pass


def merge_seat_agents(session: Session) -> list[str]:
    """Move every seat of every desk from its own agent to the shared one. Returns the old slugs that were moved."""
    if not (session.table_exists("desk_seats") and session.table_exists("agents")):
        return []
    repo = AgentsRepo(session)
    moved: list[str] = []
    for row in session.query("SELECT s.desk_id AS desk_id, s.agent_slug AS agent_slug, p.path AS path FROM desk_seats s "
                             "JOIN desks d ON d.id = s.desk_id LEFT JOIN projects p ON p.id = d.project_id ORDER BY s.desk_id, s.seat_code"):
        old = str(row["agent_slug"])
        found = _OWN_SLUG.match(old)
        if found is None or int(found.group("desk")) != int(row["desk_id"]):
            continue
        new = found.group("seat")
        try:
            kit = _kit_text(new)
            if kit is None or not _register(repo, new):
                continue                                                      # not a seat agent of this kit: left as it is
            current = repo.find(old)
            home = hokma_home()
            source = home / (current.file_path if current is not None and current.file_path else f"agents/{old}.md")
            if not _unchanged(source, kit):
                log.info("desk %s keeps its own copy of %s: the owner changed it", row["desk_id"], old)
                continue
            _move(session, int(row["desk_id"]), old, new)
            _remove_copies(old, Path(row["path"]) if row["path"] else None, new, kit)
            moved.append(old)
        except Exception as exc:                                              # noqa: BLE001  (one seat never stops the start; it is tried again next time)
            log.warning("seat agent %s was not moved: %s", old, exc)
    return moved
