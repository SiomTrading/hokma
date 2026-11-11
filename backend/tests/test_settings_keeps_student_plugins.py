"""Task 3140: plugins installed from inside Claude Code survive the regeneration of settings.json."""
import json

import pytest

from hokma.kit import extensions
from hokma.kit.launch import prepare_claude_launch
from hokma.kit.settings import build_settings, guard_command, hardblock_command, write_settings


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "work").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOKMA_HOME", str(tmp_path / "work"))
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return tmp_path


def _config(env):
    return env / "data" / "claude-config"


def _claude_installs(env, settings_extra: dict, marketplaces: dict | None = None):
    """What Claude Code leaves behind after `/plugin marketplace add` + `/plugin install`."""
    path = write_settings()
    live = json.loads(path.read_text())
    live.update(settings_extra)
    path.write_text(json.dumps(live))
    if marketplaces is not None:
        (_config(env) / "plugins").mkdir(parents=True, exist_ok=True)
        (_config(env) / "plugins" / "known_marketplaces.json").write_text(json.dumps(marketplaces))
    return path


def test_plugin_installed_in_claude_survives_launch(env):
    _claude_installs(env, {"enabledPlugins": {"tool@acme": True}},
                     {"acme": {"source": {"source": "github", "repo": "acme/plugins"}, "installLocation": "/x"}})
    prepare_claude_launch()
    settings = json.loads((_config(env) / "settings.json").read_text())
    assert settings["enabledPlugins"] == {"tool@acme": True}
    assert settings["extraKnownMarketplaces"] == {"acme": {"source": {"source": "github", "repo": "acme/plugins"}}}
    # absorbed into the authoritative copy: the dashboard sees it and the next launch keeps it too
    data, _ = extensions.load()
    assert data["enabledPlugins"] == {"tool@acme": True}
    assert "acme" in data["extraKnownMarketplaces"]
    prepare_claude_launch()
    assert json.loads((_config(env) / "settings.json").read_text())["enabledPlugins"] == {"tool@acme": True}


def test_merge_keeps_hokma_plugins_and_student_choice_wins(env):
    extensions.add_marketplace("acme", {"source": "github", "repo": "acme/plugins"})
    extensions.set_plugin("hokma-tool@acme", True)
    extensions.set_plugin("shared@acme", False)
    _claude_installs(env, {"enabledPlugins": {"hokma-tool@acme": True, "shared@acme": True, "extra@acme": True}})
    settings = json.loads(write_settings().read_text())
    assert settings["enabledPlugins"] == {"hokma-tool@acme": True, "shared@acme": False, "extra@acme": True}


def test_locks_are_never_taken_from_the_live_file(env):
    clean = build_settings()
    _claude_installs(env, {
        "enabledPlugins": {"tool@official": True},
        "permissions": {"allow": ["Bash(*)"], "deny": []},
        "sandbox": {"enabled": False},
        "hooks": {},
        "disableAllHooks": True,
    })
    settings = json.loads(write_settings().read_text())
    assert settings["permissions"] == clean["permissions"]
    assert "sandbox" not in settings
    assert settings["disableAllHooks"] is False
    commands = [h["command"] for g in settings["hooks"]["PreToolUse"] for h in g["hooks"]]
    assert guard_command() in commands and hardblock_command() in commands
    assert settings["enabledPlugins"] == {"tool@official": True}


def test_invalid_live_entries_are_dropped(env):
    work_plugin = str(env / "work" / "evil")
    _claude_installs(env, {
        "enabledPlugins": {"bad id": True, "x@y": "yes", "ok@acme": True},
        "extraKnownMarketplaces": {"inwork": {"source": {"source": "directory", "path": work_plugin}}},
    }, {"bad name!": {"source": {"source": "github", "repo": "a/b"}},
        "acme": {"source": {"source": "github", "repo": "acme/plugins", "extra": "dropped"}}})
    settings = json.loads(write_settings().read_text())
    assert settings["enabledPlugins"] == {"ok@acme": True}
    assert settings["extraKnownMarketplaces"] == {"acme": {"source": {"source": "github", "repo": "acme/plugins"}}}


def test_builtin_marketplace_is_not_copied(env):
    _claude_installs(env, {}, {"claude-plugins-official": {"source": {"source": "github", "repo": "anthropics/x"}}})
    assert "extraKnownMarketplaces" not in json.loads(write_settings().read_text())


def test_uninstall_from_dashboard_is_not_brought_back(env):
    _claude_installs(env, {"enabledPlugins": {"tool@official": True}})
    write_settings()
    extensions.remove_plugin("tool@official")
    settings = json.loads(write_settings().read_text())
    assert "tool@official" not in settings.get("enabledPlugins", {})
    data, _ = extensions.load()
    assert "tool@official" not in data["enabledPlugins"]


def test_unreadable_live_file_does_not_break_the_launch(env):
    path = write_settings()
    path.write_text("{not json")
    (_config(env) / "plugins").mkdir(parents=True, exist_ok=True)
    (_config(env) / "plugins" / "known_marketplaces.json").write_text("[1, 2]")
    assert json.loads(write_settings().read_text()) == build_settings()


def test_doctor_does_not_flag_a_fresh_install_as_tampering(env):
    """Right after `/plugin install` (before the next launch) build_settings already includes the plugin."""
    _claude_installs(env, {"enabledPlugins": {"tool@official": True}})
    assert build_settings()["enabledPlugins"] == {"tool@official": True}
