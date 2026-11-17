"""The desktop window never shows white: not while opening, not while closing (bug 1122 reopened, 0.0.36).

The window is Rust (no cargo here), so these read the source and pin the rules; the `desktop-check` workflow compiles it.
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "desktop" / "src-tauri" / "src"
MAIN = (SRC / "main.rs").read_text(encoding="utf-8")
ENGINE = (SRC / "engine.rs").read_text(encoding="utf-8")


def _fn(source: str, name: str) -> str:
    start = source.index(f"fn {name}(")
    return source[start:source.index("\n}\n", start)]


def test_the_window_is_built_hidden_with_the_dark_background():
    assert ".visible(false)" in MAIN and "background_color(Color(16, 21, 22, 255))" in MAIN


def test_the_panel_page_reveals_the_window_after_a_painted_frame_not_at_once():
    assert "engine::reveal_after_paint(&page_handle)" in MAIN
    assert "engine::reveal_once(&page_handle)" not in MAIN
    body = _fn(ENGINE, "reveal_after_paint")
    assert "PAINT_DELAY" in body and "reveal_once" in body


def test_the_fallback_timer_is_long_enough_not_to_show_the_window_early():
    fallback = int(re.search(r"REVEAL_FALLBACK: Duration = Duration::from_secs\((\d+)\)", ENGINE).group(1))
    delay = int(re.search(r"PAINT_DELAY: Duration = Duration::from_millis\((\d+)\)", ENGINE).group(1))
    assert fallback >= 10 and delay >= 300


def test_quit_hides_the_window_before_the_engine_stops():
    body = _fn(MAIN, "begin_close")
    assert "engine::hide_main(&app)" in body
    assert body.index("engine::hide_main(&app)") < body.index("engine::stop_engine(&app)")
    # the confirmation (only when an agent is working) comes first: cancelling must leave the window as it was
    assert body.index("should_ask_before_quit(health_busy, child_alive)") < body.index("engine::hide_main(&app)")


def test_closing_the_window_with_x_only_hides_it():
    assert "api.prevent_close();" in MAIN and "window.hide()" in MAIN


def test_the_start_screen_and_the_panel_page_are_dark_before_any_script():
    css = (SRC.parents[1] / "ui" / "splash.css").read_text(encoding="utf-8")
    assert re.search(r"html, body \{[^}]*background: var\(--bg\)", css) and "--bg: #101516" in css
    page = (SRC.parents[2] / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert "background: #101516" in page


def test_a_late_reveal_never_shows_the_window_while_hokma_is_closing():
    body = _fn(ENGINE, "reveal_once")
    assert "!inner.closing" in body and "!inner.exiting" in body


def test_quit_asks_when_health_fails_but_the_engine_is_alive():
    """Task 1132: a slow or failed /health is not "nobody works"."""
    body = _fn(MAIN, "begin_close")
    assert "map(|h| h.busy).unwrap_or(false)" not in body
    assert "should_ask_before_quit(health_busy, child_alive)" in body
    decision = _fn(MAIN, "should_ask_before_quit")
    assert "None => engine_alive" in decision and "Some(busy) => busy" in decision
    assert "should_ask_before_quit(None, true)" in MAIN and "!should_ask_before_quit(None, false)" in MAIN


def test_webview2_gets_the_dark_default_background_before_any_webview_exists():
    assert 'set_var("WEBVIEW2_DEFAULT_BACKGROUND_COLOR", "FF101516")' in MAIN
    assert MAIN.index("WEBVIEW2_DEFAULT_BACKGROUND_COLOR") < MAIN.index("tauri::Builder::default()")


def test_a_panel_that_comes_back_blank_after_an_unlock_is_reloaded():
    assert "WindowEvent::Focused" in MAIN and "BLANK_CHECK" in MAIN and "window.location.reload()" in MAIN
    assert "panel_shown" in MAIN[MAIN.index("WindowEvent::Focused"):MAIN.index("WindowEvent::CloseRequested")]
