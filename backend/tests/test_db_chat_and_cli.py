"""Message status, attachment names, permission records, and the CLI hooks (db migrate, post-update)."""
import sqlite3

import pytest
from dbsupport import HUMAN, db, env  # noqa: F401  (fixtures)

from hokma.db import IntegrityViolation, ValidationError, db_path, get_database, migrate
from hokma.engine.dbstore import DbChatStore
from hokma.i18n import t
from hokma_cli.main import main


def test_message_status_and_attachment_name(db):
    with db.write() as u:
        c = u.conversations.create(engine="claude_code")
        m = u.messages.add(c.id, "assistant", "partial", status="stopped")
        assert m.status == "stopped" and u.messages.add(c.id, "user", "hi").status == "complete"
        assert u.messages.update(m.id, status="error").status == "error"
        with pytest.raises(ValidationError):
            u.messages.add(c.id, "user", "x", status="weird")
        with pytest.raises(ValidationError):
            u.messages.update(m.id, status="weird")
        with pytest.raises(IntegrityViolation):
            u.session.execute("UPDATE messages SET status = 'weird' WHERE id = :i", {"i": m.id})
        a = u.attachments.add(kind="image", stored_path="/d/a", mime_type="image/png", size_bytes=3, sha256="a" * 64,
                              name="chart.png", message_id=m.id)
        assert a.name == "chart.png" and u.attachments.add(
            kind="file", stored_path="/d/b", mime_type="text/plain", size_bytes=3, sha256="b" * 64).name is None


def test_permission_answers_go_to_approvals_and_timeouts_to_the_guard_log(db):
    with db.write() as u:
        c = u.conversations.create(engine="claude_code")
        a = u.approvals.record_permission(c.id, "Bash", "approved", detail="ls -la")
        assert (a.subject_type, a.subject_id, a.decided_by_kind, a.channel, a.note) == (
            "permission", c.id, "human", "dashboard", "Bash: ls -la")
        assert u.approvals.record_permission(c.id, "Write", "rejected").verdict == "rejected"
        with pytest.raises(ValidationError):
            u.approvals.record_permission(c.id, "Bash", "changes_requested")
        g = u.guard_events.record_permission_ended("Bash", "timeout")
        assert (g.guard, g.decision, g.tool) == ("permission", "blocked", "Bash")


def test_chat_store_persists_status_names_and_permissions(db):
    store = DbChatStore(db)
    conv = store.create_conversation("t", "sonnet", None, None)
    msg = store.add_message(conv.id, "assistant", "cut short", status="stopped")
    assert store.list_messages(conv.id)[0].status == "stopped" and msg.status == "stopped"
    att = store.add_attachment("c" * 64, "photo.png", "image/png", 10)
    assert store.get_attachment(att.id).name == "photo.png"
    store.record_permission(conv.id, "Bash", {"kind": "run", "target": "ls"}, "allowed_by_human", "human", "dashboard")
    store.record_permission(conv.id, "Bash", {"kind": "run", "target": "ls"}, "timeout", "system", "dashboard")
    with db.read() as u:
        assert [a.verdict for a in u.approvals.list_for("permission", int(conv.id))] == ["approved"]
        assert [(g.decision) for g in u.guard_events.list(guard="permission")] == ["blocked", "allowed_by_human"]


def test_hokma_db_migrate_command(env, capsys):
    assert main(["db", "migrate"]) == 0
    assert t("db.migrate.done", version=6) in capsys.readouterr().out
    assert main(["db", "migrate"]) == 0
    assert sqlite3.connect(str(db_path())).execute("SELECT count(*) FROM schema_version").fetchone()[0] == 6


def test_hokma_db_migrate_exits_with_4_and_the_catalog_message(env, capsys):
    db_path().parent.mkdir(parents=True)
    db_path().write_bytes(b"garbage" * 200)
    assert main(["db", "migrate"]) == 4
    err = capsys.readouterr().err
    assert t("db.migrate.open_failed", path=str(db_path()), error="")[:30] in err


def test_post_update_migrates_after_the_kit_sync(env, capsys):
    assert main(["post-update"]) == 0
    capsys.readouterr()
    with get_database().read() as u:
        assert u.agents.count() >= 10


def test_the_owner_command_mirrors_the_names_into_the_profile(env, monkeypatch):
    import hokma_cli.main as cli

    monkeypatch.setattr(cli, "_require_human", lambda: True)
    answers = iter(["Ana", "Fondo Uno"])
    monkeypatch.setattr(cli, "_ask", lambda prompt: next(answers))
    assert main(["owner"]) == 0
    with get_database().read() as u:
        p = u.profile.get()
        assert (p.owner_name, p.fund_name) == ("Ana", "Fondo Uno")
    migrate()                                                       # nothing is lost on the next start
    with get_database().read() as u:
        assert u.profile.get().owner_name == "Ana"
