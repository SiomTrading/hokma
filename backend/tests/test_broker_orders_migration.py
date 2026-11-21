"""0020_broker_orders.sql (task 3299, SPEC-015 18.6): additive, a 1.0.8 base migrates without losing a row, the three tables
and their limits, and the repos that sit on them (an order record never holds an account identifier)."""
import shutil
import sqlite3

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from test_db_migrate import _desk, _desk_project, raw

from hokma.db import migrate
from hokma.db.errors import NotFound, ValidationError
from hokma.db.migrate import MIGRATIONS_DIR
from hokma.db.transfer import BROKER_TABLES

NEW_TABLES = ("broker_orders", "broker_external_accounts", "desk_limits")
FP = "f" * 64


def _account(con, name="A", capital=1000, broker=1):
    con.execute("INSERT INTO broker_accounts (broker_id, name, initial_capital) VALUES (?, ?, ?)", (broker, name, capital))
    return con.execute("SELECT id FROM broker_accounts WHERE name = ? AND broker_id = ?", (name, broker)).fetchone()[0]


def _order(con, **values):
    row = {"broker_kind": "schwab", "action": "place", **values}
    con.execute(f"INSERT INTO broker_orders ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))


def _tables_sql(con):
    return dict(con.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"))


def test_a_1_0_8_base_migrates_by_adding_three_tables_and_loses_nothing(env):
    old = env / "old19"
    old.mkdir()
    for f in MIGRATIONS_DIR.glob("00*.sql"):
        if int(f.name[:4]) <= 19:
            shutil.copy(f, old / f.name)
    assert migrate(migrations_dir=old, seed=False).version == 19
    con = raw()
    desk = _desk(con, _desk_project(con))
    account = _account(con)
    con.execute("INSERT INTO desk_accounts VALUES (?, ?)", (desk, account))
    con.commit()
    before_sql = _tables_sql(con)
    before_rows = {t: con.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in before_sql}
    con.close()
    assert migrate().applied == [20]
    con = raw()
    after_sql = _tables_sql(con)
    assert {n: s for n, s in after_sql.items() if n in before_sql} == before_sql        # no table was altered
    assert set(after_sql) - set(before_sql) - {n for n in after_sql if n.startswith("doc_chunks_fts_")} == set(NEW_TABLES)
    kept = ("projects", "desks", "broker_accounts", "account_ledger", "desk_accounts")      # what the test filled
    assert {t: con.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in kept} == {t: before_rows[t] for t in kept}
    assert con.execute("SELECT count(*) FROM desks").fetchone()[0] == 1
    assert con.execute("SELECT kind, name FROM brokers").fetchall() == [("sim", "Hokma")]       # only the seed of the broker is new
    assert all(con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0 for t in NEW_TABLES)
    assert set(NEW_TABLES) <= set(BROKER_TABLES)
    assert migrate().applied == []


def test_broker_orders_limits(env):
    migrate()
    con = raw()
    _order(con)
    _order(con, action="preview", rule_result="needs_owner", approved_by="owner", http_status=201, reason="x" * 2000)
    for bad in ({"action": "send"}, {"rule_result": "maybe"}, {"approved_by": "agent"}, {"http_status": 99},
                {"http_status": 600}, {"reason": "x" * 2001}, {"broker_kind": ""}, {"broker_kind": "k" * 41},
                {"broker_order_id": ""}, {"correl_id": "c" * 129}):
        with pytest.raises(sqlite3.IntegrityError):
            _order(con, **bad)
    assert con.execute("SELECT count(*) FROM broker_orders").fetchone()[0] == 2


def test_external_accounts_and_desk_limits_and_their_foreign_keys(env):
    migrate()
    con = raw()
    con.execute("PRAGMA foreign_keys = ON")
    desk = _desk(con, _desk_project(con))
    account = _account(con)
    con.execute("INSERT INTO broker_external_accounts VALUES (?, ?, ?)", (account, FP, "••••1234"))
    other = _account(con, "B")
    for row in ((other, FP, "••••9999"),                  # UNIQUE fingerprint
                (other, "short", "••••9999"),             # CHECK length
                (other, "g" * 64, ""),                    # CHECK mask
                (account, "h" * 64, "••••1")):            # one link per account
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("INSERT INTO broker_external_accounts VALUES (?, ?, ?)", row)
    with pytest.raises(sqlite3.IntegrityError):           # the account of a link is never deleted
        con.execute("DELETE FROM broker_accounts WHERE id = ?", (account,))
    _order(con, desk_id=desk, account_id=account)
    con.execute("DELETE FROM broker_external_accounts WHERE account_id = ?", (account,))
    with pytest.raises(sqlite3.IntegrityError):           # nor the account of an order
        con.execute("DELETE FROM broker_accounts WHERE id = ?", (account,))
    con.execute("INSERT INTO desk_limits (desk_id) VALUES (?)", (desk,))
    assert con.execute("SELECT config FROM desk_limits").fetchone()[0] == "{}"
    con.execute("DELETE FROM desks WHERE id = ?", (desk,))
    assert con.execute("SELECT count(*) FROM desk_limits").fetchone()[0] == 0                      # cascade
    assert con.execute("SELECT desk_id, account_id FROM broker_orders").fetchall() == [(None, account)]   # the log stays


# ---------------------------------------------------------------------------------------------------- the repos


def test_the_repo_records_an_order_with_the_mask_already_applied_and_lists_it(db):
    with db.write() as u:
        desk = _desk_for(u)
        account = u.broker.create_account(1, "S", 1000)
        row = u.broker.record_order(
            broker_kind="schwab", action="place", desk_id=desk, account_id=account.id, agent_slug="hugo", reason="entry",
            request={"orderType": "LIMIT", "account_ref": "A1", "mask": "••••1234"}, rule_result="auto", approved_by="auto",
            http_status=201, broker_order_id=123, response={"orderId": 123}, correl_id="c-1")
        u.broker.record_order(broker_kind="schwab", action="preview", desk_id=desk, rule_result="refused", rule_why="off")
        assert row.request_json == {"orderType": "LIMIT", "account_ref": "A1", "mask": "••••1234"}
        assert (row.broker_order_id, row.response_json, row.approved_by) == ("123", {"orderId": 123}, "auto")
        newest = u.broker.list_order_records(desk, limit=1)
        assert [r.action for r in newest] == ["preview"] and newest[0].request_json is None
        assert [r.action for r in u.broker.list_order_records(desk)] == ["preview", "place"]
        assert u.broker.list_order_records(desk, since="2999-01-01T00:00:00Z") == []
        assert u.broker.count_order_records(desk, "place", "2000-01-01T00:00:00Z") == 1
        assert u.broker.count_order_records(desk, "preview", "2000-01-01T00:00:00Z") == 0     # a refused attempt does not count


def test_the_repo_refuses_an_order_record_that_still_carries_an_identifier(db):
    with db.write() as u:
        for field in ("request", "response"):
            for leak in ({"accountNumber": "12345678"}, {"a": [{"b": {"hashValue": "ABCDEF"}}]}):
                with pytest.raises(ValidationError):
                    u.broker.record_order(broker_kind="schwab", action="place", **{field: leak})
        for bad in ({"action": "send"}, {"rule_result": "x"}, {"approved_by": "x"}, {"reason": "x" * 2001},
                    {"broker_kind": ""}):
            with pytest.raises(ValidationError):
                u.broker.record_order(**{"broker_kind": "schwab", "action": "place", **bad})
        assert u.broker.s.scalar("SELECT count(*) FROM broker_orders") == 0


def test_the_repo_links_finds_and_archives_external_accounts(db):
    with db.write() as u:
        broker = u.broker.create_broker("schwab", "Schwab", {"orders_enabled": False})
        account = u.broker.create_account(broker.id, "S", 1000)
        link = u.broker.add_external(account.id, FP, "••••1234")
        assert (link.account_id, link.fingerprint, link.mask) == (account.id, FP, "••••1234")
        assert u.broker.find_by_fingerprint(FP).id == account.id and u.broker.find_by_fingerprint("0" * 64) is None
        with pytest.raises(ValidationError):
            u.broker.add_external(account.id, "short", "••••1234")
        with pytest.raises(NotFound):
            u.broker.add_external(9999, FP, "••••1234")
        assert u.broker.archive_broker_accounts(broker.id) == 1 and u.broker.list_accounts(broker.id) == []
        assert u.broker.archive_broker_accounts(broker.id) == 0                   # idempotent
        assert u.broker.set_account_status(account.id, "active").status == "active"
        with pytest.raises(ValidationError):
            u.broker.set_account_status(account.id, "deleted")


def test_desk_limits_default_to_none_and_are_replaced_whole(db):
    with db.write() as u:
        desk = _desk_for(u)
        assert u.broker.get_limits(desk) == {}
        assert u.broker.set_limits(desk, {"max_order_pct": 10}) == {"max_order_pct": 10}
        assert u.broker.set_limits(desk, {"short_allowed": False}) == {"short_allowed": False}
        assert u.broker.set_limits(desk, {}) == {}
        with pytest.raises(ValidationError):
            u.broker.set_limits(desk, ["x"])


def _desk_for(u) -> int:
    """A desk through the session, with the same minimal columns the migration tests use."""
    project = u.broker.s.insert("projects", {"slug": "mesa", "name": "mesa", "path": "/p/mesa", "kind": "strategy"})
    return u.broker.s.insert("desks", {"project_id": project, "name": "Mesa", "capital": 1000})
