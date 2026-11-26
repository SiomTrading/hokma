"""The isolated process: the only place where generated code runs (SPEC-031 sections 5.5 and 6).

Nothing here is imported by Hokma: the parent starts this module as its own interpreter (`python -I -X utf8 -m hokma.factory.usercode.worker`), with an
empty environment, in an empty folder, with no handle but the pipe. It talks by that pipe, one JSON line per message, 8 MB at most.

The order of the life of the process is the order of the layers:
1. Trusted phase: it imports math, numpy and user_api (the only modules a rule may use), takes private copies of the pipe, closes stdin and stdout to
   /dev/null and applies the layers of its platform: network namespace, no new privileges, death with the parent, its own process group, limits of the
   operating system, Landlock (no file access by path, Linux 5.13+ with the LSM on), then removes ctypes and the pidfd calls and last the audit hook, which cannot be removed from Python. The hook's policy and
   the state it reads live in closures, and this module leaves sys.modules before generated code runs (see `_make_hook`). seccomp was not added: it needs
   a BPF program per architecture and no dependency of the project writes one; Landlock covers the files and the network namespace covers the network. Then it says ready and WAITS: the parent puts it in the Job Object (Windows) before
   it sends a single line of generated code.
2. Load: it checks the code again with the static check (it does not trust its parent), compiles it and runs the module in a dictionary of its own
   whose builtins are the short list of the static check and whose importer gives back only the modules already loaded from that list.
3. Calls: one answer per message, always plain data. An exception of the rule comes back as data and the process lives. A violation of a layer (the
   hook, the importer, the memory) is written to the parent and the process ends IN THE SAME CALL that found it (`_make_report`): there is no exception left
   for the rule to catch and no flag for it to reset.

THE LAYERS, in the order in which they stop a rule (decision of the owner, 2026-11-25: the language stays Python, and the main wall is the static check):
 1. Static check (`static_check`, fixed rules: allowed names and attributes only, no underscore name, no closure, cell, frame or pidfd). It runs in the parent
    and again here. This is the main barrier.
 2. AI reviewer (Ben, session 3) reads the code before it can be approved.
 3. Approval of the owner before the strategy operates (`owner_reviewed`: the owner read the exact hash).
 4. Audit hook + restricted builtins + resolved importer. BEST EFFORT, an extra layer: it is Python inside the process it guards, and no more rounds of attack
    against it are planned. The importer holds only a read-only table of the allowed modules (no sys.modules, no report); ctypes is removed from the process
    (sys.modules, the numpy attribute that held it) and os.pidfd_open and signal.pidfd_send_signal are deleted before the hook goes on.
 5. Kernel (Linux only, independent of Python): Landlock (no file access by path, 5.13+ with the LSM on), network namespace, no_new_privs, death with the
    parent, its own process group, rlimits (memory, CPU, file size, descriptors), plus the parent's timeouts and RSS watch.
BY PLATFORM:
- Linux: layers 1 to 5. If a rule ever disabled the hook, Landlock still refuses every path and the namespace refuses the network; what it could still do is
  run Python inside the open process (compute, use the pipe, read what is in memory). When Landlock or the namespace is missing, `layers` says `hook_only`
  and `degraded` is true, and the parent can refuse to run a rule that way.
- macOS: layers 1 to 4 + rlimits (RLIMIT_AS only in part: the parent watches the RSS) + the parent's timeouts. NO kernel layer for files or network.
- Windows: layers 1 to 4 + Job Object (memory, CPU, no child processes) + the parent's timeouts. No kernel layer for files or network.
RESIDUAL RISK ACCEPTED: on macOS and Windows a rule that passes layers 1 to 3 and then defeats the hook reads files and opens sockets as the user that runs
Hokma. On every platform a bug in CPython or in a C extension (numpy) can escape the Python layers. A rule can also send a signal to a process of the same user
through a call that raises no audit event only if it reaches code the static check refuses. That risk is covered by layers 1 to 3, not by the hook.

The parent checks the hash of the code and validates every answer again: this process runs untrusted code, so what it says is untrusted too.
"""
from __future__ import annotations

import dataclasses
import json
import math
import os
import sys
import types
from typing import Any, Callable

import numpy as np

from hokma.factory import user_api
from hokma.factory.params import ParamError, ParamSchema, validate
from hokma.factory.usercode import static_check

MAX_MESSAGE = 8 * 1024 * 1024
MODULE_NAME = "hokma_user_rule"
USER_FILENAME = "<user_rule>"
ALLOWED_IMPORTS = frozenset({"math", "numpy", "__future__", "hokma.factory.user_api"})
LOOKBACK_MAX = static_check.LOOKBACK_MAX
MAX_INSTANCES = 16
ERROR_TEXT = 300
DEFAULT_AS_MB = 1536
DEFAULT_CPU_SECONDS = 90

METHODS = {"alpha": ("signal",), "filter": ("allows", "must_flatten"), "exit": ("levels", "manage")}
KWARGS = {"signal": (), "allows": ("side", "state"), "must_flatten": (), "levels": ("side",), "manage": ("side", "state")}


