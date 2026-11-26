"""The attack of Vera on the audit hook (task 3414, finding 1): the static check is skipped, the rule walks from `object.__subclasses__()` to the
globals of the worker module and tries to empty the tables of the hook and to turn its flags off, then opens /etc/passwd and /proc/self/environ.

Three proofs, each on its own layer: the hook with Landlock off (nothing in the globals can be switched off), Landlock with the hook off (the kernel refuses
the file once the hook is gone), and both on.
"""
import sys
from pathlib import Path

import pytest

from hokma.factory.usercode.sandbox import Sandbox, SandboxError
from usercode.support import alpha_ctx, INSIDE_UP, source

LINUX = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux layers")
UNSAFE = [sys.executable, "-I", "-X", "utf8", str(Path(__file__).with_name("unsafe_worker.py"))]
NOHOOK = [sys.executable, "-I", "-X", "utf8", str(Path(__file__).with_name("nohook_worker.py"))]
PATHS = ["/etc/passwd", "/proc/self/environ"]

# The attack of Vera in the body of the module (where the old code ran with the permissive flag), generalised: every dictionary of globals that a class
# or a module of the process gives, searched for the names of the old policy and emptied.
TAMPER = '''
_b = ().__class__.__base__.__subclasses__()
_w = [c for c in _b if c.__name__ == "_wrap_close"][0]
_sys = _w.__init__.__globals__["sys"]
_B = _sys.modules["builtins"]
getattr, isinstance, any, list, hasattr = _B.getattr, _B.isinstance, _B.any, _B.list, _B.hasattr
frozenset, dict = _B.frozenset, _B.dict
_seen = []
for _c in _b:
    _g = getattr(getattr(_c, "__init__", None), "__globals__", None)
    if isinstance(_g, dict) and not any(_g is s for s in _seen):
        _seen.append(_g)
for _m in list(_sys.modules.values()):
    _d = getattr(_m, "__dict__", None)
    if isinstance(_d, dict) and not any(_d is s for s in _seen):
        _seen.append(_d)
for _g in _seen:
    for _name in ("_FILE_EVENTS", "_PROCESS_EVENTS", "_REFLECTION_EVENTS", "_DANGEROUS_MEMBERS"):
        if _name in _g:
            _g[_name] = frozenset()
    for _name in ("_FILE_PREFIXES", "_PROCESS_PREFIXES", "_NETWORK_PREFIXES", "_REFLECTION_PREFIXES"):
        if _name in _g:
            _g[_name] = ()
    _st = _g.get("STATE")
    if _st is not None:
        _st.loading = True
        _st.loaded = False
    if "classify" in _g:
        _g["classify"] = lambda *a, **k: None
for _name in ("__main__", "hokma.factory.usercode.worker"):
    _m = _sys.modules.get(_name)
    if _m is not None and hasattr(_m, "_FILE_EVENTS"):
        _m._FILE_EVENTS = frozenset()
'''
READ_AT_LOAD = "_data = _sys.modules['io'].open({path!r}, 'rb').read(64)\n"
SIGNAL_READS = '''        _data = _sys.modules['io'].open({path!r}, 'rb').read(64)
        return view(ctx, "u_x", 0.0, (), "LEAK " + str(len(_data)))'''


def attack(path: str, *, at_load: bool) -> str:
    if at_load:
        return source("alpha", module_extra=TAMPER + READ_AT_LOAD.format(path=path))
    return source("alpha", module_extra=TAMPER, body=SIGNAL_READS.format(path=path))


def outcome(argv: list[str], src: str, tmp_path: Path, extra: list[str] | None = None) -> tuple[str, str, dict]:
    sb = Sandbox(workdir=tmp_path / "empty", worker_argv=argv + (extra or []), call_timeout=5, load_timeout=20)
    try:
        layers = sb.start()
        try:
            sb.load(src, "alpha")
            return "ran", str(sb.call("signal", ctx=alpha_ctx(INSIDE_UP)).get("reason")), layers
        except SandboxError as exc:
            return "blocked", exc.code, layers
    finally:
        sb.close()


@LINUX
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("at_load", [True, False])
def test_the_attack_of_vera_opens_nothing_with_the_hook_alone(tmp_path, path, at_load):
    """Landlock off: only the hook. The tables and flags are in no global that the rule can reach, so the tampering changes nothing."""
    status, detail, layers = outcome(UNSAFE, attack(path, at_load=at_load), tmp_path, ["--no-landlock"])
    assert layers["landlock"] == "off" and layers["audit_hook"] is True
    assert (status, detail) == ("blocked", "blocked_file"), (status, detail)


@LINUX
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("at_load", [True, False])
def test_the_attack_of_vera_opens_nothing_with_both_layers(tmp_path, path, at_load):
    status, detail, _ = outcome(UNSAFE, attack(path, at_load=at_load), tmp_path)
    assert (status, detail) == ("blocked", "blocked_file"), (status, detail)


@LINUX
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("at_load", [True, False])
def test_landlock_alone_refuses_the_file_when_the_hook_is_gone(tmp_path, path, at_load):
    status, detail, layers = outcome(NOHOOK, attack(path, at_load=at_load), tmp_path)
    if layers.get("landlock") != "active":
        pytest.skip(f"this kernel does not give Landlock: {layers.get('landlock')}")
    assert layers["audit_hook"] is False
    assert status == "blocked" and detail in ("rule_error", "load_error"), (status, detail)      # the kernel said EACCES: the open raised PermissionError


# ---------------------------------------------------------------- round 2 (task 3414): the hook depends on nothing the rule can modify

