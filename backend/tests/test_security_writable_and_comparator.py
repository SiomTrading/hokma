# One list of agent-writable places and one path comparator (guard.under_any) for the extension validators.
import os

import pytest

from hokma.kit import extensions, guard, writable
from hokma.kit.settings import build_settings
from hokma.kit.sync import sync_kit


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "work").mkdir()
    (tmp_path / "outside").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOKMA_HOME", str(tmp_path / "work"))
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return tmp_path


# ---- one list of agent-writable places, one comparator ------------------------------------------

def test_extension_validators_use_the_sandbox_allow_write_list(env):
    allow = build_settings()["sandbox"]["filesystem"]["allowWrite"]
    assert allow == [str(p) for p in writable.sandbox_allow_write()]
    roots = {str(p) for p in writable.agent_writable_roots()}
    assert set(allow) <= roots and set(map(str, writable.temp_dirs())) <= roots
    for place in allow:                                           # everything the sandbox can write is refused
        with pytest.raises(ValueError):
            extensions.validate_source({"source": "directory", "path": os.path.join(place, "plugins")})


def test_extension_list_follows_the_sandbox_list_when_it_grows(env, monkeypatch):
    extra = env / "extra-writable"
    extra.mkdir()
    monkeypatch.setattr(writable, "sandbox_allow_write", lambda: [env / "work", extra])
    assert str(extra) in build_settings()["sandbox"]["filesystem"]["allowWrite"]
    with pytest.raises(ValueError):
        extensions.validate_source({"source": "directory", "path": str(extra / "market")})


def test_extensions_use_the_guard_comparator(env, monkeypatch):
    calls = []
    real = guard.under_any
    monkeypatch.setattr(extensions, "under_any", lambda p, roots: calls.append((p, tuple(roots))) or real(p, roots))
    extensions.validate_source({"source": "directory", "path": "/usr/share/hokma-market"})
    assert calls and calls[0][0] == "/usr/share/hokma-market" and str(env / "work") in " ".join(calls[0][1])


def test_comparator_is_case_folded_where_the_fs_is_and_resolves_both_sides(env, monkeypatch, tmp_path):
    work = env / "work"
    monkeypatch.setattr(guard, "CASE_INSENSITIVE", True)
    upper = str(work).upper() + "/Plugins"
    assert guard.under_any(upper, [str(work)]) is True
    with pytest.raises(ValueError):
        extensions.validate_source({"source": "directory", "path": upper})
    monkeypatch.setattr(guard, "CASE_INSENSITIVE", False)
    assert guard.under_any(upper, [str(work)]) is False
    alias_root = env / "outside" / "alias-root"
    alias_root.symlink_to(work)                                   # the ROOT is reached through a symlink
    assert guard.under_any(str(work / "x"), [str(alias_root)]) is True
    assert guard.under_any("/usr/share/x", [str(work)]) is False


def test_comparator_catches_an_alias_by_inode_when_names_differ(env, monkeypatch):
    work = env / "work"
    alias = env / "outside" / "alias"
    alias.symlink_to(work)
    monkeypatch.setattr(guard.os.path, "realpath", lambda p: p)   # disable name resolution: only identity is left
    assert guard.under_any(str(alias / "not-created-yet"), [str(work)]) is True
