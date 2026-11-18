"""Optional native parts never stop the start (task 3288): a probe child that dies or hangs leaves the part off, the
answer is remembered until an update, safe mode turns every part off."""
import json
import sys

import pytest
from dbsupport import env  # noqa: F401  (fixture)

from hokma.core import nativeprobe
from hokma.db import vecext
from hokma.library import runtime


@pytest.fixture
def probing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HOKMA_NATIVE_PROBE", "on")
    nativeprobe.reset_for_tests()
    yield tmp_path
    nativeprobe.reset_for_tests()


def runner(results, calls):
    def run(part, timeout):
        calls.append(part)
        return results[part]
    return run


def test_a_child_that_ends_well_allows_the_part_and_is_not_asked_again(probing):
    calls = []
    run = runner({"sqlite_vec": (0, "")}, calls)
    assert nativeprobe.allowed("sqlite_vec", run) and nativeprobe.allowed("sqlite_vec", run)
    assert calls == ["sqlite_vec"]
    nativeprobe.reset_for_tests()                                        # a new process reads the file
    assert nativeprobe.allowed("sqlite_vec", run) and calls == ["sqlite_vec"]


def test_a_child_that_crashes_leaves_the_part_off_and_says_which_one(probing):
    assert not nativeprobe.allowed("embedder", runner({"embedder": (3221225477, "")}, []))
    log = (probing / "logs" / "engine.log").read_text()
    assert "native: native part off: embedder (ended with code 0xC0000005)" in log
    assert nativeprobe.off_parts() == [{"part": "embedder", "code": 3221225477, "reason": "crash"}]
    assert "Library" in nativeprobe.notices()[0] or "Biblioteca" in nativeprobe.notices()[0]


def test_a_child_that_hangs_leaves_the_part_off(probing):
    assert not nativeprobe.allowed("voice", runner({"voice": (None, "")}, []))
    assert "native part off: voice (timed out)" in (probing / "logs" / "engine.log").read_text()


def test_an_update_probes_again(probing, monkeypatch):
    calls = []
    assert not nativeprobe.allowed("sqlite_vec", runner({"sqlite_vec": (-11, "")}, calls))
    nativeprobe.reset_for_tests()
    assert not nativeprobe.allowed("sqlite_vec", runner({"sqlite_vec": (0, "")}, calls))     # remembered as off
    assert calls == ["sqlite_vec"]
    monkeypatch.setattr(nativeprobe, "_version", lambda: "9.9.9")                           # Hokma was updated
    nativeprobe.reset_for_tests()
    assert nativeprobe.allowed("sqlite_vec", runner({"sqlite_vec": (0, "")}, calls)) and len(calls) == 2


def test_safe_mode_turns_every_part_off_without_probing(probing):
    nativeprobe.set_safe_mode(True)
    calls = []
    assert not nativeprobe.allowed("sqlite_vec", runner({}, calls)) and calls == []
    nativeprobe.reset_for_tests()
    assert nativeprobe.safe_mode() and json.loads((probing / "native-probe.json").read_text())["safe_mode"]
    assert nativeprobe.summary()["safe_mode"] is True


def test_the_switch_allows_everything(probing, monkeypatch):
    monkeypatch.setenv("HOKMA_NATIVE_PROBE", "off")
    assert nativeprobe.allowed("embedder", runner({}, []))


def test_a_real_child_that_kills_its_own_process_is_off_and_the_start_goes_on(probing):
    """A fake native module that ends its process the way an access violation does (os._exit, no Python cleanup): the
    child is started with the same spawn the probe uses, its exit code is what the log names, and the part is off."""
    import subprocess

    from hokma.core.proc import spawn_kwargs

    def run_fake_native(part, timeout):
        done = subprocess.run([sys.executable, "-I", "-c", "import os; os._exit(139)"], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout, **spawn_kwargs())
        return done.returncode, ""

    assert not nativeprobe.allowed("sqlite_vec", run_fake_native)
    assert nativeprobe.off_parts()[0]["code"] == 139
    assert "native part off: sqlite_vec (ended with code 139)" in (probing / "logs" / "engine.log").read_text()


def test_run_child_reports_a_timeout_and_an_exit_code(monkeypatch):
    import subprocess

    def fake_run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert nativeprobe.run_child("voice", 0.1) == (None, "")


def test_the_probe_child_really_runs_the_smoke_test(monkeypatch):
    """No fake: a Python child loads sqlite-vec and answers 0 (skipped where the extension cannot be loaded)."""
    if not vecext.smoke().ok:
        pytest.skip("sqlite-vec cannot be loaded here")
    code, _ = nativeprobe.run_child("sqlite_vec", 60)
    assert code == 0


