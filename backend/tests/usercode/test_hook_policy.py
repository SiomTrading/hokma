"""The policy of the audit hook, called directly (task 3414, finding 1). The hook is built by `worker._make_hook(state)`: its tables are cells of that closure,
not names of the module, and the permission of the load belongs to the FRAME that makes the operation, never to a flag alone.
"""
import dataclasses
import dis
import os
import sys
import types

import pytest

from hokma.factory.usercode import worker


def build(**flags):
    """A hook without a reporter: it raises Violation (the real one ends the process in the same call, see test_sandbox_tamper)."""
    state = worker._new_state()
    for k, v in flags.items():
        setattr(state, k, v)
    return worker._make_hook(state), state


def called_from(filename: str, hook, event: str, args=()):
    """Run hook(event, args) from a frame whose code file is `filename`."""
    code = compile("def run(h, e, a):\n    h(e, a)\n", filename, "exec")
    scope: dict = {}
    exec(code, scope)  # noqa: S102  (the test builds a frame with a given file name)
    scope["run"](hook, event, args)


def test_the_module_keeps_no_table_and_no_flag_for_a_rule_to_reach():
    names = {"STATE", "classify", "_FILE_EVENTS", "_FILE_PREFIXES", "_NETWORK_PREFIXES", "_PROCESS_EVENTS", "_PROCESS_PREFIXES", "_REFLECTION_EVENTS",
             "_REFLECTION_PREFIXES", "_DANGEROUS_MEMBERS"}
    assert not names & set(vars(worker))
    assert not [v for v in vars(worker).values() if isinstance(v, (frozenset, set)) and len(v) > 5 and v is not worker.ALLOWED_IMPORTS]


def functions_of(root):
    """Every Python function reachable from `root` through closure cells (the hook, classify, trusted_caller, fire, report...)."""
    seen, todo = [], [root]
    while todo:
        f = todo.pop()
        if not isinstance(f, types.FunctionType) or any(f is s for s in seen):
            continue
        seen.append(f)
        todo.extend(c.cell_contents for c in (f.__closure__ or ()))
    return seen


def nested_codes(code):
    yield code
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            yield from nested_codes(const)


def test_the_tables_and_the_flags_are_cells_of_the_closure_only():
    hook, state = build()
    held = [c.cell_contents for f in functions_of(hook) for c in (f.__closure__ or ())]
    assert any(v is state for v in held)                   # the state is a cell, not a name of the module
    assert not any(isinstance(v, types.SimpleNamespace) for v in vars(worker).values())


ALLOWED_ATTRIBUTES = {"f_code", "f_back", "co_filename", "co_name", "loading", "loaded"}
FORBIDDEN_OPS = {"LOAD_GLOBAL", "LOAD_NAME", "STORE_GLOBAL", "STORE_NAME", "DELETE_GLOBAL", "DELETE_NAME", "IMPORT_NAME", "IMPORT_FROM"}


def offences(function):
    found = []
    for code in nested_codes(function.__code__):
        for ins in dis.get_instructions(code):
            if ins.opname in FORBIDDEN_OPS:
                found.append(f"{function.__qualname__}: {ins.opname} {ins.argval}")
            elif ins.opname in ("LOAD_ATTR", "LOAD_METHOD", "STORE_ATTR", "DELETE_ATTR", "LOAD_SUPER_ATTR") and ins.argval not in ALLOWED_ATTRIBUTES:
                found.append(f"{function.__qualname__}: {ins.opname} {ins.argval}")
    return found


def test_the_code_of_the_hook_and_of_what_it_calls_reads_no_global_no_builtin_and_no_attribute_of_a_shared_object():
    """The finding of Vera (builtins.len patched from the rule made the hook call the rule's function with the hook off): every name the hook uses is a local or a
    cell, the only attributes it reads are the ones of a frame, a code object and its own state, and no import happens inside it."""
    state = worker._new_state()
    report = worker._make_report(1)
    hook = worker._make_hook(state, report)
    functions = functions_of(hook)
    names = {f.__name__ for f in functions}
    assert {"hook", "classify", "trusted_caller", "fire", "report"} <= names, names
    assert not [o for f in functions for o in offences(f)]


def test_the_importer_the_rule_can_reach_reads_no_global_and_holds_no_state():
    """Task 3414 round 4: the importer holds a resolved read-only table and exact types only: no sys.modules, no fire, no report, no state, no mutable box."""
    importer = worker._restricted_builtins()["__import__"]
    functions = functions_of(importer)
    assert {f.__name__ for f in functions} == {"guarded_import"}
    assert not [o for f in functions for o in offences(f)]
    held = [c.cell_contents for f in functions for c in (f.__closure__ or ())]
    assert not any(v is sys.modules or isinstance(v, (types.SimpleNamespace, list, dict, set)) for v in held)
    tables = [v for v in held if isinstance(v, types.MappingProxyType)]
    assert len(tables) == 1 and set(tables[0]) == worker.ALLOWED_IMPORTS
    assert all(isinstance(m, types.ModuleType) for m in tables[0].values())


