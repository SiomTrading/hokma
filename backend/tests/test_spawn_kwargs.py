import subprocess
import sys

from hokma.core import proc
from hokma.core.proc import CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW, DETACHED_PROCESS, spawn_kwargs


def test_windows_flags(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert spawn_kwargs() == {"creationflags": CREATE_NO_WINDOW}
    assert spawn_kwargs(extra_flags=CREATE_NEW_PROCESS_GROUP) == {
        "creationflags": CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP}
    assert spawn_kwargs(detached=True) == {
        "creationflags": DETACHED_PROCESS | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP}
    assert spawn_kwargs(detached=True, extra_flags=0x1000)["creationflags"] & 0x1000


def test_posix_flags(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert spawn_kwargs() == {}
    assert spawn_kwargs(extra_flags=CREATE_NEW_PROCESS_GROUP) == {}
    assert spawn_kwargs(detached=True) == {"start_new_session": True}
    monkeypatch.setattr(sys, "platform", "darwin")
    assert spawn_kwargs() == {}


def test_constants_match_windows_values():
    assert (CREATE_NO_WINDOW, CREATE_NEW_PROCESS_GROUP, DETACHED_PROCESS) == (0x08000000, 0x200, 0x8)


def test_posix_kwargs_are_accepted_by_subprocess():
    assert subprocess.run([sys.executable, "-c", "pass"], **spawn_kwargs(detached=True)).returncode == 0
