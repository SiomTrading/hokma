"""What the desktop window (Hokma.exe) needs from the engine: `busy` in /health and a shortcut that opens the window when it is installed."""
from pathlib import Path

from fastapi.testclient import TestClient

from hokma.core import app as app_module, config, shortcuts
from hokma.core.app import create_app


def test_health_reports_busy_false_when_nothing_runs():
    body = TestClient(create_app()).get("/health").json()
    assert body["status"] == "ok" and body["busy"] is False


def test_health_reports_busy_while_a_turn_runs(monkeypatch):
    monkeypatch.setattr(app_module, "turns_running", lambda: 1)
    assert TestClient(create_app()).get("/health").json()["busy"] is True


def test_health_reports_busy_while_a_scheduled_job_runs(monkeypatch):
    class Running:
        def running_ids(self):
            return [7]

    monkeypatch.setattr(app_module, "turns_running", lambda: 0)
    monkeypatch.setattr(app_module, "current_scheduler", lambda: Running())
    assert TestClient(create_app()).get("/health").json()["busy"] is True


def test_desktop_app_lives_outside_the_data_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert config.desktop_app_path() == tmp_path / "Programs" / "Hokma" / "Hokma.exe"


def test_windows_spec_opens_the_desktop_window_when_installed(monkeypatch):
    app = Path(r"C:\Users\a\AppData\Local\Programs\Hokma\Hokma.exe")
    spec = shortcuts.windows_spec(Path(r"C:\Users\a\.local\bin\hokma.cmd"), shortcuts.all_labels(), False,
                                  Path(r"C:\x\hokma-hidden.vbs"), None, app)
    assert spec["target"] == str(app) and spec["icon"] == f"{app},0"
    assert [i["arguments"] for i in spec["items"]] == [""]
    assert spec["remove"]                                    # old Shut down and Restart shortcuts still removed


def test_windows_spec_without_the_window_keeps_the_hidden_launcher(monkeypatch):
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    spec = shortcuts.windows_spec(Path(r"C:\Users\a\.local\bin\hokma.cmd"), shortcuts.all_labels(), False,
                                  Path(r"C:\x\hokma-hidden.vbs"), None)
    assert spec["target"].endswith("wscript.exe")


def test_macos_desktop_app_is_found_in_the_user_applications(monkeypatch, tmp_path):
    monkeypatch.setattr(config.sys, "platform", "darwin")
    monkeypatch.setattr(config.Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / "Applications" / "Hokma.app").mkdir(parents=True)
    assert config.desktop_app_path() == tmp_path / "Applications" / "Hokma.app"


def test_macos_with_the_desktop_app_makes_no_applescript_shortcut(monkeypatch, tmp_path):
    app = tmp_path / "Hokma.app"
    app.mkdir()
    old = tmp_path / "apps" / "Hokma.app"
    old.mkdir(parents=True)
    monkeypatch.setattr(shortcuts.config, "desktop_app_path", lambda: app)
    monkeypatch.setattr(shortcuts, "macos_applications_dir", lambda: tmp_path / "apps")
    assert shortcuts._install_macos(tmp_path / "hokma", shortcuts.all_labels(), False) == []
    assert not old.exists() and app.is_dir()


ROOT = Path(__file__).resolve().parents[2]
DESKTOP = ROOT / "desktop"


def test_desktop_texts_exist_in_spanish_and_english_and_cover_the_code():
    import json
    import re

    es = json.loads((DESKTOP / "messages" / "es.json").read_text(encoding="utf-8"))
    en = json.loads((DESKTOP / "messages" / "en.json").read_text(encoding="utf-8"))
    assert es.keys() == en.keys()
    used = set()
    for source in (DESKTOP / "src-tauri" / "src").glob("*.rs"):
        used |= set(re.findall(r'\bt\("(\w+)"', source.read_text(encoding="utf-8")))
    used |= set(re.findall(r'\bt\("(\w+)"\)', (DESKTOP / "ui" / "splash.js").read_text(encoding="utf-8")))
    assert used and used <= es.keys(), used - es.keys()


def test_desktop_build_names_match_the_workflow():
    import json
    import tomllib

    cargo = tomllib.loads((DESKTOP / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8"))
    conf = json.loads((DESKTOP / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    workflow = (ROOT / ".github" / "workflows" / "release-installers.yml").read_text(encoding="utf-8")
    assert f"target/release/{cargo['package']['name']}.exe" in workflow
    assert conf["productName"] == "Hokma" and conf["build"]["frontendDist"] == "../ui"
    for icon in conf["bundle"]["icon"]:
        assert (DESKTOP / "src-tauri" / icon).is_file(), icon
    assert "releases/latest/download/Hokma-Setup.exe" in workflow and "Hokma.dmg" in workflow


def test_desktop_files_have_no_em_dash():
    for path in DESKTOP.rglob("*"):
        if path.is_file() and path.suffix in {".rs", ".json", ".js", ".html", ".css", ".md", ".toml", ".plist"}:
            assert "—" not in path.read_text(encoding="utf-8"), path


def test_version_change_clears_only_webview_caches_never_the_panel_storage():
    sources = "".join(p.read_text(encoding="utf-8") for p in (DESKTOP / "src-tauri" / "src").glob("*.rs"))
    code = "\n".join(line for line in sources.splitlines() if not line.lstrip().startswith("//"))
    assert "clear_all_browsing_data" not in code               # it would delete layout, favorites and language
    assert "prune_if_version_changed()" in code and "versioned(&url, version)" in code


def test_3150_the_window_only_hands_web_pages_and_email_to_the_system():
    """Read without compiling Rust: the scheme list of `opens_outside` is exactly http, https and mailto, and the one
    call to the system opener sits behind it (cargo test runs the Rust tests of the same function in the release)."""
    import re

    source = (DESKTOP / "src-tauri" / "src" / "main.rs").read_text("utf-8")
    body = re.search(r"fn opens_outside\(url: &Url\) -> bool \{(.*?)\n\}", source, re.S).group(1)
    assert sorted(re.findall(r'"([a-z]+)"', body)) == ["http", "https", "mailto"]
    calls = [m.start() for m in re.finditer(r"open_url\(", source)]
    assert len(calls) == 1
    guard = source.rfind("if opens_outside(url) {", 0, calls[0])
    assert guard != -1 and source.count("{", guard, calls[0]) == 1 and "}" not in source[guard:calls[0]]


def test_3138_shared_state_is_a_plain_reference_and_desktop_compiles_before_release():
    """The 0.0.15 installers failed with E0716: `shared()` returned a temporary `tauri::State` and every
    `let inner = shared(app).0.lock().unwrap();` kept a lock guard on it. `shared()` now returns `&Shared`, and the
    desktop-check workflow compiles and tests the window on Windows and macOS on every push, before a release."""
    import re

    engine = (DESKTOP / "src-tauri" / "src" / "engine.rs").read_text("utf-8")
    assert re.search(r"pub fn shared\(app: &AppHandle\) -> &Shared \{\s*app\.state::<Shared>\(\)\.inner\(\)", engine)
    sources = "".join(p.read_text("utf-8") for p in (DESKTOP / "src-tauri" / "src").glob("*.rs"))
    assert "-> tauri::State<" not in sources and "-> State<" not in sources
    workflow = (ROOT / ".github" / "workflows" / "desktop-check.yml").read_text("utf-8")
    assert "windows-latest" in workflow and "macos-latest" in workflow
    assert "cargo check --all-targets" in workflow and "cargo test" in workflow
    assert "push:" in workflow and "workflow_dispatch:" in workflow
    assert "contents: read" in workflow and "secrets." not in workflow