def test_a_refused_import_raises_the_violation_and_an_allowed_one_gives_the_resolved_module():
    importer = worker._restricted_builtins()["__import__"]
    assert importer("math") is __import__("math")
    for bad in ("os", "numpy.linalg", "ctypes", "hokma.broker"):
        with pytest.raises(worker.Violation):
            importer(bad)
    with pytest.raises(worker.Violation):
        importer("math", None, None, (), 1)
    with pytest.raises(worker.Violation):
        importer(b"math")


def test_the_checker_of_the_bytecode_would_catch_a_global_and_a_foreign_attribute():
    def uses_len(x):
        return len(x)

    def reads_attr(x):
        return x.startswith("a")

    assert offences(uses_len) and offences(reads_attr)


def test_patching_builtins_does_not_change_what_the_hook_decides():
    """The attack of Vera (LEN2/LEN3) at the unit level, in a process of its own: every builtin the hook might name is replaced by a function that records the
    call. The hook still decides the same and never calls any of them."""
    import subprocess

    code = (
        "import builtins\n"
        "from hokma.factory.usercode import worker\n"
        "state = worker._new_state(); state.loaded = True\n"
        "hook = worker._make_hook(state)\n"
        "names = ['len', 'isinstance', 'issubclass', 'id', 'type', 'getattr', 'frozenset', 'tuple', 'str', 'set', 'int', 'hasattr', 'any', 'all', 'iter', 'next']\n"
        "saved = {n: getattr(builtins, n) for n in names}\n"
        "calls = []\n"
        "def spy(n):\n"
        "    def f(*a, **k):\n"
        "        calls.append(n)\n"
        "        return 0\n"
        "    return f\n"
        "for n in names:\n"
        "    setattr(builtins, n, spy(n))\n"
        "out = []\n"
        "for ev, args in (('open', ('/etc/passwd',)), ('object.__getattr__', (None, '__globals__')), ('import', ('socket',)), ('os.getpid', ())):\n"
        "    try:\n"
        "        hook(ev, args)\n"
        "        out.append('allowed')\n"
        "    except worker.Violation as v:\n"
        "        out.append(v.code)\n"
        "for n in names:\n"
        "    setattr(builtins, n, saved[n])\n"
        "print(out, calls)\n")
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=30, env={"PYTHONPATH": os.pathsep.join(sys.path)})
    assert done.stdout.strip() == "['blocked_file', 'blocked_import', 'blocked_import', 'allowed'] []", (done.stdout, done.stderr[-400:])


def test_the_real_reporter_writes_the_violation_to_the_pipe_and_ends_the_process():
    import json as _json
    import subprocess

    code = ("import os, sys\n"
            "from hokma.factory.usercode import worker\n"
            "r = worker._make_report(1)\n"
            "worker._make_hook(worker._new_state(), r)('open', ('/etc/passwd',))\n"
            "print('NOT REACHED')\n")
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=30, env={"PYTHONPATH": os.pathsep.join(sys.path)})
    lines = done.stdout.splitlines()
    assert done.returncode == 3 and len(lines) == 1, (done.returncode, done.stdout, done.stderr[-300:])
    assert _json.loads(lines[0]) == {"violation": "blocked_file", "message": "file or folder access (open)"}


@pytest.mark.parametrize("event", ["_posixsubprocess.fork_exec", "_winapi.CreateProcess", "_winapi.OpenProcess", "_winapi.CreateFile", "_multiprocessing.sem_unlink", "ctypes.dlopen",
                                   "os.exec", "subprocess.Popen", "nt.spawn"])
def test_native_process_events_are_blocked_as_process(event):
    hook, state = build(loaded=True)
    with pytest.raises(worker.Violation) as caught:
        hook(event, ())
    assert caught.value.code == "blocked_process"


@pytest.mark.parametrize("event", ["_socket.getaddrinfo", "_ssl.txt2obj"])
def test_native_network_events_are_blocked(event):
    hook, _ = build(loaded=True)
    with pytest.raises(worker.Violation):
        hook(event, ())


@pytest.mark.parametrize("event", ["object.__setattr__", "object.__delattr__", "type.__setattr__"])
def test_writing_to_a_module_is_blocked_even_while_loading_and_from_a_trusted_file(event):
    for flags in ({"loading": True}, {"loaded": True}):
        hook, _ = build(**flags)
        with pytest.raises(worker.Violation):
            called_from(dataclasses.__file__, hook, event, (types.ModuleType("m"), "x", 1))
    hook, _ = build(loaded=True)
    hook(event, (object(), "x", 1))                         # an ordinary object is fine


