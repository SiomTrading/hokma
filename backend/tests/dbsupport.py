"""Shared fixtures for the database tests (import them into a test module)."""
import json

import pytest

from hokma.db import Actor, get_database, migrate

HUMAN = Actor.human("owner")
SYSTEM = Actor.system()


def agent(slug: str) -> Actor:
    return Actor.agent(slug)


class FakeClaude:
    """Stands in for `claude plugin ...` so no test downloads anything: records every call, and an `install` that
    succeeds writes the folder and plugins/installed_plugins.json the way Claude Code does. `fail` maps a
    plugin id to the output of a failed install."""

    def __init__(self, config_dir):
        self.config = config_dir
        self.calls: list[list[str]] = []
        self.fail: dict[str, str] = {}
        self.timeout = False
        self.silent = False          # `install` exits 0 but leaves nothing on disk

    def _write(self, name, content):
        path = self.config / "plugins" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content), encoding="utf-8")

    def _read(self, name):
        path = self.config / "plugins" / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def __call__(self, argv, timeout):
        from hokma.status.plugin_install import InstallError

        self.calls.append(list(argv))
        if self.timeout:
            raise InstallError("timeout")
        action = argv[:2] if argv[1] != "marketplace" else argv[:3]
        target = argv[-1]
        if action == ["plugin", "marketplace", "add"]:
            known = self._read("known_marketplaces.json")
            known[target.split("/")[0]] = {"source": target}   # owner/repo: the manifest names it after the owner here
            self._write("known_marketplaces.json", known)
            return 0, '{"outcome":"ok","message":"added"}'
        if action == ["plugin", "install"]:
            if self.silent:
                return 0, '{"outcome":"ok"}'
            if target in self.fail:
                return 1, json.dumps({"outcome": "failed", "message": self.fail[target], "failureCode": "x"})
            folder = self.config / "plugins" / "cache" / target.replace("@", "-")
            (folder / ".claude-plugin").mkdir(parents=True, exist_ok=True)
            (folder / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
            (folder / "hooks").mkdir(exist_ok=True)
            (folder / "hooks" / "hooks.json").write_text("{}", encoding="utf-8")
            installed = self._read("installed_plugins.json") or {"version": 2, "plugins": {}}
            installed["plugins"][target] = [{"scope": "user", "installPath": str(folder), "version": "1"}]
            self._write("installed_plugins.json", installed)
            return 0, '{"outcome":"ok"}'
        if action == ["plugin", "marketplace", "remove"]:
            known = self._read("known_marketplaces.json")
            known.pop(target, None)
            self._write("known_marketplaces.json", known)
            return 0, ""
        if action == ["plugin", "uninstall"]:
            installed = self._read("installed_plugins.json") or {"version": 2, "plugins": {}}
            installed["plugins"].pop(target, None)
            self._write("installed_plugins.json", installed)
            return 0, '{"outcome":"ok"}'
        return 0, ""


@pytest.fixture
def fake_claude(env):
    """The FakeClaude every `env` already installed (so no test can run the real CLI)."""
    from hokma.status import plugin_install

    return plugin_install.runner


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOKMA_HOME", str(tmp_path / "work"))
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOKMA_LANG", "en")
    from hokma.status import plugin_install

    monkeypatch.setattr(plugin_install, "runner", FakeClaude(tmp_path / "data" / "claude-config"))
    monkeypatch.setattr(plugin_install, "worker_alive", lambda port: True)
    monkeypatch.setattr(plugin_install, "log", lambda message: None)
    return tmp_path


@pytest.fixture
def db(env):
    """A migrated database in a temporary data folder, with three agents ready to work."""
    migrate()
    database = get_database()
    with database.write() as u:
        for slug in ("hokma", "joel", "vera", "iris"):
            u.agents.upsert_kit(slug, slug.capitalize(), None, "balanced", f"agents/{slug}.md")
            u.agents.set_status(slug, "active")
    return database
