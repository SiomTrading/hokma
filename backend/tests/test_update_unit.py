"""Unit tests: version manifest, safe archives, kit sync, extensions, update endpoint, atomic pointer."""
import hashlib
import io
import json
import re
import subprocess
import tarfile
import zipfile

import pytest
from fastapi.testclient import TestClient

from helpers import before
from hokma.i18n import t
from hokma.core import config
from hokma.core.app import create_app
from hokma.kit import extensions
from hokma.kit.settings import build_settings
from hokma.kit.sync import sync_kit
from hokma.rpc.ws import SESSION_TOKEN
from hokma.update import archive
from hokma.update.runner import UpdateError, run_update, write_pointer
from hokma.update.source import ManifestSource, parse_version, validate_manifest


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "work").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOKMA_HOME", str(tmp_path / "work"))
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("HOKMA_UPDATE_SOURCE", raising=False)
    return tmp_path


# ---- version manifest ---------------------------------------------------------------------

GOOD = {"version": "0.2.0", "url": "hokma.tar.gz", "sha256": "a" * 64}


def test_manifest_validation(tmp_path):
    m = validate_manifest(GOOD, str(tmp_path / "manifest.json"))
    assert m.version == "0.2.0" and m.url == str(tmp_path / "hokma.tar.gz") and m.sha256 == "a" * 64
    assert validate_manifest(GOOD, "https://x.test/v/manifest.json").url == "https://x.test/v/hokma.tar.gz"
    for bad in ({**GOOD, "sha256": "zz"}, {**GOOD, "version": "1.2"}, {**GOOD, "url": "http://x/y.tgz"},
                {"version": "1.0.0"}, [], {**GOOD, "url": ""}):
        with pytest.raises(UpdateError):
            validate_manifest(bad, str(tmp_path / "m.json"))
    assert parse_version("1.10.0") > parse_version("1.9.9")


def test_source_rejects_plain_http_and_missing_file(tmp_path):
    for ref in ("http://example.test/m.json", str(tmp_path / "nope.json")):
        with pytest.raises(UpdateError):
            ManifestSource(ref).fetch()


# ---- archives -----------------------------------------------------------------------------

def _tar(path, members):
    with tarfile.open(path, "w:gz") as t:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "link":
                info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
                t.addfile(info)
            else:
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))


def test_bad_sha256_is_rejected_before_unpacking(tmp_path):
    pkg = tmp_path / "p.tar.gz"
    _tar(pkg, [("pyproject.toml", b"x", "file")])
    dest = tmp_path / "out.pkg"
    with pytest.raises(UpdateError, match="sha256"):
        archive.fetch_archive(str(pkg), dest, "0" * 64)
    assert not dest.exists()
    archive.fetch_archive(str(pkg), dest, hashlib.sha256(pkg.read_bytes()).hexdigest())
    assert dest.exists()


@pytest.mark.parametrize("members", [
    [("../evil.txt", b"x", "file")],
    [("/abs/evil.txt", b"x", "file")],
    [("pyproject.toml", b"x", "file"), ("link", b"", "link")],
])
def test_dangerous_tar_is_rejected(tmp_path, members):
    pkg = tmp_path / "p.tar.gz"
    _tar(pkg, members)
    with pytest.raises(UpdateError):
        archive.extract(pkg, tmp_path / "x")
    assert not (tmp_path / "evil.txt").exists()


def test_dangerous_zip_is_rejected(tmp_path):
    pkg = tmp_path / "p.zip"
    with zipfile.ZipFile(pkg, "w") as z:
        z.writestr("../evil.txt", "x")
    with pytest.raises(UpdateError):
        archive.extract(pkg, tmp_path / "x")


def test_archive_without_expected_structure_is_rejected(tmp_path):
    pkg = tmp_path / "p.tar.gz"
    _tar(pkg, [("pyproject.toml", b"x", "file")])
    with pytest.raises(UpdateError, match=before("update.package_structure", "missing")):
        archive.extract(pkg, tmp_path / "x")


# ---- atomic pointer / not installed ---------------------------------------------------------

def test_pointer_flip_is_a_single_replace(env):
    (env / "data").mkdir()
    write_pointer("a")
    assert config.current_slot() == "a"
    write_pointer("b")
    assert config.current_slot() == "b" and not (env / "data" / "current.tmp").exists()


def test_update_refuses_when_not_installed(env):
    with pytest.raises(UpdateError, match=re.escape(t("update.not_installed"))):
        run_update(str(env / "m.json"))


# ---- kit sync -----------------------------------------------------------------------------

def _git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                          capture_output=True, text=True, check=True).stdout


@pytest.fixture
def kit(env):
    work = env / "work"
    _git(work, "init", "-q")
    tpl = env / "tpl"
    (tpl / "agents").mkdir(parents=True)
    (tpl / ".hokma").mkdir()
    (tpl / "CLAUDE.md").write_text("kit v1\n")
    (tpl / "agents" / "base.md").write_text("agent v1\n")
    (tpl / ".hokma" / "extensions.json").write_text('{"enabledPlugins": {}, "extraKnownMarketplaces": {}}\n')
    return work, tpl