def test_sqlite_vec_stays_off_when_its_probe_says_so(probing, monkeypatch):
    monkeypatch.setattr(nativeprobe, "run_child", lambda part, timeout: (-11, ""))
    assert vecext.check_support().code == vecext.NATIVE_OFF
    import sqlite3
    assert vecext.load_into(sqlite3.connect(":memory:")) is False


def test_the_search_model_is_not_ready_while_its_probe_says_off(probing, monkeypatch):
    monkeypatch.setattr(runtime, "load_manifest", lambda: {"id": "m", "files": []})   # nothing to wait for on the disk
    monkeypatch.setattr(runtime, "model_directory", lambda: probing)
    monkeypatch.setattr(nativeprobe, "run_child", lambda part, timeout: (3221225477, ""))
    assert runtime.model_ready() is False
    monkeypatch.setattr(nativeprobe, "run_child", lambda part, timeout: (0, ""))
    assert runtime.model_ready() is False                                 # still the remembered answer until an update


def test_the_start_goes_on_with_the_vector_extension_off(env, monkeypatch):  # noqa: F811
    """The whole path of the student's start: database opened, vector store prepared, Library status read. With the
    sqlite-vec probe dead nothing raises, the status says why in plain words and the Library has no vectors to use."""
    from hokma.db import get_database, migrate, vectors

    monkeypatch.setenv("HOKMA_NATIVE_PROBE", "on")
    nativeprobe.reset_for_tests()
    monkeypatch.setattr(nativeprobe, "run_child", lambda part, timeout: (3221225477, ""))
    migrate()
    database = get_database()
    nativeprobe.probe_startup()
    status = vectors.prepare(database)
    assert not status.ok and status.code == vecext.NATIVE_OFF
    assert "searches by words only" in status.message
    assert "native part off: sqlite_vec (ended with code 0xC0000005)" in (env / "data" / "logs" / "engine.log").read_text()
    assert "sqlite_vec" in nativeprobe.summary()["off"]
    nativeprobe.reset_for_tests()


# ------------------------------------------------------------------------------------------------ B3: time is not a crash

def test_a_part_that_timed_out_is_probed_again_at_the_next_start_and_a_crash_is_not(probing):
    calls = []
    assert not nativeprobe.allowed("embedder", runner({"embedder": (None, "")}, calls))        # too slow
    assert not nativeprobe.allowed("voice", runner({"voice": (-11, "")}, calls))                # crashed
    assert calls == ["embedder", "voice"]
    nativeprobe.reset_for_tests()                                                               # the next start
    assert nativeprobe.allowed("embedder", runner({"embedder": (0, "")}, calls))               # asked again, now fine
    assert not nativeprobe.allowed("voice", runner({"voice": (0, "")}, calls))                  # still off, not asked
    assert calls == ["embedder", "voice", "embedder"]
    assert [item["part"] for item in nativeprobe.off_parts()] == ["voice"]


def test_a_timeout_is_still_off_for_the_rest_of_this_process(probing):
    calls = []
    assert not nativeprobe.allowed("embedder", runner({"embedder": (None, "")}, calls))
    assert not nativeprobe.allowed("embedder", runner({"embedder": (0, "")}, calls)) and calls == ["embedder"]


def test_the_embedder_gets_ninety_seconds():
    assert nativeprobe.TIMEOUT_SECONDS[nativeprobe.EMBEDDER] == 90.0


def test_clear_forgets_every_answer_and_safe_mode(probing):
    nativeprobe.allowed("voice", runner({"voice": (-11, "")}, []))
    nativeprobe.set_safe_mode(True)
    nativeprobe.clear()
    assert not (probing / "native-probe.json").exists()
    assert not nativeprobe.safe_mode() and nativeprobe.off_parts() == []


def test_the_try_again_route_clears_the_file_and_restarts_the_engine(probing):
    from fastapi.testclient import TestClient

    from hokma.core.app import create_app
    from hokma.rpc.ws import SESSION_TOKEN

    nativeprobe.allowed("voice", runner({"voice": (-11, "")}, []))
    assert (probing / "native-probe.json").exists()
    app = create_app()
    api = TestClient(app)
    assert api.post("/api/native/retry").status_code == 401
    answer = api.post("/api/native/retry", headers={"X-Hokma-Token": SESSION_TOKEN})
    assert answer.status_code == 200 and answer.json() == {"restarting": True}
    assert not (probing / "native-probe.json").exists()
    assert app.state.lifecycle.restarting