class Violation(BaseException):
    """A layer refused something. A BaseException so that a rule's `except Exception` does not swallow it; the state records it anyway."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.message = message


def _new_state() -> types.SimpleNamespace:
    """What the audit hook reads and the main loop writes: two flags. It is created by `main` and lives ONLY in locals and in the closure of the hook: no
    global of this module, no object in sys.modules and no closure of anything the rule can obtain points to it (the importer does not hold it). There is no
    "violation" field: a violation is not a state that can be read back and cleared, it is a message to the parent and the end of the process (see `_make_report`).
    A SimpleNamespace, not a class of ours: its methods cannot be patched."""
    return types.SimpleNamespace(loading=False, loaded=False)


# ---------------------------------------------------------------- how a violation ends the process

def _make_report(fd: int) -> Callable[[str, str], None]:
    """`report(code, text)`: write the violation to the parent on the private copy of the pipe and END THE PROCESS, in the same call that found it.

    Nothing is left for the rule to undo: no exception to catch, no flag to reset, no frame that outlives the call. Every name it uses is bound here as a
    local of this closure (C functions and exact types only), so patching `builtins`, a module or a class cannot change what it does. The text is
    escaped by the C function of the json module, so the line is valid JSON whatever the rule put in the name of the event."""
    from json.encoder import encode_basestring_ascii as quote

    write, leave, encode, head = os.write, os._exit, str.encode, b'{"violation":'
    middle, tail = b',"message":', b"}\n"

    def report(code: str, text: str) -> None:
        line = head + encode(quote(code), "ascii") + middle + encode(quote(text), "ascii") + tail
        try:
            write(fd, line)
        finally:
            leave(3)

    return report


def _make_fire(report: Callable[[str, str], None] | None) -> Callable[[str, str], None]:
    """`fire(code, text)`: report the violation (which ends the process) and, only if `report` came back (the tests build a hook without one), raise it."""
    violation_type, limit = Violation, ERROR_TEXT

    def fire(code: str, text: str) -> None:
        text = text[:limit]
        if report is not None:
            report(code, text)
        raise violation_type(code, text)

    return fire


def _trusted_codes() -> tuple[Any, ...]:
    """The code objects the rule's load may pass through while it builds its dataclass and the worker checks it: the ones the worker itself calls, named one by
    one (no module is trusted as a whole). A function that a newer Python renames is simply not in the list, and the load fails loudly."""
    import ast
    import dataclasses
    import inspect

    found: list[Any] = []
    for module, names in ((dataclasses, ("_create_fn", "_cmp_fn", "_frozen_get_del_attr", "_hash_fn", "_hash_add", "_init_fn", "_repr_fn", "_process_class")),
                          (inspect, ("_signature_from_function", "_signature_from_callable", "signature")),
                          (ast, ("parse",)), (static_check, ("check", "_check"))):
        for name in names:
            function = getattr(module, name, None)
            if isinstance(function, types.FunctionType):
                found.append(function.__code__)
    found.append(inspect.Signature.from_callable.__func__.__code__)                 # type: ignore[attr-defined]
    found.extend(c for c in dataclasses.dataclass.__code__.co_consts if isinstance(c, types.CodeType) and c.co_name == "wrap")
    return tuple(found)


# ---------------------------------------------------------------- audit hook

def _make_hook(state: types.SimpleNamespace, report: Callable[[str, str], None] | None = None) -> Callable[[str, tuple[Any, ...]], None]:
    """The audit hook, with its whole policy as locals of this closure.

    THE RULE: the code of the hook (`hook`, `classify`, `trusted_caller`, `fire`, `report`) depends on nothing the rule can modify. It reads no global of
    the module, no builtin by name (`len`, `isinstance`, `type` and the rest are bound below as locals, so patching the `builtins` module changes nothing),
    and it only operates on exact types (`str`, `tuple`, `frozenset`, frames) whose methods are C and cannot be replaced. A test walks the bytecode of all
    of it and fails on any LOAD_GLOBAL, LOAD_NAME or attribute that is not on a short list.
    Nothing that the rule can obtain holds the state either: the importer has no state and nothing in sys.modules points to this closure; the hook is held
    only by the interpreter (Python cannot list its audit hooks) and the frames cannot be reached (sys._getframe, gc and the frame attributes are events it
    refuses). A violation is not an exception that the rule can catch and undo: `report` writes it to the parent and ends the process in the same call.
    The hook never calls a method of an object the rule controls, because code that runs inside the hook runs with the hook off.
    """
    import _thread

    file_events = frozenset({
        "open", "os.listdir", "os.scandir", "os.remove", "os.rename", "os.replace", "os.mkdir", "os.rmdir", "os.chdir", "os.fchdir", "os.chmod", "os.chown",
        "os.link", "os.symlink", "os.truncate", "os.utime", "os.chroot", "os.mkfifo", "os.mknod", "os.walk", "os.fwalk", "os.getxattr", "os.setxattr",
        "os.removexattr", "os.listxattr", "os.add_dll_directory", "winreg", "pathlib.Path.glob", "pathlib.Path.rglob", "glob.glob", "glob.glob/2"})
    file_prefixes = ("shutil.", "tempfile.", "glob.", "pathlib.", "fcntl.", "mmap.", "sqlite3.", "zipfile.", "tarfile.", "winreg.", "dbm.", "shelve.")
    network_prefixes = ("socket.", "urllib.", "http.", "ftplib.", "smtplib.", "telnetlib.", "ssl.", "webbrowser.", "imaplib.", "poplib.", "nntplib.", "_socket.",
                        "_ssl.")
    process_events = frozenset({
        "os.system", "os.fork", "os.forkpty", "os.kill", "os.killpg", "os.startfile", "os.startfile/2", "os.putenv", "os.unsetenv", "os.setuid", "os.setgid",
        "os.unshare", "os.setns", "os.chroot"})
    process_prefixes = ("os.exec", "os.spawn", "os.posix_spawn", "subprocess.", "pty.", "multiprocessing.", "ctypes.", "msvcrt.", "signal.", "resource.",
                        "_thread.", "threading.", "_posixsubprocess.", "_winapi.", "_multiprocessing.", "_ctypes.", "_xxsubinterpreters.", "_interpreters.",
                        "_posixshmem.", "_overlapped.", "nt.", "posix.")
    reflection_events = frozenset({
        "sys._getframe", "sys._current_frames", "sys._current_exceptions", "sys.settrace", "sys.setprofile", "sys._getframemodulename", "marshal.loads",
        "marshal.dumps", "pickle.find_class", "sys.addaudithook", "sys.set_asyncgen_hooks_firstiter", "sys.set_asyncgen_hooks_finalizer",
        "cpython.PyInterpreterState_New", "cpython.PyInterpreterState_Clear", "cpython._PySys_ClearAuditHooks", "gc.get_objects", "gc.get_referrers",
        "gc.get_referents"})
    reflection_prefixes = ("sys.monitoring.", "cpython.")
    dangerous_members = frozenset({
        "__globals__", "__code__", "__closure__", "__builtins__", "__dict__", "__subclasses__", "f_globals", "f_locals", "f_builtins", "f_back", "f_code",
        "gi_frame", "gi_code", "cr_frame", "cr_code", "ag_frame", "ag_code", "tb_frame", "tb_next", "co_code"})
    member_events = frozenset({"object.__getattr__", "object.__setattr__", "object.__delattr__", "type.__setattr__"})
    write_events = frozenset({"object.__setattr__", "object.__delattr__", "type.__setattr__"})
    creation_events = frozenset({"compile", "exec", "code.__new__", "function.__new__"})
    allowed_imports = ALLOWED_IMPORTS
    user_filename = USER_FILENAME
    module_code_name = "<module>"
    # While the rule loads, a frame may compile, exec or read the code of a function only if it is one of the code objects of the worker and of the few
    # functions of the standard library that the worker itself calls to build the dataclass and check the code (`_trusted_codes`), and only if the frame of
    # `_load` is in the stack driving the call, with nothing between them but more of those code objects and the module level of the rule. The code objects
    # are held by identity (their ids), never by file name.
    load_code = _load.__code__
    trusted_codes = _trusted_codes() + (load_code,)
    trusted_ids = frozenset(id(code) for code in trusted_codes)
    # The only trusted code the module level of the rule may call directly is the decorator of the dataclass (`wrap`): `dataclasses._create_fn` or
    # `_process_class` called by the rule itself have the rule's frame right above them and are refused.
    entry_ids = frozenset(id(c) for c in dataclasses.dataclass.__code__.co_consts if isinstance(c, types.CodeType) and c.co_name == "wrap")

    # builtins and types the hook needs, bound here so that patching the `builtins` module cannot change the hook
    type_of, str_type, module_type, issubclass_of, len_of, id_of = type, str, types.ModuleType, issubclass, len, id
    starts_with, getframe, thread_id = str.startswith, sys._getframe, _thread.get_ident
    busy: set[int] = set()
    busy_add, busy_discard, busy_has = busy.add, busy.discard, busy.__contains__
    fire = _make_fire(report)

    def trusted_caller() -> bool:
        frame = getframe(3)          # 0 this function, 1 classify, 2 the hook, 3 the Python frame that made the operation (the audit call sits in C)
        if id_of(frame.f_code) not in trusted_ids:
            return False
        below = id_of(frame.f_code)       # the code object of the frame under the one being examined
        while frame is not None:
            code = frame.f_code
            if code is load_code:
                return True
            if code.co_filename == user_filename and code.co_name == module_code_name:
                if below not in entry_ids:               # the module of the rule may only be under the decorator, never directly above `_create_fn` and the rest
                    return False
            elif id_of(code) not in trusted_ids:
                return False
            below = id_of(code)
            frame = frame.f_back
        return False

    def classify(event: str, args: tuple[Any, ...]) -> tuple[str, str] | None:
        if type_of(event) is not str_type:
            return None                                                # only sys.audit(...) from Python sends something else: it is not an operation
        loading = state.loading
        if event == "open" and loading and len_of(args) > 0 and type_of(args[0]) is str_type and args[0] == user_filename:
            return None                                                # CPython looks for the source line of a SyntaxError by opening the file named in it
        if event in file_events or starts_with(event, file_prefixes):
            return "blocked_file", f"file or folder access ({event})"
        if starts_with(event, network_prefixes):
            return "blocked_network", f"network access ({event})"
        if event in process_events or starts_with(event, process_prefixes):
            return "blocked_process", f"process or native code ({event})"
        if event in reflection_events or starts_with(event, reflection_prefixes):
            return "blocked_import", f"reflection or serialization ({event})"
        if event in member_events and len_of(args) > 1:
            name = args[1]
            if event in write_events and issubclass_of(type_of(args[0]), module_type):
                return "blocked_import", f"changing an attribute of a module ({event})"      # no rule writes to a module, ever, not even while loading
            if type_of(name) is str_type and name in dangerous_members:
                if loading and trusted_caller():                      # inspect and dataclasses read code objects while the class is built
                    return None
                return "blocked_import", f"access to {name!r}"
            return None
        if event == "import":
            module = args[0] if len_of(args) > 0 else ""
            if type_of(module) is str_type and (module in allowed_imports or module == "numpy" or starts_with(module, "numpy.")) and (loading or state.loaded):
                return None
            if type_of(module) is not str_type:
                return "blocked_import", "import of a name that is not a text"
            return "blocked_import", f"import of {module!r} " + ("not in the list" if loading else "after the load")
        if event in creation_events:
            if loading and trusted_caller():
                return None
            return "blocked_import", f"{event} outside the build of the class"
        return None

    def hook(event: str, args: tuple[Any, ...]) -> None:
        me = thread_id()
        if busy_has(me):                                               # the hook asked for the frame: that event is its own
            return
        busy_add(me)
        try:
            verdict = classify(event, args)
        finally:
            busy_discard(me)
        if verdict is not None:
            fire(verdict[0], verdict[1])

    return hook


# ---------------------------------------------------------------- importer and builtins of the rule

def _make_importer() -> Callable[..., Any]:
    """The `__import__` of the rule. The rule CAN reach this function, so it closes on nothing but a read-only table that is ALREADY resolved (name to module
    object, only the allowed ones) and three exact types: no `sys.modules`, no `fire`, no `report`, no state, no hook. A refused import raises `Violation`
    (a BaseException): `main` writes it to the parent and ends the process, and the static check does not let a rule write `except BaseException`. The audit hook
    refuses on its own the imports that reach the real importer."""
    import __future__

    table = types.MappingProxyType({"math": math, "numpy": np, "__future__": __future__, "hokma.factory.user_api": user_api})
    if frozenset(table) != ALLOWED_IMPORTS:
        raise RuntimeError("the table of the importer and ALLOWED_IMPORTS must list the same modules")
    type_of, str_type, int_type, refuse = type, str, int, Violation

    def guarded_import(name: Any, globals: Any = None, locals: Any = None, fromlist: Any = (), level: Any = 0) -> Any:      # noqa: A002
        if type_of(name) is not str_type:
            raise refuse("blocked_import", "import of a name that is not a text")
        if type_of(level) is not int_type or level != 0 or name not in table or ("." in name and not fromlist):
            raise refuse("blocked_import", "import of " + name + " is not allowed")
        return table[name]

    return guarded_import


def _restricted_builtins() -> dict[str, Any]:
    import builtins

    names = static_check.BUILTINS - {"True", "False", "None"}
    table: dict[str, Any] = {name: getattr(builtins, name) for name in names}
    table["__build_class__"] = builtins.__build_class__
    table["__import__"] = _make_importer()
    table["__name__"] = MODULE_NAME
    return table


# ---------------------------------------------------------------- the layers of the platform

def _linux_layers(as_mb: int, cpu_seconds: int) -> dict[str, Any]:
    import ctypes
    import resource
    import signal

    layers: dict[str, Any] = {}
    parent = os.getppid()
    # 1. network namespace: needs a process with one thread (the parent sets OPENBLAS_NUM_THREADS=1 so numpy starts no pool)
    try:
        os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNET)
        layers["net_namespace"] = "active"
    except (OSError, AttributeError):
        layers["net_namespace"] = "unavailable"
    # 2. no new privileges and death with the parent
    libc = ctypes.CDLL(None, use_errno=True)
    layers["no_new_privs"] = libc.prctl(38, 1, 0, 0, 0) == 0                      # PR_SET_NO_NEW_PRIVS
    layers["pdeathsig"] = libc.prctl(1, int(signal.SIGKILL), 0, 0, 0) == 0          # PR_SET_PDEATHSIG
    try:
        os.setpgrp()                                                                # its own process group: a signal to the group of Hokma never reaches it, nor the other way
        layers["own_group"] = os.getpgrp() == os.getpid()
    except OSError:
        layers["own_group"] = False
    if os.getppid() != parent:                                                      # the parent died before the prctl took hold
        os._exit(4)
    # 3. limits of the operating system
    try:
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        opened = [int(n) for n in os.listdir("/proc/self/fd")]
        limit_as = as_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit_as, limit_as))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        top = max(opened) + 5
        resource.setrlimit(resource.RLIMIT_NOFILE, (top, top))
        layers["rlimits"] = True
    except (OSError, ValueError):
        layers["rlimits"] = False
    return layers


def _darwin_layers(as_mb: int, cpu_seconds: int) -> dict[str, Any]:
    layers: dict[str, Any] = {"net_namespace": "not_supported"}
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        try:
            resource.setrlimit(resource.RLIMIT_AS, (as_mb * 1024 * 1024, as_mb * 1024 * 1024))     # macOS honours it only in part: the parent watches the RSS
        except (OSError, ValueError):
            pass
        layers["rlimits"] = True
    except (OSError, ValueError, ImportError):
        layers["rlimits"] = False
    return layers


def _landlock_deny_files() -> str:
    """Landlock (Linux 5.13 or later, if the kernel has it on): a ruleset that handles every file access and allows none, so that from here on the process
    cannot open, list, create, remove or execute anything by path. The pipe, the descriptors it already has and the memory it already mapped keep working,
    and the trusted phase has already loaded everything a legal rule uses. It does not depend on the audit hook, so a rule that switches the hook off still
    cannot open /etc/passwd or /proc/self/environ. Plain ctypes on the syscalls 444 to 446 (the same on every architecture): no dependency. It needs
    no_new_privs, which the layers above set. Returns 'active' or 'unavailable'; nothing here raises."""
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        create, restrict = 444, 446
        abi = int(libc.syscall(ctypes.c_long(create), None, ctypes.c_size_t(0), ctypes.c_uint32(1)))            # LANDLOCK_CREATE_RULESET_VERSION
        if abi < 1:
            return "unavailable"
        handled = 0x1FFF | ((1 << 13) if abi >= 2 else 0) | ((1 << 14) if abi >= 3 else 0) | ((1 << 15) if abi >= 5 else 0)   # every file access of that ABI
        attr = ctypes.c_uint64(handled)
        fd = int(libc.syscall(ctypes.c_long(create), ctypes.byref(attr), ctypes.c_size_t(8), ctypes.c_uint32(0)))
        if fd < 0:
            return "unavailable"
        try:
            return "active" if int(libc.syscall(ctypes.c_long(restrict), ctypes.c_int(fd), ctypes.c_uint32(0))) == 0 else "unavailable"
        finally:
            os.close(fd)
    except (OSError, AttributeError, ValueError):
        return "unavailable"


def _drop_native_loaders() -> dict[str, Any]:
    """Last step of the trusted phase (after Landlock, the last user of ctypes) and before the audit hook: the process of the rule must not hold a way to call
    native code or to signal without an audit event.
    - ctypes and _ctypes leave sys.modules and are replaced by None there (so `import ctypes` fails even if the hook were off); every module attribute that
      pointed to ctypes (numpy keeps it in `numpy._core._internal.ctypes`, a different attribute path from `np.ctypeslib`, which the static check refuses)
      is set to None. numpy only uses it for `ndarray.ctypes`, which a rule cannot write, so numpy keeps working.
    - os.pidfd_open and signal.pidfd_send_signal (they send a signal without an audit event) are deleted from every module that exposes them.
    Returns what it did, for the `layers` the parent sees."""
    import signal

    holders = 0
    loader = sys.modules.get("ctypes")
    for name in [n for n in sys.modules if n in ("ctypes", "_ctypes") or n.startswith("ctypes.")]:
        sys.modules.pop(name, None)
    sys.modules["ctypes"] = None            # type: ignore[assignment]
    sys.modules["_ctypes"] = None           # type: ignore[assignment]
    if loader is not None:
        for module in list(sys.modules.values()):
            if module is None or module is loader or not isinstance(module, types.ModuleType):
                continue
            for key, value in list(vars(module).items()):
                if value is loader:
                    setattr(module, key, None)
                    holders += 1
    removed = 0
    for module in (os, signal, sys.modules.get("posix"), sys.modules.get("_signal"), sys.modules.get("nt")):
        for name in ("pidfd_open", "pidfd_send_signal"):
            if module is not None and hasattr(module, name):
                delattr(module, name)
                removed += 1
    return {"ctypes_dropped": True, "ctypes_holders_cleared": holders, "pidfd_removed": removed}


def _warm_up() -> None:
    """Touch what a legal rule touches, so that no lazy import happens under the hook."""
    x = np.array([1.0, 2.0, 4.0, 3.0, 5.0])
    for name in static_check.NUMPY_ATTRS:
        attr = getattr(np, name)
        if callable(attr) and name not in ("full", "arange", "linspace", "percentile", "quantile", "convolve", "concatenate", "where", "clip", "maximum",
                                           "minimum", "power", "roll", "zeros", "ones", "float64", "int64", "array", "asarray"):
            try:
                attr(x)
            except (TypeError, ValueError):
                pass
    np.percentile(x, 50), np.quantile(x, 0.5), np.convolve(x, x), np.concatenate((x, x)), np.where(x > 2, x, 0.0), np.clip(x, 1, 3)
    np.maximum(x, 2), np.minimum(x, 2), np.power(x, 2), np.roll(x, 1), np.full(3, 1.0), np.arange(3.0), np.linspace(0, 1, 5), np.zeros(2), np.ones(2)
    x.astype(float).copy(), x.reshape(-1), x[x > 2], x[::-1], x.tolist(), float(x.sum()), x.max(), x.min(), x.shape, x.size, float(np.float64(2)), np.int64(2)
    for f in (math.sqrt, math.log, math.exp, math.floor, math.ceil, math.fabs, math.log10, math.log2, math.tanh):
        f(2.0)
    math.fsum([1.0]), math.hypot(3.0, 4.0), math.pow(2.0, 2.0), math.copysign(1.0, -1.0), math.trunc(2.5), math.isfinite(1.0)
    f"{1.5:.2f} {2!r} {'a'}", "%s %d" % ("a", 1), "\u00e9".encode("utf-8").decode("utf-8"), sorted({1: 2}.items()), json.dumps({"a": [1.5, None]})
    "example.invalid".encode("idna")            # a name lookup imports the codec: let it happen here and not as a false "import" violation under the hook
    user_api.ind.sma(x, 2), user_api.ind.ema(x, 2), user_api.ind.highest(x, 2), user_api.ind.lowest(x, 2)
    user_api.Criterion("c", 1.0, None, None, "s"), user_api.IndicatorRequest("a", "ema")
    validate(ParamSchema((user_api.Param("n", "int", 3, 1, 9), user_api.Param("t", "choice", "a", choices=("a", "b")))), {})


# ---------------------------------------------------------------- the channel

class _Channel:
    """Private copies of fds 0 and 1, so that nothing the rule does to stdin or stdout can touch the protocol."""

    def __init__(self) -> None:
        self.reader = os.fdopen(os.dup(0), "rb")
        self.writer = os.fdopen(os.dup(1), "wb")
        null = os.open(os.devnull, os.O_RDWR)
        os.dup2(null, 0)
        os.dup2(null, 1)
        os.close(null)

    def read(self) -> bytes | None:
        """One line without its newline; None at the end of the pipe; b'\\x00TOO_LARGE' when the line is over the limit (the rest is discarded)."""
        line = self.reader.readline(MAX_MESSAGE + 2)
        if not line:
            return None
        if not line.endswith(b"\n"):
            while True:
                chunk = self.reader.readline(1 << 16)
                if not chunk or chunk.endswith(b"\n"):
                    break
            return b"\x00TOO_LARGE"
        if len(line) - 1 > MAX_MESSAGE:
            return b"\x00TOO_LARGE"
        return line[:-1]

    def send(self, message: dict[str, Any]) -> None:
        data = json.dumps(message, allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(data) - 1 > MAX_MESSAGE:
            data = json.dumps({"error": {"type": "bad_answer", "message": "the answer is too large (over 8 MB)"}}, separators=(",", ":")).encode() + b"\n"
        self.writer.write(data)
        self.writer.flush()


def _error(kind: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"error": {"type": kind, "message": str(message)[:ERROR_TEXT], **extra}}


# ---------------------------------------------------------------- load and call

class _Rule:
    def __init__(self, cls: type, kind: str) -> None:
        self.cls, self.kind = cls, kind
        self.schema: ParamSchema = cls.SCHEMA          # type: ignore[attr-defined]
        self.instances: dict[str, Any] = {}

    def instance(self, params: dict[str, Any]) -> Any:
        values = validate(self.schema, params)
        key = json.dumps(values, sort_keys=True, default=str)
        found = self.instances.get(key)
        if found is None:
            if len(self.instances) >= MAX_INSTANCES:
                self.instances.clear()
            found = self.instances[key] = self.cls(**values)
        return found


def _schema_data(schema: ParamSchema) -> list[dict[str, Any]]:
    return [{"name": p.name, "kind": p.kind, "default": p.default, "low": None if p.low is None else float(p.low),
             "high": None if p.high is None else float(p.high), "choices": list(p.choices)} for p in schema.params]


def _load(message: Any, check: Callable[..., Any], state: types.SimpleNamespace, report: Callable[[str, str], None] | None = None) -> tuple[dict[str, Any], _Rule | None]:
    if not isinstance(message, dict) or not isinstance(message.get("source"), str) or message.get("kind") not in static_check.KINDS:
        return _error("bad_input", "load needs a text source and a kind: alpha, filter or exit"), None
    source, kind = message["source"], message["kind"]
    state.loading = True
    try:
        result = check(source, kind=kind)
        if not result.ok:
            return _error("static_check", f"{len(result.findings)} finding(s) of the static check",
                          findings=[f.to_data() for f in result.findings[:50]]), None
        code = compile(source, USER_FILENAME, "exec", dont_inherit=True)
        module = types.ModuleType(MODULE_NAME)
        module.__dict__["__builtins__"] = _restricted_builtins()
        sys.modules[MODULE_NAME] = module
        exec(code, module.__dict__)                              # noqa: S102  (the one place that runs generated code, after the static check, in this process)
        classes = [v for v in module.__dict__.values() if isinstance(v, type) and v.__module__ == MODULE_NAME]
        if len(classes) != 1:
            return _error("contract", "the code must define exactly one class"), None
        cls = classes[0]
        schema = getattr(cls, "SCHEMA", None)
        field_names = {f.name for f in dataclasses.fields(cls)} if dataclasses.is_dataclass(cls) else set()
        if not isinstance(schema, ParamSchema):
            return _error("contract", "SCHEMA must be a ParamSchema"), None
        if field_names != set(schema.names):
            return _error("contract", f"the parameters of SCHEMA {sorted(schema.names)} must be the fields of the class {sorted(field_names)}"), None
        lookback, names = getattr(cls, "LOOKBACK", None), getattr(cls, "FACTS" if kind == "alpha" else "FIELDS", None)
        if isinstance(lookback, bool) or not isinstance(lookback, int) or not 1 <= lookback <= LOOKBACK_MAX:
            return _error("contract", f"LOOKBACK must be a whole number from 1 to {LOOKBACK_MAX}"), None
        if not isinstance(names, tuple) or not all(isinstance(n, str) for n in names):
            return _error("contract", "FACTS or FIELDS must be a tuple of texts"), None
        rule = _Rule(cls, kind)
        default = rule.instance({})
        requests = default.requests()
        if not isinstance(requests, tuple) or not all(isinstance(r, user_api.IndicatorRequest) for r in requests):
            return _error("contract", "requests must return a tuple of IndicatorRequest"), None
        methods = [m for m in METHODS[kind] if callable(getattr(cls, m, None))]
        if kind == "alpha" and "signal" not in methods or kind == "filter" and "allows" not in methods or kind == "exit" and "levels" not in methods:
            return _error("contract", "the class does not have the method of its kind"), None
        manifest = {"kind": kind, "schema": _schema_data(schema), "lookback": lookback, "requests": [r.to_data() for r in requests],
                    "methods": methods, "facts" if kind == "alpha" else "fields": list(names)}
        return {"loaded": manifest}, rule
    except Violation:
        raise
    except MemoryError:
        raise
    except SyntaxError as exc:
        return _error("load_error", f"syntax error: {exc.msg}"), None
    except Exception as exc:                                       # the module or the default instance failed
        return _error("load_error", f"{type(exc).__name__}: {exc}"), None
    finally:
        state.loading = False


def _plain(value: Any, depth: int = 0) -> Any:
    """Plain data only: what the parent can take as an answer. Anything else is a bad answer."""
    if depth > 6:
        raise TypeError("too deeply nested")
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, (int, float, np.integer, np.floating)):
        number = float(value)
        if not math.isfinite(number):
            raise TypeError("a number in the answer is not finite")
        return value if isinstance(value, (int, float)) else number
    if isinstance(value, (list, tuple)):
        return [_plain(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v, depth + 1) for k, v in value.items() if isinstance(k, str)}
    raise TypeError(f"{type(value).__name__} is not plain data")


def _answer(method: str, result: Any) -> dict[str, Any]:
    if method == "signal":
        if not isinstance(result, user_api.Signal):
            raise TypeError("signal must return view(...) or ctx.abstain(...)")
        if result.abstained:
            return {"abstain": result.reason, "missing": list(result.missing)}
        return {"value": result.value, "reason": result.reason, "criteria": [c.to_data() for c in result.criteria], "source": result.source}
    if method in ("allows", "must_flatten"):
        if method == "must_flatten" and isinstance(result, bool):
            return {"flatten": result, "reason": None}
        if not (isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], bool) and (result[1] is None or isinstance(result[1], str))):
            raise TypeError(f"{method} must return (bool, reason or None)")
        return {("allow" if method == "allows" else "flatten"): result[0], "reason": None if result[1] is None else result[1][:ERROR_TEXT]}
    if method == "levels":
        units = (user_api.Price, user_api.Ticks, user_api.ATR, user_api.Pct)
        if not (isinstance(result, tuple) and len(result) == 2 and all(r is None or isinstance(r, units) for r in result)):
            raise TypeError("levels must return (stop or None, target or None) with Price, Ticks, ATR or Pct")
        return {"sl": None if result[0] is None else result[0].to_data(), "tp": None if result[1] is None else result[1].to_data()}
    return {"manage": _plain(result)}


def _call(message: Any, rule: _Rule | None) -> dict[str, Any]:
    if rule is None:
        return _error("not_loaded", "load the code before calling it")
    if not isinstance(message, dict) or not isinstance(message.get("method"), str):
        return _error("bad_input", "call needs a method name")
    method = message["method"]
    if method not in METHODS[rule.kind] or not callable(getattr(rule.cls, method, None)):
        return _error("bad_input", f"the rule has no method {method!r} to call")
    params, ctx, args = message.get("params") or {}, message.get("ctx") or {}, message.get("args") or {}
    if not all(isinstance(x, dict) for x in (params, ctx, args)):
        return _error("bad_input", "params, ctx and args are objects")
    try:
        instance = rule.instance(params)
    except ParamError as exc:
        return _error("bad_params", str(exc))
    except Exception as exc:
        return _error("rule_error", f"{type(exc).__name__}: {exc}")
    if rule.kind == "alpha":
        context: Any = user_api.SignalContext(source=str(ctx.get("source", "rule")), symbol=str(ctx.get("symbol", "")), as_of=str(ctx.get("as_of", "")),
                                              facts=ctx.get("facts") or {})
    else:
        context = user_api.RuleBarContext(as_of=str(ctx.get("as_of", "")), symbol=str(ctx.get("symbol", "")), data=ctx.get("series") or {},
                                          position_side=ctx.get("position_side"), equity=ctx.get("equity"))
    kwargs = {k: args[k] for k in KWARGS[method] if k in args}
    if "state" in KWARGS[method]:
        kwargs.setdefault("state", {})
    try:
        if method == "signal":
            result = instance.signal(context)
        else:
            result = getattr(instance, method)(ctx=context, **kwargs)
    except Violation:
        raise
    except MemoryError:
        raise
    except Exception as exc:
        return _error("rule_error", f"{type(exc).__name__}: {exc}")
    try:
        return {"ok": _answer(method, result)}
    except Exception as exc:
        return _error("bad_answer", f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- main

def _option(argv: list[str], name: str, default: int) -> int:
    if name in argv:
        try:
            return int(argv[argv.index(name) + 1])
        except (IndexError, ValueError):
            return default
    return default


def main(*, check: Callable[..., Any] = static_check.check, install_audit_hook: bool = True, landlock: bool = True, argv: list[str] | None = None) -> int:
    """The life of the process. `check`, `install_audit_hook` and `landlock` exist for the adversarial tests, which run it by file path with a layer off
    to prove the next one on its own."""
    args = sys.argv[1:] if argv is None else argv
    as_mb, cpu = _option(args, "--as-mb", DEFAULT_AS_MB), _option(args, "--cpu", DEFAULT_CPU_SECONDS)
    state = _new_state()
    channel = _Channel()
    report = _make_report(channel.writer.fileno())
    _warm_up()
    layers: dict[str, Any] = {}
    if sys.platform.startswith("linux"):
        layers.update(_linux_layers(as_mb, cpu))
        layers["landlock"] = _landlock_deny_files() if landlock else "off"
    elif sys.platform == "darwin":
        layers.update(_darwin_layers(as_mb, cpu))
    else:
        layers["net_namespace"] = "not_supported"
    layers.update(_drop_native_loaders())
    if install_audit_hook:
        sys.addaudithook(_make_hook(state, report))
    layers["audit_hook"] = install_audit_hook
    for name in {__name__, "__main__", "hokma.factory.usercode.worker"}:      # the module of this file is not a thing a rule can look up in sys.modules
        sys.modules.pop(name, None)
    channel.send({"ready": True, "layers": layers})
    rule: _Rule | None = None
    loaded = False
    while True:
        raw = channel.read()
        if raw is None:
            return 0
        if raw == b"\x00TOO_LARGE":
            channel.send(_error("message_too_large", "a message is at most 8 MB"))
            continue
        try:
            message = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            channel.send(_error("bad_message", "a message is one line of JSON"))
            continue
        if not isinstance(message, dict) or len(message) != 1:
            channel.send(_error("bad_message", "a message is an object with one key: load, call or close"))
            continue
        (op, body), = message.items()
        try:
            if op == "close":
                channel.send({"closed": True})
                return 0
            if op == "load":
                if loaded:
                    channel.send(_error("already_loaded", "this process already has its code: start another one"))
                    continue
                answer, rule = _load(body, check, state, report)
                loaded = rule is not None
                state.loaded = loaded
            elif op == "call":
                answer = _call(body, rule)
            else:
                answer = _error("bad_message", f"unknown message {op!r}")
        except Violation as violation:
            channel.send({"violation": violation.code, "message": violation.message[:ERROR_TEXT]})      # only when `report` came back: it ends the process itself
            return 3
        except MemoryError:
            channel.send({"violation": "memory_limit", "message": "the process ran out of memory"})
            return 3
        channel.send(answer)


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
