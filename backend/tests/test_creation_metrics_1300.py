"""1.3.0 (task 3433), F: what a creation costs. The baseline of 1.2.8 is in the base, the calls are counted as they come in, the creation closes into one row and the tokens are read again."""
import pytest
from dbsupport import env  # noqa: F401  (fixture)
from pgsupport import pg_admin_url, pg_template, pg_url_migrated  # noqa: F401  (fixtures)

from hokma.db import get_database, migrate
from hokma.factory import creation_metrics as cm
from hokma.kit import hokma_tools
from e2e_h4.support import caller, ok


@pytest.fixture
def db(env):
    migrate()
    cm.reset()
    return get_database()


def rows(db, sql, **params):
    with db.read() as u:
        return u.session.query(sql, params)


def test_the_baseline_of_1_2_8_is_saved_with_the_values_the_owner_measured(db):
    base = {r["agent"]: r for r in cm.baseline(db)}
    assert set(base) == {"sam", "joel-lab"} and all(r["label"] == "baseline-1.2.8" and r["kind"] == "baseline" for r in base.values())
    assert (base["sam"]["seconds"], base["sam"]["llm_calls"], base["sam"]["tokens_total"]) == (15 * 60 + 3, 75, 254880)
    assert (base["joel-lab"]["seconds"], base["joel-lab"]["llm_calls"], base["joel-lab"]["tokens_total"]) == (4 * 60 + 35, 10, 89934)


def test_every_call_of_a_tool_is_counted_and_dry_runs_apart(db):
    hokma_tools.call("task_list", {}, caller("hokma", conversation=7))
    hokma_tools.call("task_list", {"bogus": 1}, caller("hokma", conversation=7))          # a refused call is a call too
    cm.note_call(7, "strategy_dry_run", "hokma")
    cm.note_call(8, "task_list", "hokma")
    first = cm.window(7)
    assert first["tool_calls"] == 3 and first["dry_runs"] == 1 and cm.window(8)["tool_calls"] == 1


def test_a_window_idle_for_half_an_hour_starts_again(db):
    cm.note_call(3, "a", "hokma", now=1_000.0)
    cm.note_call(3, "b", "hokma", now=1_000.0 + cm.IDLE_SECONDS + 1)
    assert cm.window(3)["tool_calls"] == 1


def test_finishing_writes_one_row_and_reads_the_tokens_of_the_conversation(db):
    import time

    with db.write() as u:
        conv = u.session.insert("conversations", {"engine": "claude"})
        u.session.insert("messages", {"conversation_id": conv, "role": "assistant", "content": "x", "tokens_in": 1000, "tokens_out": 234})
    now = time.time()
    cm.note_call(conv, "strategy_dry_run", "hokma", now=now - 60)
    cm.note_call(conv, "strategy_create", "hokma", now=now - 30)
    out = cm.finish(conversation_id=conv, bank_id=5, str_code="STR-005", agent="hokma", hokma_version="1.3.0", db=db, now=now)
    assert (out["seconds"], out["tool_calls"], out["llm_calls"], out["dry_runs"]) == (60, 2, 3, 1) and out["tokens_total"] == 1234
    with db.write() as u:                                                                      # the turn that was running saves its tokens: the report reads them again
        u.session.insert("messages", {"conversation_id": conv, "role": "assistant", "content": "y", "tokens_in": 10, "tokens_out": 6})
    assert cm.report(out["id"], db=db)["tokens_total"] == 1250
    assert out["baseline"][0]["llm_calls"] == 75 and "Baseline 1.2.8 (Sam): 15 min 03 s, 75 calls, 254.880 tokens" in out["summary"]
    saved = rows(db, "SELECT * FROM creation_metrics WHERE kind = 'creation'")
    assert len(saved) == 1 and saved[0]["str_code"] == "STR-005" and saved[0]["bank_id"] == 5 and cm.window(None) is None


def test_the_report_tool_answers_with_the_last_creation(db):
    cm.note_call(None, "strategy_create", "hokma")
    cm.finish(conversation_id=None, bank_id=1, str_code="STR-001", agent="hokma", hokma_version="1.3.0", db=db)
    answer = ok("creation_report", {}, caller("hokma", conversation=1))
    assert answer["kind"] == "creation" and answer["str_code"] == "STR-001"