PRE = '''
_b = ().__class__.__base__.__subclasses__()
_w = [c for c in _b if c.__name__ == "_wrap_close"][0]
_sys = _w.__init__.__globals__["sys"]
_B = _sys.modules["builtins"]
_os = _sys.modules["os"]
'''
# LEN2 of Vera, for any builtin: replace it by a function that restores itself and reads /etc/passwd, then do something that makes the hook run
# (the read of __code__ is an event). If the hook named that builtin, the rule's function ran INSIDE the hook, with the hook off.
PATCH_ATTACK = PRE + '''
_name = {name!r}
_orig = _B.getattr(_B, _name)
_box = []
def _evil(*a, **k):
    _B.setattr(_B, _name, _orig)
    try:
        _box.append(_B.open("/etc/passwd", "rb").read(40))
    except _B.BaseException as e:
        _box.append("ERR " + _B.repr(e))
    return 0
_B.setattr(_B, _name, _evil)
_x = _w.__init__.__code__
_B.setattr(_B, _name, _orig)
raise _B.RuntimeError("LEAK:" + _B.repr(_box))
'''
PATCHED = ["len", "isinstance", "type", "issubclass", "id", "getattr", "frozenset", "tuple", "str", "set", "int", "any", "all", "hasattr", "iter", "next", "repr"]


@LINUX
@pytest.mark.parametrize("name", PATCHED)
def test_patching_a_builtin_does_not_run_the_function_of_the_rule_inside_the_hook(tmp_path, name):
    """LEN2 and LEN3 of Vera and the variants: the hook, the importer and the reporter bind every builtin as a local, so a patched builtin is never called by
    them. The read of __code__ ends the process with the violation; nothing of /etc/passwd leaves."""
    status, detail, _ = outcome(UNSAFE, source("alpha", module_extra=PATCH_ATTACK.format(name=name)), tmp_path, ["--no-landlock"])
    assert (status, detail) == ("blocked", "blocked_import"), (status, detail)


IMMUTABLE_ATTACK = PRE + '''
_done = []
for _t in (_B.str, _B.frozenset, _B.tuple, _B.type, _B.int, _B.set):
    for _attr in ("startswith", "__contains__", "__getitem__", "__eq__", "__hash__", "__instancecheck__", "__subclasscheck__", "__call__"):
        try:
            _B.setattr(_t, _attr, lambda *a, **k: True)
            _done.append(_t.__name__ + "." + _attr)
        except _B.BaseException:
            pass
_x = _w.__init__.__code__
raise _B.RuntimeError("PATCHED:" + _B.repr(_done))
'''


@LINUX
def test_the_methods_of_the_types_the_hook_uses_cannot_be_replaced_and_the_hook_still_decides(tmp_path):
    status, detail, _ = outcome(UNSAFE, source("alpha", module_extra=IMMUTABLE_ATTACK), tmp_path, ["--no-landlock"])
    assert (status, detail) == ("blocked", "blocked_import"), (status, detail)


RESET_ATTACK = PRE + '''
_imp = __builtins__["__import__"]
try:
    _B.open("/etc/passwd", "rb")
except _B.BaseException:
    # the rule caught the exception of the layer and tries to wipe whatever the importer holds, then goes on as if nothing happened
    for _c in _imp.__closure__ or ():
        try:
            _c.cell_contents = None
        except _B.BaseException:
            pass
    _st = _sys.modules.get("__main__")
_x = 1
'''


@LINUX
def test_a_rule_that_catches_the_violation_and_resets_the_state_does_not_keep_the_process_alive(tmp_path):
    """The parent sees the violation and the worker is gone: the violation was written to the pipe and the process ended in the same call that found it."""
    src = source("alpha", module_extra=RESET_ATTACK)
    sb = Sandbox(workdir=tmp_path / "empty", worker_argv=UNSAFE + ["--no-landlock"], call_timeout=5, load_timeout=20)
    try:
        sb.start()
        with pytest.raises(SandboxError) as caught:
            sb.load(src, "alpha")
            sb.call("signal", ctx=alpha_ctx(INSIDE_UP))
        assert caught.value.code == "blocked_file", (caught.value.code, caught.value.message)
        assert not sb.alive and sb.exit_code == 3
    finally:
        sb.close()


CLOSURE_ATTACK = PRE + '''
_imp = __builtins__["__import__"]
_kinds = [_B.type(_c.cell_contents).__name__ for _c in _imp.__closure__]
_more = []
for _c in _imp.__closure__:
    _f = _c.cell_contents
    for _cc in (_B.getattr(_f, "__closure__", None) or ()):
        _more.append(_B.type(_cc.cell_contents).__name__)
raise _B.RuntimeError("CELLS:" + _B.repr(_kinds + _more))
'''


@LINUX
def test_the_importer_the_rule_can_reach_holds_no_state_even_with_the_hook_off(tmp_path):
    """Worker with no audit hook (so the closure can be read): what the importer closes on is a table of names, the module table and the functions that report
    and raise. Not the flags of the hook, and no list or dictionary to clear."""
    sb = Sandbox(workdir=tmp_path / "empty", worker_argv=NOHOOK, call_timeout=5, load_timeout=20)
    try:
        sb.start()
        with pytest.raises(SandboxError) as caught:
            sb.load(source("alpha", module_extra=CLOSURE_ATTACK), "alpha")
        text = caught.value.message
        assert "CELLS:" in text, text
        assert "SimpleNamespace" not in text and "'list'" not in text and "'set'" not in text, text
    finally:
        sb.close()