def test_kit_first_install_then_clean_update_then_conflict(kit):
    work, tpl = kit
    r1 = sync_kit(tpl, version="1.0.0", workdir=work)
    assert sorted(r1.added) == [".hokma/extensions.json", "CLAUDE.md", "agents/base.md"]

    # kit changes agents/base.md (student untouched) and CLAUDE.md (student edited it) and adds a file
    (tpl / "agents" / "base.md").write_text("agent v2\n")
    (tpl / "CLAUDE.md").write_text("kit v2\n")
    (tpl / "new.md").write_text("new\n")
    (work / "CLAUDE.md").write_text("kit v1\nmy notes\n")
    (work / "agents" / "mine.md").write_text("my agent\n")          # student's own, never in the kit
    r2 = sync_kit(tpl, version="2.0.0", workdir=work)
    assert r2.updated == ["agents/base.md"] and r2.added == ["new.md"] and r2.conflicts == ["CLAUDE.md"]
    assert (work / "agents" / "base.md").read_text() == "agent v2\n"
    assert (work / "CLAUDE.md").read_text() == "kit v1\nmy notes\n"           # student's version kept
    assert (work / "CLAUDE.md.hokma-new").read_text() == "kit v2\n"           # new one beside it
    assert (work / "agents" / "mine.md").read_text() == "my agent\n"

    # the same kit again: nothing to do, no new conflict noise
    r3 = sync_kit(tpl, version="2.0.0", workdir=work)
    assert (r3.added, r3.updated, r3.conflicts) == ([], [], [])


def test_kit_respects_deleted_files_and_owns_nothing_of_extensions(kit):
    work, tpl = kit
    sync_kit(tpl, version="1.0.0", workdir=work)
    (work / "agents" / "base.md").unlink()                                      # student removed it
    (work / ".hokma" / "extensions.json").write_text('{"enabledPlugins": {"a@b": true}}\n')
    (tpl / "agents" / "base.md").write_text("agent v2\n")
    (tpl / ".hokma" / "extensions.json").write_text('{"enabledPlugins": {"x@y": true}}\n')
    r = sync_kit(tpl, version="2.0.0", workdir=work)
    assert not (work / "agents" / "base.md").exists() and r.updated == [] and r.conflicts == []
    assert json.loads((work / ".hokma" / "extensions.json").read_text()) == {"enabledPlugins": {"a@b": True}}


# ---- extensions: locks always win ------------------------------------------------------------

def test_extensions_merge_and_cannot_disable_locks(env):
    ext = env / "data" / "extensions.json"
    ext.parent.mkdir()
    ext.write_text(json.dumps({
        "enabledPlugins": {"good@market": True, "bad plugin": True, "x@y": "yes"},
        "extraKnownMarketplaces": {"market": {"source": {"source": "github", "repo": "o/r"}},
                                   "evil": {"source": {"source": "shell", "cmd": "rm"}}},
        "permissions": {"deny": [], "allow": ["Bash(*)"]},
        "sandbox": {"enabled": False},
        "hooks": {},
        "env": {"X": "1"},
    }))
    s = build_settings()
    assert s["enabledPlugins"] == {"good@market": True}
    assert list(s["extraKnownMarketplaces"]) == ["market"]
    assert s["permissions"] == {"allow": ["mcp__hokma__trash_move"]}   # not the student's
    assert "sandbox" not in s and s["hooks"]["PreToolUse"] and "env" not in s
    _, problems = extensions.load()
    assert any("permissions" in p for p in problems) and any("evil" in p for p in problems)


def test_extensions_file_broken_is_ignored_not_fatal(env):
    ext = env / "data" / "extensions.json"
    ext.parent.mkdir()
    ext.write_text("{not json")
    s = build_settings()
    assert "enabledPlugins" not in s and s["hooks"]["PreToolUse"]


def test_ext_commands_validate_and_persist(env):
    extensions.set_plugin("p@m", True)
    extensions.add_marketplace("m", {"source": "github", "repo": "o/r"})
    s = build_settings()
    assert s["enabledPlugins"] == {"p@m": True} and "m" in s["extraKnownMarketplaces"]
    extensions.set_plugin("p@m", False)
    assert build_settings()["enabledPlugins"] == {"p@m": False}
    with pytest.raises(ValueError):
        extensions.set_plugin("no-at-sign", True)
    with pytest.raises(ValueError):
        extensions.add_marketplace("m", {"source": "shell"})


def test_extensions_file_is_not_protected_from_the_agent_any_more(env):
    from hokma.kit.settings import protected_paths
    assert config.workdir_hokma_dir() not in protected_paths()
    assert "sandbox" not in build_settings()


# ---- update endpoint ----------------------------------------------------------------------

def test_update_endpoint_needs_a_token_and_takes_no_source_from_the_request(env, monkeypatch):
    from hokma.update import api as update_api

    seen = []

    def check(source=None, force=False):
        seen.append(source)
        return None                                          # up to date: nothing is launched

    monkeypatch.setattr(update_api.detached, "check_newer", check)
    c = TestClient(create_app())
    assert c.post("/api/update").status_code == 401
    assert c.post("/api/update", headers={"X-Hokma-Token": "wrong"}).status_code == 401
    assert seen == []
    # no configured source is needed any more: the default is the latest GitHub Release
    r = c.post("/api/update", headers={"X-Hokma-Token": SESSION_TOKEN})
    assert r.status_code == 200 and r.json()["status"] == "up-to-date"
    # a source cannot be injected through the request
    r = c.post("/api/update", headers={"X-Hokma-Token": SESSION_TOKEN}, json={"source": "https://evil.test/m.json"})
    assert r.status_code == 200
    assert seen == [None, None]


def test_the_cli_error_shown_to_the_student_has_no_internal_tag():
    from hokma.i18n import t
    from hokma.update.api import clean_cli_error

    raw = f"{t('cli.tag.warn')} careful\n{t('cli.tag.fail')} Not updated: no network. You are still on 0.0.6.\n"
    cleaned = clean_cli_error(raw)
    assert "[FAIL]" not in cleaned and "[warn]" not in cleaned
    assert cleaned.endswith("Not updated: no network. You are still on 0.0.6.")
    assert clean_cli_error("plain message") == "plain message" and clean_cli_error("") == ""
