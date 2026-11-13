"""P1 (SPEC-011): keep-awake blocker, preference and API. Nothing here touches the real power state."""

import pytest
from dbsupport import env  # noqa: F401  (fixture)
from fastapi.testclient import TestClient

from hokma.core.app import create_app
from hokma.db import get_database, migrate
from hokma.power import keepawake, wake
from hokma.power.api import apply_preference
from hokma.rpc.ws import SESSION_TOKEN

HEADERS = {"X-Hokma-Token": SESSION_TOKEN}


class FakeKernel:
    def __init__(self):
        self.calls = []

    def SetThreadExecutionState(self, flags):
        self.calls.append(flags)
        return 1


class FakeProc:
    def __init__(self):
        self.alive, self.terminated = True, False

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.terminated, self.alive = True, False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.alive = False


def test_windows_blocker_requests_system_required_and_releases():
    kernel = FakeKernel()
    blocker = keepawake.WindowsBlocker(kernel)
    assert blocker.start() and blocker.is_started()
    assert kernel.calls == [keepawake.ES_CONTINUOUS | keepawake.ES_SYSTEM_REQUIRED]
    blocker.stop()
    assert not blocker.is_started() and kernel.calls[-1] == keepawake.ES_CONTINUOUS


def test_mac_blocker_runs_caffeinate_tied_to_this_process_and_ends_it(monkeypatch):
    seen, proc = [], FakeProc()

    def popen(args, **kwargs):
        seen.append(args)
        return proc

    blocker = keepawake.MacBlocker(popen)
    assert blocker.start() and blocker.is_started()
    assert seen[0][:3] == ["caffeinate", "-i", "-w"] and seen[0][3].isdigit()
    blocker.stop()
    assert proc.terminated and not blocker.is_started()


def test_mac_blocker_without_caffeinate_stays_off():
    def popen(args, **kwargs):
        raise FileNotFoundError("caffeinate")

    assert not keepawake.MacBlocker(popen).start()


def test_keep_awake_is_idempotent_and_follows_preference_and_work():
    kernel = FakeKernel()
    keep = keepawake.KeepAwake(keepawake.WindowsBlocker(kernel))
    assert keep.set(True) and keep.set(True) and len(kernel.calls) == 1
    assert not keep.set(False) and not keep.is_active()
    assert not keep.sync(True, False) and not keep.sync(False, True)
    assert keep.sync(True, True)
    assert not keep.sync(True, False)


def test_platform_choice():
    assert isinstance(keepawake.platform_blocker("win32"), keepawake.WindowsBlocker)
    assert isinstance(keepawake.platform_blocker("darwin"), keepawake.MacBlocker)
    assert isinstance(keepawake.platform_blocker("linux"), keepawake.NullBlocker)
    assert keepawake.supported("win32") and not keepawake.supported("linux")


def test_migration_adds_both_flags_off(env):
    migrate()
    db = get_database()
    with db.write() as u:
        u.profile.save(owner_name="Ana")
        profile = u.profile.get()
    assert profile.keep_awake is False and profile.wake_enabled is False


@pytest.fixture
def fake_keep(monkeypatch):
    kernel = FakeKernel()
    keep = keepawake.KeepAwake(keepawake.WindowsBlocker(kernel))
    monkeypatch.setattr(keepawake, "_instance", {"keep_awake": keep})
    monkeypatch.setattr(wake, "_instance", {"waker": wake.Waker(wake.NullWake())})  # never the real scheduler
    yield keep
    keep.set(False)


def test_apply_preference_holds_only_with_flag_and_work(env, fake_keep):
    migrate()
    db = get_database()
    with db.write() as u:
        u.profile.save(owner_name="Ana")
    assert not apply_preference(db, lambda: True)             # preference off: never held
    with db.write() as u:
        u.profile.save(keep_awake=True)
    assert apply_preference(db, lambda: True)
    assert not apply_preference(db, lambda: False)            # work ended: released
    assert apply_preference(db, lambda: True)
    with db.write() as u:
        u.profile.save(keep_awake=False)
    assert not apply_preference(db, lambda: True)             # preference turned off: released


def test_api_needs_token_and_a_profile(env, fake_keep):
    migrate()
    with TestClient(create_app()) as client:
        assert client.get("/api/power").status_code == 401
        assert client.put("/api/power", json={"keep_awake": True}, headers={"X-Hokma-Token": "bad"}).status_code == 401
        no_profile = client.put("/api/power", json={"keep_awake": True}, headers=HEADERS)
        assert no_profile.status_code == 409 and no_profile.json()["detail"]["code"] == "no_profile"
        assert client.get("/api/power", headers=HEADERS).json()["keep_awake"] is False


def test_api_saves_the_flags_and_holds_while_busy(env, fake_keep):
    migrate()
    app = create_app()
    app.state.power_busy = lambda: True
    with TestClient(app) as client:
        with get_database().write() as u:
            u.profile.save(owner_name="Ana")
        first = client.get("/api/power", headers=HEADERS).json()
        assert {k: v for k, v in first.items() if k != "wake"} == {
            "keep_awake": False, "wake_enabled": False, "active": False, "supported": keepawake.supported()}
        on = client.put("/api/power", json={"keep_awake": True}, headers=HEADERS).json()
        assert on["keep_awake"] is True and on["active"] is True
        off = client.put("/api/power", json={"keep_awake": False, "wake_enabled": True}, headers=HEADERS).json()
        assert off["keep_awake"] is False and off["wake_enabled"] is True and off["active"] is False
        assert client.put("/api/power", json={"other": 1}, headers=HEADERS).status_code == 422
    assert not fake_keep.is_active()                           # shutdown released the block
