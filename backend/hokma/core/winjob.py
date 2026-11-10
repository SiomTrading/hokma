"""Windows Job Object that kills its processes when the supervisor goes away.

`KILL_ON_JOB_CLOSE` makes Windows terminate every process of the job when the last handle to the job closes,
and the supervisor's own handle closes when the supervisor ends, however it ends (closed terminal, killed
process, crash). `BREAKAWAY_OK` lets one process leave on purpose: the detached updater, which must outlive the
supervisor it stops (it asks for it with CREATE_BREAKAWAY_FROM_JOB; nothing else does). Standard library only (ctypes). The structures are declared with fixed-size types so their
layout can be checked on any 64-bit platform; the calls only run on Windows.
"""
import ctypes
import sys

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800       # a child started with CREATE_BREAKAWAY_FROM_JOB may leave (the updater)
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9


class IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", BasicLimitInformation),
        ("IoInfo", IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def kill_on_close_info() -> ExtendedLimitInformation:
    info = ExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK
    return info


class KillOnCloseJob:
    """Create it once in the supervisor, `assign()` each engine process, keep the object alive until exit."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("job objects exist only on Windows")
        from ctypes import wintypes

        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        k = self._kernel
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        k.SetInformationJobObject.restype = wintypes.BOOL
        k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        k.AssignProcessToJobObject.restype = wintypes.BOOL
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        self._handle = k.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = kill_on_close_info()
        if not k.SetInformationJobObject(self._handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                                         ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.get_last_error()
            k.CloseHandle(self._handle)
            self._handle = None
            raise ctypes.WinError(error)

    def assign(self, process_handle: int) -> None:
        """Put a process (the handle of a `subprocess.Popen`) in the job."""
        if not self._kernel.AssignProcessToJobObject(self._handle, int(process_handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self._handle:
            self._kernel.CloseHandle(self._handle)
            self._handle = None
