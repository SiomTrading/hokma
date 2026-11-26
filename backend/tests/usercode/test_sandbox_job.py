"""The limits of the Job Object of Windows (SPEC-031 section 6.3). The structures and the limits are checked on any platform; creating the job only on Windows."""
import ctypes
import sys

import pytest

from hokma.core import winjob


def test_the_structures_have_the_layout_of_windows_64():
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        pytest.skip("layout of 64 bit Windows")
    assert ctypes.sizeof(winjob.BasicLimitInformation) == 64
    assert ctypes.sizeof(winjob.ExtendedLimitInformation) == 144
    assert ctypes.sizeof(winjob.BasicUiRestrictions) == 4


def test_the_limits_of_the_sandbox_job_are_the_ones_of_the_spec():
    info = winjob.sandbox_limit_info(memory_bytes=768 * 1024 * 1024, cpu_seconds=90)
    flags = info.BasicLimitInformation.LimitFlags
    for flag in (winjob.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, winjob.JOB_OBJECT_LIMIT_ACTIVE_PROCESS, winjob.JOB_OBJECT_LIMIT_PROCESS_MEMORY,
                 winjob.JOB_OBJECT_LIMIT_PROCESS_TIME, winjob.JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION):
        assert flags & flag
    assert not flags & winjob.JOB_OBJECT_LIMIT_BREAKAWAY_OK and not flags & winjob.JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
    assert info.BasicLimitInformation.ActiveProcessLimit == 1
    assert info.ProcessMemoryLimit == 768 * 1024 * 1024
    assert info.BasicLimitInformation.PerProcessUserTimeLimit == 90 * 10_000_000          # units of 100 nanoseconds


def test_the_interface_restrictions_close_the_clipboard_the_desktop_and_the_system_settings():
    mask = winjob.SANDBOX_UI_LIMITS
    for flag in (winjob.JOB_OBJECT_UILIMIT_HANDLES, winjob.JOB_OBJECT_UILIMIT_READCLIPBOARD, winjob.JOB_OBJECT_UILIMIT_WRITECLIPBOARD,
                 winjob.JOB_OBJECT_UILIMIT_SYSTEMPARAMETERS, winjob.JOB_OBJECT_UILIMIT_DISPLAYSETTINGS, winjob.JOB_OBJECT_UILIMIT_GLOBALATOMS,
                 winjob.JOB_OBJECT_UILIMIT_DESKTOP, winjob.JOB_OBJECT_UILIMIT_EXITWINDOWS):
        assert mask & flag
    assert winjob.JOB_OBJECT_UILIMIT_HANDLES == 0x1 and winjob.JOB_OBJECT_UILIMIT_EXITWINDOWS == 0x80


def test_the_older_jobs_keep_their_flags():
    assert winjob.kill_on_close_info().BasicLimitInformation.LimitFlags == winjob.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | winjob.JOB_OBJECT_LIMIT_BREAKAWAY_OK


@pytest.mark.skipif(sys.platform == "win32", reason="the refusal is for the other platforms")
def test_the_sandbox_job_exists_only_on_windows():
    with pytest.raises(OSError):
        winjob.SandboxJob()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only: it needs a real Job Object (this suite was NOT run on Windows by the author)")
def test_a_sandbox_job_takes_a_process_and_ends_it_with_terminate():
    import subprocess

    job = winjob.SandboxJob()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        job.assign(int(proc._handle))                          # noqa: SLF001  (the handle of the child, as the sandbox does)
        job.terminate()
        proc.wait(timeout=5)
        assert proc.returncode is not None
    finally:
        proc.kill()
        job.close()
