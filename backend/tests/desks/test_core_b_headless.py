"""OA-69: an unattended desk run ends and leaves a record; without an engine it fails with a notice."""
import json

from hokma.desks.core import headless
from hokma.llm.protocol import LLMError, LLMReply

KEY = "SENTINEL-KEY-77aa88bb99"


class FakeLLM:
    model = "fake-model"

    def __init__(self, text="all good", error=None):
        self.text, self.error, self.prompts = text, error, []

    def complete(self, messages, *, schema=None, max_tokens=4096):
        self.prompts.append(messages[-1]["content"])
        if self.error:
            raise self.error
        return LLMReply(text=self.text, usage={"tokens_out": 3})


def runner(tmp_path, llm=None, check=lambda: None, notices=None, factory=None):
    registry = headless.RunRegistry(tmp_path / "runs" / "runs.json")
    return headless.HeadlessRunner(registry, llm_factory=factory or (lambda agent, desk: llm), engine_check=check,
                                   notify=(notices.append if notices is not None else None), clock=lambda: "2026-11-22 10:00:00")


def test_a_run_started_by_the_scheduler_finishes_and_leaves_a_record_OA_69(tmp_path):
    llm = FakeLLM("Morning review done")
    r = runner(tmp_path, llm)
    record = r.run_scheduled(7, "Review the open positions")
    assert record.status == "done" and record.desk_id == 7 and record.trigger == "scheduler" and record.agent == "desk-chief"
    assert record.reply_preview == "Morning review done" and record.model == "fake-model" and record.usage == {"tokens_out": 3}
    assert record.started_at == record.ended_at == "2026-11-22 10:00:00" and record.error == "" and llm.prompts == ["Review the open positions"]
    stored = r.registry.get(record.run_id)
    assert stored == record and r.registry.list(desk_id=7, status="done") == [record] and r.registry.list(status="failed") == []


def test_without_a_configured_engine_the_run_fails_with_a_notice_and_calls_no_model_OA_69(tmp_path):
    notices, called = [], []

    def no_engine():
        raise headless.NoEngine("nothing is connected")

    r = runner(tmp_path, check=no_engine, notices=notices, factory=lambda a, d: called.append(1))
    record = r.run(3, "Do the review")
    assert record.status == "failed" and record.code == "no_engine" and "No engine is configured" in record.error
    assert not called and record.notice_sent and len(notices) == 1 and "Desk 3" in notices[0]
    assert r.registry.get(record.run_id).status == "failed"


def test_a_model_failure_is_a_failed_record_not_an_exception_OA_69(tmp_path):
    r = runner(tmp_path, FakeLLM(error=LLMError("quota", "usage limit reached")))
    record = r.run(1, "x")
    assert record.status == "failed" and record.code == "quota" and record.error == "usage limit reached" and record.ended_at
    crash = runner(tmp_path, FakeLLM(error=RuntimeError("boom"))).run(1, "x")
    assert crash.status == "failed" and crash.code == "engine_error" and "RuntimeError: boom" in crash.error


def test_a_broken_notifier_does_not_hide_the_record_OA_69(tmp_path):
    def boom(message):
        raise OSError("telegram is down")

    r = headless.HeadlessRunner(headless.RunRegistry(tmp_path / "r.json"), llm_factory=lambda a, d: FakeLLM(error=LLMError("auth")),
                                engine_check=lambda: None, notify=boom)
    record = r.run(1, "x")
    assert record.status == "failed" and record.notice_sent is False and record.error


def test_a_run_left_running_by_a_dead_process_becomes_interrupted_OA_69(tmp_path):
    registry = headless.RunRegistry(tmp_path / "runs.json")
    left = registry.start(2, "desk-chief", "scheduler", "a long review")
    finished = registry.start(2, "desk-chief", "scheduler", "b")
    finished.status, finished.ended_at = "done", "2026-11-22 09:00:00"
    registry.update(finished)
    assert registry.reconcile(now="2026-11-22 12:00:00") == 1
    again = registry.get(left.run_id)
    assert again.status == "interrupted" and again.code == "interrupted" and again.ended_at == "2026-11-22 12:00:00"
    assert registry.get(finished.run_id).status == "done" and registry.reconcile() == 0


def test_the_record_holds_no_credential_and_no_escape_codes_OA_69(tmp_path):
    r = runner(tmp_path, FakeLLM(f"result with api_key={KEY} and \x1b[31mred\x1b[0m text"))
    record = r.run(1, f"call https://x/?token={KEY} please")
    stored = (tmp_path / "runs" / "runs.json").read_text(encoding="utf-8")
    assert KEY not in stored and "\\u001b" not in stored and "red text" in record.reply_preview


def test_the_registry_keeps_a_bounded_history_and_survives_a_corrupt_file_OA_69(tmp_path):
    path = tmp_path / "runs.json"
    registry = headless.RunRegistry(path)
    for i in range(headless.MAX_RECORDS + 5):
        registry.start(1, "a", "scheduler", f"p{i}")
    assert len(registry.list()) == headless.MAX_RECORDS and registry.list()[-1].prompt_preview == f"p{headless.MAX_RECORDS + 4}"
    path.write_text("{not json", encoding="utf-8")
    assert registry.list() == [] and registry.start(1, "a", "scheduler", "again").status == "running"
    assert json.loads(path.read_text(encoding="utf-8"))["runs"][0]["prompt_preview"] == "again"


def test_the_failure_summary_is_never_empty_OA_69():
    assert headless.failure_summary("auth", "") == "The run failed without a message (code auth)."
    assert headless.failure_summary("x", "", killed=True).startswith("The run was stopped")
    assert len(headless.failure_summary("x", "e" * 5000)) == headless.ERROR_TAIL