@pytest.mark.parametrize("event,args", [("exec", ()), ("compile", ()), ("code.__new__", ()), ("function.__new__", ()),
                                        ("object.__getattr__", (None, "__code__")), ("object.__getattr__", (None, "tb_frame")), ("import", ("socket",))])
def test_while_loading_a_frame_of_the_rule_gets_no_exception(event, args):
    hook, _ = build(loading=True)
    with pytest.raises(worker.Violation):
        called_from(worker.USER_FILENAME, hook, event, args)


@pytest.mark.parametrize("event,args", [("exec", ()), ("compile", ()), ("object.__getattr__", (None, "__code__"))])
def test_while_loading_no_file_is_trusted_as_a_whole_only_the_code_objects_of_the_load_inside_its_stack(event, args):
    """The load may compile, exec and read code only from named code objects with the frame of `_load` below them. A function of dataclasses, inspect or ast
    called from anywhere else, the very file of the worker, and a frame of the rule, are refused."""
    import ast
    import inspect

    hook, _ = build(loading=True)
    for foreign in (dataclasses.__file__, inspect.__file__, ast.__file__, worker.__file__, "/tmp/other.py", "<string>", worker.USER_FILENAME):
        with pytest.raises(worker.Violation):
            called_from(foreign, hook, event, args)


def test_a_trusted_code_object_called_from_a_stack_without_the_load_ends_the_process():
    """Same code object as the build of the dataclass (`dataclasses._create_fn`, which execs), with the hook really installed and the load flag on, but the stack
    has no `_load` frame under it: the process ends with the violation. The same call inside `_load` is what every sample rule does when it loads."""
    import subprocess

    code = ("import dataclasses, sys\n"
            "from hokma.factory.usercode import worker\n"
            "state = worker._new_state(); state.loading = True\n"
            "sys.addaudithook(worker._make_hook(state, worker._make_report(1)))\n"
            "dataclasses._create_fn('f', ('a',), ['return 1'], globals={}, locals={})\n"
            "print('NOT REACHED')\n")
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=30, env={"PYTHONPATH": os.pathsep.join(sys.path)})
    assert done.returncode == 3 and "NOT REACHED" not in done.stdout and '"blocked_import"' in done.stdout, (done.returncode, done.stdout, done.stderr[-300:])


@pytest.mark.parametrize("event,args", [("exec", ()), ("compile", ()), ("object.__getattr__", (None, "__code__"))])
def test_after_the_load_not_even_a_trusted_code_object_gets_the_exception(event, args):
    hook, _ = build(loading=False, loaded=True)
    with pytest.raises(worker.Violation):
        called_from(dataclasses.__file__, hook, event, args)


def test_the_hook_calls_nothing_of_an_object_the_rule_controls():
    class Evil(str):
        def __eq__(self, other):
            raise AssertionError("the hook compared an object of the rule")

        __hash__ = str.__hash__

        def startswith(self, *a):
            raise AssertionError("the hook asked a method of an object of the rule")

    hook, _ = build(loading=True)
    with pytest.raises(worker.Violation):
        hook("open", (Evil(worker.USER_FILENAME),))                   # a str subclass is not the file name: refused, and its own __eq__ never ran
    hook(Evil("open"), ("x",))                                        # not an exact str: ignored, and none of its methods ran
    hook("object.__getattr__", (None, Evil("__code__")))              # not an exact str name: it is not looked up


def test_sys_modules_has_no_worker_inside_the_process(tmp_path):
    from hokma.factory.usercode.sandbox import Sandbox
    from usercode.support import alpha, alpha_ctx, INSIDE_UP
    from pathlib import Path

    unsafe = [sys.executable, "-I", "-X", "utf8", str(Path(__file__).with_name("unsafe_worker.py"))]
    body = ("_b = ().__class__.__base__.__subclasses__()\n"
            "_w = [c for c in _b if c.__name__ == '_wrap_close'][0]\n"
            "_sys = _w.__init__.__globals__['sys']\n"
            "_n = [k for k, v in _sys.modules.items() if v is not None and '_make_hook' in v.__dict__]\n"
            "return view(ctx, 'u_x', 0.0, (), 'WORKER ' + ','.join(_n) + '|')")
    sb = Sandbox(workdir=tmp_path / "e", worker_argv=unsafe, call_timeout=5)
    try:
        sb.start()
        src = alpha(body)
        sb.load(src, "alpha")
        assert sb.call("signal", ctx=alpha_ctx(INSIDE_UP))["reason"] == "WORKER |"
    finally:
        sb.close()
