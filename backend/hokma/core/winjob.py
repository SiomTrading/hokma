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
JOB_OBJECT_BASIC_UI_RESTRICTIONS_CLASS = 4
JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
JOB_OBJECT_UILIMIT_HANDLES = 0x00000001
JOB_OBJECT_UILIMIT_READCLIPBOARD = 0x00000002
JOB_OBJECT_UILIMIT_WRITECLIPBOARD = 0x00000004
JOB_OBJECT_UILIMIT_SYSTEMPARAMETERS = 0x00000008
JOB_OBJECT_UILIMIT_DISPLAYSETTINGS = 0x00000010
JOB_OBJECT_UILIMIT_GLOBALATOMS = 0x00000020
JOB_OBJECT_UILIMIT_DESKTOP = 0x00000040
JOB_OBJECT_UILIMIT_EXITWINDOWS = 0x00000080
SANDBOX_UI_LIMITS = (JOB_OBJECT_UILIMIT_HANDLES | JOB_OBJECT_UILIMIT_READCLIPBOARD | JOB_OBJECT_UILIMIT_WRITECLIPBOARD | JOB_OBJECT_UILIMIT_SYSTEMPARAMETERS
                     | JOB_OBJECT_UILIMIT_DISPLAYSETTINGS | JOB_OBJECT_UILIMIT_GLOBALATOMS | JOB_OBJECT_UILIMIT_DESKTOP | JOB_OBJECT_UILIMIT_EXITWINDOWS)


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


class BasicUiRestrictions(ctypes.Structure):
    _fields_ = [("UIRestrictionsClass", ctypes.c_uint32)]


def sandbox_limit_info(*, memory_bytes: int, cpu_seconds: int) -> ExtendedLimitInformation:
    """The limits of the job of the isolated process that runs generated code (SPEC-031 6.3): it dies with Hokma, it is the only process of the job,
    it cannot use more memory or CPU time than its limits, and a crash does not open a dialog. No breakaway: nothing leaves the job."""
    info = ExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_ACTIVE_PROCESS | JOB_OBJECT_LIMIT_PROCESS_MEMORY
                                             | JOB_OBJECT_LIMIT_PROCESS_TIME | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION)
    info.BasicLimitInformation.ActiveProcessLimit = 1
    info.BasicLimitInformation.PerProcessUserTimeLimit = int(cpu_seconds) * 10_000_000          # units of 100 nanoseconds
    info.ProcessMemoryLimit = int(memory_bytes)
    return info


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


class TreeJob:
    """One job per process tree Hokma starts for a conversation or a command (task 3175).

    Every process the child starts joins the job on its own (git, uv, a shell and what that shell starts), so
    `terminate()` ends the whole tree in one call, even the grandchildren whose parent already died, which
    `taskkill /T` no longer finds. KILL_ON_JOB_CLOSE also ends the tree if Hokma itself goes away. Nested jobs need
    Windows 8 or later; the engine process already runs inside the supervisor's job."""

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
        k.TerminateJobObject.restype = wintypes.BOOL
        k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        self._handle = k.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(self._handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                                         ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.get_last_error()
            k.CloseHandle(self._handle)
            self._handle = None
            raise ctypes.WinError(error)

    def assign(self, process_handle: int) -> None:
        if not self._kernel.AssignProcessToJobObject(self._handle, int(process_handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def terminate(self, exit_code: int = 1) -> None:
        if self._handle and not self._kernel.TerminateJobObject(self._handle, exit_code):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self._handle:
            self._kernel.CloseHandle(self._handle)
            self._handle = None


class SandboxJob(TreeJob):
    """The job of ONE isolated process that runs generated code (SPEC-031 6.3): limits of memory, CPU time and processes, and the restrictions of
    the interface (clipboard, desktop, system settings). `assign` must succeed BEFORE the parent sends the first line of generated code: if it fails,
    no generated code runs on this computer. Nothing here was run on Windows by its author: the tests of test_sandbox_job.py and test_sandbox_windows.py are
    marked for Windows and must be run there."""

    def __init__(self, memory_bytes: int = 768 * 1024 * 1024, cpu_seconds: int = 90) -> None:
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
        k.TerminateJobObject.restype = wintypes.BOOL
        k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        self._handle = k.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = sandbox_limit_info(memory_bytes=memory_bytes, cpu_seconds=cpu_seconds)
        ui = BasicUiRestrictions(SANDBOX_UI_LIMITS)
        ok = (k.SetInformationJobObject(self._handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(info), ctypes.sizeof(info))
              and k.SetInformationJobObject(self._handle, JOB_OBJECT_BASIC_UI_RESTRICTIONS_CLASS, ctypes.byref(ui), ctypes.sizeof(ui)))
        if not ok:
            error = ctypes.get_last_error()
            k.CloseHandle(self._handle)
            self._handle = None
            raise ctypes.WinError(error)
