"""Migration 0039 (Hokma 1.2.9, task 3431): the agents are free with the base. The triggers that decided who moves a task or a bug and from where are gone, the bug table is rebuilt without the
check of the fixing task, and the locks that stay (the approval of the owner, the real-money lock) are still in the base. The rows of bugs and tasks of 1.2.8 survive."""
import shutil
import sqlite3

import pytest
from dbsupport import env  # noqa: F401  (fixture)

from hokma.db import current_version, db_path, migrate
from hokma.db.migrate import MIGRATIONS_DIR, discover

VERSION = int(next(p.name for p in MIGRATIONS_DIR.glob("*_free_agents.sql"))[:4])


def con_of():
    con = sqlite3.connect(str(db_path()))
    con.execute("PRAGMA foreign_keys = ON")
    return con


def test_the_triggers_of_state_are_gone_and_the_locks_of_money_stay(env):
    assert VERSION in migrate().applied and VERSION == 39 and current_version() == discover()[-1][0]
    names = {r[0] for r in con_of().execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert "trg_bugs_transition" not in names and {"trg_tasks_guard", "trg_tasks_transition", "trg_tasks_touch", "trg_tasks_event_ins", "trg_bugs_touch"} <= names
    text = (MIGRATIONS_DIR / f"{VERSION:04d}_free_agents.sql").read_text(encoding="utf-8")
    assert "\u2014" not in text and "task requires the approval of the owner" in text and "an agent cannot lower approval or the real-money lock" in text


def test_the_bugs_and_the_tasks_of_1_2_8_survive_the_rebuild(env, tmp_path):
    older = tmp_path / "older"
    older.mkdir()
    for v, _name, path in discover():
        if v < VERSION:
            shutil.copy(path, older / path.name)
    migrate(migrations_dir=older)
    con = con_of()
    t = con.execute("INSERT INTO tasks (title, status, created_by_kind, created_by, changed_by_kind, changed_by) VALUES ('t', 'todo', 'human', 'owner', 'human', 'owner')").lastrowid
    for status in ("open", "in_progress"):
        con.execute("INSERT INTO bugs (title, status, found_by_kind, found_by) VALUES (?, ?, 'human', 'owner')", (f"bug {status}", status))
    con.execute("INSERT INTO bugs (title, status, found_by_kind, found_by, fixed_by_task_id) VALUES ('fixed', 'fixed', 'agent', 'joel', ?)", (t,))
    con.commit()
    con.close()
    assert VERSION in migrate().applied
    con = con_of()
    assert [r[0] for r in con.execute("SELECT status FROM bugs ORDER BY id")] == ["open", "in_progress", "fixed"]
    assert con.execute("SELECT fixed_by_task_id FROM bugs WHERE status = 'fixed'").fetchone()[0] == t
    assert con.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
    con.execute("UPDATE bugs SET status = 'fixed', changed_by_kind = 'agent', changed_by = 'hokma' WHERE status = 'open'")          # open to fixed, with no task: the case of 1.2.8
    con.execute("UPDATE bugs SET status = 'verified', changed_by_kind = 'agent', changed_by = 'hokma' WHERE status = 'fixed' AND found_by = 'owner'")
    assert con.execute("SELECT count(*) FROM bugs WHERE status = 'verified'").fetchone()[0] == 1
    con.execute("UPDATE bugs SET status = 'wont_fix', closed_at = CURRENT_TIMESTAMP WHERE status = 'in_progress'")
    con.commit()
    idx = {r[1] for r in con.execute("PRAGMA index_list(bugs)")}
    assert "idx_bugs_status" in idx
    with pytest.raises(sqlite3.IntegrityError):                       # the checks of the table that stay
        con.execute("INSERT INTO bugs (title, status, found_by_kind, found_by) VALUES ('x', 'closed', 'human', 'owner')")
