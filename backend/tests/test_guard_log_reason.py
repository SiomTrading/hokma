"""A block of code assembled at run time keeps the rule `real_money` in guard_events and carries its reason to the chat card."""
import json

from hokma.permissions import guard_log


class _Stored:
    id = 1
    created_at = "2026-11-05 12:00:00"


class _Events:
    def record_block(self, rule, tool, target, task_id=None, at=None):
        self.rule = rule
        return _Stored()


class _Db:
    def __init__(self):
        self.events = _Events()

    def write(self):
        db = self

        class _Ctx:
            def __enter__(self):
                return type("U", (), {"guard_events": db.events})()

            def __exit__(self, *a):
                return False
        return _Ctx()


def _drain(tmp_path, monkeypatch, line):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path))
    (tmp_path / "guard").mkdir()
    (tmp_path / "guard" / "blocks.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
    db = _Db()
    return db, guard_log.drain(db)


def test_dynamic_code_reason_reaches_the_event_and_the_rule_stays_real_money(tmp_path, monkeypatch):
    db, events = _drain(tmp_path, monkeypatch, {"rule": "real_money", "reason": "dynamic_code", "tool": "Bash", "target": "x"})
    assert db.events.rule == "real_money" and events[0]["rule"] == "real_money" and events[0]["reason"] == "dynamic_code"


def test_unknown_reason_is_not_passed_on(tmp_path, monkeypatch):
    _, events = _drain(tmp_path, monkeypatch, {"rule": "real_money", "reason": "<script>", "tool": "Bash", "target": "x"})
    assert "reason" not in events[0]
