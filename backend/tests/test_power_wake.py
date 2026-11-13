"""P2 (SPEC-011): wake-up task XML and commands, the single-wake logic and the /api/power state. The real
Task Scheduler is never touched: the runner, the registry and powercfg are fakes."""
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import pytest
from dbsupport import env  # noqa: F401  (fixture)
from fastapi.testclient import TestClient

from hokma.core.app import create_app
from hokma.db import get_database, migrate
from hokma.power import keepawake, wake, wake_windows
from hokma.rpc.ws import SESSION_TOKEN

HEADERS = {"X-Hokma-Token": SESSION_TOKEN}
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
AT = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


class Result:
    def __init__(self, code=0, out=""):
        self.returncode, self.stdout = code, out


class Runner:
    def __init__(self, code=0, powercfg="0x00000000 0x00000001 0x00000001 0x00000001"):
        self.calls, self.code, self.powercfg, self.xml = [], code, powercfg, None

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if argv[0] == "powercfg":
            return Result(0, self.powercfg)
        if argv[1] == "/Create":
            self.xml = open(argv[argv.index("/XML") + 1], encoding="utf-16").read()
        return Result(self.code)


class Reg:
    def __init__(self, cs):
        self.cs = cs
        self.HKEY_LOCAL_MACHINE = 0

    def OpenKey(self, *a):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def QueryValueEx(self, key, name):
        if self.cs is None:
            raise OSError
        return (self.cs, 4)


def test_xml_has_wake_to_run_interactive_logon_and_one_trigger():
    root = ET.fromstring(wake_windows.build_xml(AT, "C:\\py\\pythonw.exe").replace('encoding="UTF-16"', ""))
    assert root.find("t:Settings/t:WakeToRun", NS).text == "true"
    assert root.find("t:Principals/t:Principal/t:LogonType", NS).text == "InteractiveToken"
    assert root.find("t:Principals/t:Principal/t:RunLevel", NS).text == "LeastPrivilege"
    assert len(root.findall("t:Triggers/t:TimeTrigger", NS)) == 1
    start = root.find("t:Triggers/t:TimeTrigger/t:StartBoundary", NS).text
    assert datetime.fromisoformat(start) == AT.astimezone().replace(tzinfo=None)
    assert root.find("t:Actions/t:Exec/t:Command", NS).text == "C:\\py\\pythonw.exe"


def test_commands_and_no_console_flag(monkeypatch):
    assert wake_windows.create_argv("a.xml") == ["schtasks", "/Create", "/TN", "HokmaWake", "/XML", "a.xml", "/F"]
    assert wake_windows.delete_argv() == ["schtasks", "/Delete", "/TN", "HokmaWake", "/F"]
    monkeypatch.setattr("sys.platform", "win32")
    run = Runner()
    backend = wake_windows.WindowsWake(run, Reg(0))
    assert backend.schedule(AT) is True
    argv, kw = run.calls[-1]
    assert kw["creationflags"] & 0x08000000                     # CREATE_NO_WINDOW
    assert "<WakeToRun>true</WakeToRun>" in run.xml
    backend.cancel()
    assert run.calls[-1][0] == wake_windows.delete_argv()
    assert wake_windows.WindowsWake(Runner(code=1), Reg(0)).schedule(AT) is False


def test_check_modern_standby_timers_off_and_ok():
    assert wake_windows.WindowsWake(Runner(), Reg(1)).check() == "modern_standby"
    off = Runner(powercfg="Minimum: 0x00000000 Maximum: 0x00000002 AC: 0x00000000 DC: 0x00000000")
    assert wake_windows.WindowsWake(off, Reg(None)).check() == "timers_off"
    assert wake_windows.WindowsWake(Runner(powercfg="AC: 0x00000001 DC: 0x00000000"), Reg(0)).check() is None


class Fake:
    def __init__(self, reason=None, ok=True):
        self.reason, self.ok, self.log = reason, ok, []

    def check(self):
        return self.reason

    def schedule(self, at):
        self.log.append(("schedule", at))
        return self.ok

    def cancel(self):
        self.log.append(("cancel",))


def test_waker_keeps_one_wake_and_cancels_when_none():
    fake = Fake()
    w = wake.Waker(fake)
    assert w.sync(True, AT) == "ok" and w.at == AT
    w.sync(True, AT)                                            # unchanged: no second call
    assert fake.log == [("schedule", AT)]
    later = AT + timedelta(hours=1)
    w.sync(True, later)
    assert fake.log[-1] == ("schedule", later)                  # replaced, still one
    assert w.sync(True, None) == "ok" and fake.log[-1] == ("cancel",) and w.at is None
    assert w.sync(False, None) == "off" and w.info()["message"] == ""


@pytest.mark.parametrize("fake,status", [(Fake(reason="modern_standby"), "unsupported"),
                                          (Fake(reason="platform"), "unsupported"),
                                          (Fake(ok=False), "needs_permission")])
def test_waker_states_and_messages(fake, status):
    w = wake.Waker(fake)
    assert w.sync(True, AT) == status
    assert w.info()["status"] == status and w.info()["message"]
    assert not any(c[0] == "schedule" for c in fake.log if fake.reason)


def test_macos_and_linux_are_unsupported():
    assert wake.platform_backend("darwin").check() == "platform"
    assert wake.platform_backend("linux").check() == "platform"
    assert wake.Waker(wake.platform_backend("darwin")).sync(True, AT) == "unsupported"


def test_next_wake_is_two_minutes_before_the_nearest_approved_job(env):
    migrate()
    db = get_database()
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    with db.write() as u:
        u.profile.save(owner_name="Ana")
        agent = u.agents.list()[0].slug if u.agents.list() else None
    if agent is None:
        pytest.skip("no agent seeded")
    with db.write() as u:
        for name, hours, approved in (("near", 5, True), ("far", 9, True), ("unapproved", 1, False), ("past", -3, True)):
            appr = u.approvals.record("scheduled_job", 1, "approved", decided_by="Ana", channel="dashboard").id if approved else None
            u.scheduled_jobs.create(name, agent, "x", "interval", "3600", "UTC", is_active=True, approval_id=appr,
                                    next_run_at=now + timedelta(hours=hours))
    assert wake.next_wake(db, now) == now + timedelta(hours=5) - wake.LEAD
    assert wake.next_wake(db, now + timedelta(days=1)) is None


def test_api_exposes_wake_state(env, monkeypatch):
    migrate()
    fake = Fake(reason="modern_standby")
    monkeypatch.setattr(wake, "_instance", {"waker": wake.Waker(fake)})
    monkeypatch.setattr(keepawake, "_instance", {"keep_awake": keepawake.KeepAwake(keepawake.NullBlocker())})
    with TestClient(create_app()) as client:
        with get_database().write() as u:
            u.profile.save(owner_name="Ana")
        assert client.get("/api/power", headers=HEADERS).json()["wake"]["status"] == "off"
        body = client.put("/api/power", json={"wake_enabled": True}, headers=HEADERS).json()
        assert body["wake"]["status"] == "unsupported" and body["wake"]["message"]
        assert fake.log == []
