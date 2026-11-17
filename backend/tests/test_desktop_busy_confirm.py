"""Bug 1135 on the desktop window: Update asks the same question as Quit when an agent works (no cargo here: source rules)."""
import json
from pathlib import Path

DESKTOP = Path(__file__).resolve().parents[2] / "desktop"
SRC = DESKTOP / "src-tauri" / "src"
MAIN = (SRC / "main.rs").read_text(encoding="utf-8")
UPDATE = (SRC / "update.rs").read_text(encoding="utf-8")
HTTP = (SRC / "http.rs").read_text(encoding="utf-8")


def _fn(source: str, name: str) -> str:
    start = source.index(f"fn {name}(")
    return source[start:source.index("\n}\n", start)]


def test_the_question_is_one_helper_used_by_quit_and_update():
    helper = _fn(MAIN, "ask_cut_agent")
    for key in ("busy_question", "busy_close", "busy_keep"):
        assert f't("{key}"' in helper
    assert "busy_question" not in _fn(MAIN, "begin_close")      # not duplicated in Quit
    assert "busy_question" not in UPDATE                         # nor in Update
    assert "ask_cut_agent(&app)" in _fn(MAIN, "begin_close")
    assert "ask_cut_agent(app)" in _fn(UPDATE, "run_update")


def test_update_reuses_the_quit_decision_and_asks_before_calling_the_engine():
    body = _fn(UPDATE, "run_update")
    assert "should_ask_before_quit(health_busy, true)" in body
    assert "http::health(port)" in body
    assert body.index("ask_cut_agent(app)") < body.index("http::start_update(port, token, asked)")


def test_update_sends_force_only_after_the_owner_agreed_and_handles_the_engine_refusal():
    assert "pub fn start_update(port: u16, token: &str, force: bool)" in HTTP
    assert '"?force=true"' in HTTP and "update.in_use" in HTTP and "UpdateOutcome::InUse" in HTTP
    body = _fn(UPDATE, "run_update")
    assert "UpdateOutcome::InUse" in body and "http::start_update(port, token, true)" in body


def test_the_dialog_texts_exist_in_both_desktop_catalogs():
    for lang in ("es", "en"):
        messages = json.loads((DESKTOP / "messages" / f"{lang}.json").read_text(encoding="utf-8"))
        for key in ("busy_question", "busy_close", "busy_keep"):
            assert messages[key]
