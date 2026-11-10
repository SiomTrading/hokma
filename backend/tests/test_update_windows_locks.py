"""Task 3136: updates on Windows when files are in use or the folder is synced (OneDrive, antivirus)."""
import types

import pytest

from hokma.core import config
from hokma.i18n import t
from hokma.kit import safefs
from hokma.update import runner
from hokma.update.runner import UpdateError, run_update, write_pointer


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "work").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOKMA_HOME", str(tmp_path / "work"))
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    (tmp_path / "data").mkdir()
    return tmp_path


class _Newer:
    def fetch(self):
        return types.SimpleNamespace(version="9.9.9", url="x", sha256="0" * 64, headers={})


def test_update_refuses_when_this_process_runs_in_the_slot_to_rebuild(env, monkeypatch):
    write_pointer("b")
    monkeypatch.setattr(runner, "running_slot", lambda: "a")     # a stale engine in the old slot
    monkeypatch.setattr(runner.shutil, "rmtree", lambda *a, **k: pytest.fail("must not delete a slot in use"))
    with pytest.raises(UpdateError, match=t("update.running_old_slot")[:30]):
        run_update(_Newer())
    assert config.current_slot() == "b"
    assert not (env / "data" / "update.lock").exists()


def test_inactive_slot_removal_waits_for_a_locked_file(env, monkeypatch):
    slot = env / "data" / "runtime" / "a"
    slot.mkdir(parents=True)
    calls, waits = [], []

    def flaky(path):
        calls.append(path)
        if len(calls) < 3:
            raise PermissionError(32, "in use")

    monkeypatch.setattr(runner.shutil, "rmtree", flaky)
    monkeypatch.setattr(runner.time, "sleep", waits.append)
    runner._remove_slot(slot)
    assert len(calls) == 3 and len(waits) == 2


def test_inactive_slot_still_locked_gives_a_plain_message(env, monkeypatch):
    slot = env / "data" / "runtime" / "a"
    slot.mkdir(parents=True)

    def locked(path):
        raise PermissionError(32, "in use")

    monkeypatch.setattr(runner.shutil, "rmtree", locked)
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    with pytest.raises(UpdateError) as info:
        runner._remove_slot(slot)
    assert str(slot) in str(info.value) and "PermissionError" not in str(info.value)


# ---- atomic writer, Windows variant (forced on any OS) -------------------------------------

@pytest.fixture
def generic(monkeypatch):
    monkeypatch.setattr(safefs, "_POSIX", False)
    monkeypatch.setattr(safefs.time, "sleep", lambda s: None)


def test_write_survives_the_folder_vanishing_once(tmp_path, generic, monkeypatch):
    real, state = safefs.Path.write_bytes, {"n": 0}

    def vanishing(self, data):
        state["n"] += 1
        if state["n"] == 1:
            raise FileNotFoundError(2, "gone", str(self))     # what OneDrive did to the student
        return real(self, data)

    monkeypatch.setattr(safefs.Path, "write_bytes", vanishing)
    safefs.write_atomic(tmp_path, "agents/axel.md", b"hola")
    assert (tmp_path / "agents" / "axel.md").read_bytes() == b"hola"
    assert [p.name for p in (tmp_path / "agents").iterdir()] == ["axel.md"]


def test_write_survives_a_locked_replace_with_a_new_temp_name(tmp_path, generic, monkeypatch):
    real, names = safefs.os.replace, []

    def locked_once(src, dst):
        names.append(safefs.Path(src).name)
        if len(names) == 1:
            raise PermissionError(32, "scanner has it open")
        return real(src, dst)

    monkeypatch.setattr(safefs.os, "replace", locked_once)
    safefs.write_atomic(tmp_path, "a.md", b"x")
    assert (tmp_path / "a.md").read_bytes() == b"x"
    assert len(names) == 2 and names[0] != names[1] and all(n.startswith(".a.md.hokma-tmp-") for n in names)
    assert [p.name for p in tmp_path.iterdir()] == ["a.md"]


def test_write_gives_up_after_the_attempts_and_leaves_no_temp(tmp_path, generic, monkeypatch):
    def always(src, dst):
        raise PermissionError(32, "locked")

    monkeypatch.setattr(safefs.os, "replace", always)
    with pytest.raises(PermissionError):
        safefs.write_atomic(tmp_path, "a.md", b"x")
    assert list(tmp_path.iterdir()) == []


def test_unsafe_path_is_not_retried(tmp_path, generic, monkeypatch):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    slept = []
    monkeypatch.setattr(safefs.time, "sleep", slept.append)
    with pytest.raises(safefs.UnsafePath):
        safefs.write_atomic(tmp_path, "link/a.md", b"x")
    assert slept == []
