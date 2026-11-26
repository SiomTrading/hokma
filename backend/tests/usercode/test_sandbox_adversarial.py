"""Adversarial tests of the isolated process (SPEC-031 sections 5.5, 6 and 10).

The static check is skipped on purpose (the worker of unsafe_worker.py): they prove the layers that run when the static check fails.
Every one of these ends blocked with its code and the parent keeps running. The Windows ones are in test_sandbox_windows.py.
"""
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hokma.factory.usercode.sandbox import Sandbox, SandboxError
from usercode.support import alpha, alpha_ctx, INSIDE_UP

UNSAFE = [sys.executable, "-I", "-X", "utf8", str(Path(__file__).with_name("unsafe_worker.py"))]
POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX layers")

ESCAPE = """
base = ().__class__.__base__.__subclasses__()
wrap = [c for c in base if c.__name__ == "_wrap_close"][0]
sysmod = wrap.__init__.__globals__["sys"]
os = sysmod.modules["os"]
B = sysmod.modules["builtins"]
"""


def evil(body: str, *, view_reason: str = "x") -> str:
    """A valid rule whose signal runs `body` (after the escape preamble) and then gives a view."""
    return alpha(ESCAPE + body + f"\nreturn view(ctx, 'u_evil', 0.0, (), {view_reason})")


@pytest.fixture
def unsafe(tmp_path):
    boxes = []

    def make(**kw):
        sb = Sandbox(workdir=tmp_path / "empty", worker_argv=UNSAFE, **kw)
        boxes.append(sb)
        sb.start()
        return sb

    yield make
    for sb in boxes:
        sb.close()


def run(sb, body: str, **kw):
    sb.load(evil(body, **kw), "alpha")
    return sb.call("signal", ctx=alpha_ctx(INSIDE_UP))


def blocked(sb, body: str) -> str:
    with pytest.raises(SandboxError) as caught:
        run(sb, body)
    return caught.value.code


def test_a_socket_to_loopback_is_blocked_and_nobody_connects(unsafe):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(0.5)
    port = listener.getsockname()[1]
    try:
        code = blocked(unsafe(call_timeout=5), f"s = sysmod.modules['socket'].socket()\ns.connect(('127.0.0.1', {port}))")
        assert code == "blocked_network"
        with pytest.raises(socket.timeout):
            listener.accept()
    finally:
        listener.close()


def test_resolving_a_name_is_blocked(unsafe):
    assert blocked(unsafe(call_timeout=5), "sysmod.modules['socket'].getaddrinfo('example.invalid', 80)") == "blocked_network"


def test_creating_a_socket_at_all_is_blocked(unsafe):
    assert blocked(unsafe(call_timeout=5), "sysmod.modules['socket'].socket()") == "blocked_network"


@pytest.mark.parametrize("body", [
    "sysmod.modules['io'].open('/etc/hosts').read()",
    "sysmod.modules['io'].open('/etc/hosts', 'rb')",
    "os.open('/etc/hosts', 0)",
    "os.listdir('/')",
    "os.scandir('/')",
    "sysmod.modules['shutil'].copyfile('/etc/hosts', '/tmp/should_not_exist_3414')",
    "sysmod.modules['tempfile'].mkstemp()",
    "sysmod.modules['glob'].glob('/etc/*')",
    "os.mkdir('/tmp/should_not_exist_dir_3414')",
    "os.chdir('/')",
])
def test_reading_listing_or_creating_files_is_blocked(unsafe, body):
    assert blocked(unsafe(call_timeout=5), body) == "blocked_file"


def test_writing_a_file_in_the_data_folder_is_blocked_and_nothing_is_created(unsafe, tmp_path):
    target = tmp_path / "stolen.txt"
    assert blocked(unsafe(call_timeout=5), f"f = sysmod.modules['io'].open({str(target)!r}, 'w')\nf.write('x')") == "blocked_file"
    assert not target.exists()


def test_removing_and_renaming_files_is_blocked(unsafe, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    assert blocked(unsafe(call_timeout=5), f"os.remove({str(victim)!r})") == "blocked_file"
    assert blocked(unsafe(call_timeout=5), f"os.rename({str(victim)!r}, {str(victim) + '.x'!r})") == "blocked_file"
    assert victim.read_text() == "keep"


@pytest.mark.parametrize("body", [
    "os.system('touch MARK')",
    "sysmod.modules['subprocess'].Popen(['touch', 'MARK'])",
    "sysmod.modules['subprocess'].run(['touch', 'MARK'])",
    "os.fork()",
    "os.posix_spawn('/usr/bin/touch', ['touch', 'MARK'], {})",
    "os.execv('/usr/bin/touch', ['touch', 'MARK'])",
    "os.spawnv(0, '/usr/bin/touch', ['touch', 'MARK'])",
    "os.kill(1, 0)",
    "[c for c in base if c.__name__ == 'Popen'][0](['touch', 'MARK'])",
])
def test_creating_processes_is_blocked_and_the_command_never_runs(unsafe, tmp_path, body):
    mark = tmp_path / "MARK"
    assert blocked(unsafe(call_timeout=5), body.replace("MARK", str(mark))) == "blocked_process"
    time.sleep(0.2)
    assert not mark.exists()


@pytest.mark.parametrize("module", ["os", "socket", "hokma.broker", "hokma.exec", "hokma.db", "hokma.desks", "hokma.kit", "importlib", "subprocess"])
def test_importing_anything_outside_the_list_is_blocked(unsafe, module):
    assert blocked(unsafe(call_timeout=5), f"__import__({module!r})") == "blocked_import"


@pytest.mark.parametrize("module", ["hokma.broker", "hokma.exec", "hokma.db", "hokma.desks", "hokma.schwab", "hokma.connections"])
def test_the_real_importer_of_the_interpreter_is_blocked_for_a_module_not_loaded(unsafe, module):
    assert blocked(unsafe(call_timeout=5), f"B.__import__({module!r})") == "blocked_import"


def test_ctypes_is_gone_from_the_process_even_when_the_rule_reaches_sys_modules(unsafe):
    """The worker removes ctypes (already imported here, as the strongest attacker would have it) before the hook: sys.modules holds None for it, a direct
    import is refused and the real importer refuses it too."""
    assert blocked(unsafe(call_timeout=5), "sysmod.modules['ctypes'].CDLL('libc.so.6')") == "rule_error"
    assert blocked(unsafe(call_timeout=5), "B.__import__('ctypes')") == "blocked_import"
    assert blocked(unsafe(call_timeout=5), "__import__('ctypes')") == "blocked_import"
    assert run(unsafe(call_timeout=5), "pass", view_reason="str(sysmod.modules['ctypes'])")["reason"] == "None"


@pytest.mark.parametrize("body", ["B.compile('1', 'f', 'eval')", "B.exec('x = 1')", "B.eval('1')", "sysmod._getframe(0)", "sysmod.settrace(None)",
                                  "sysmod.setprofile(None)"])
def test_compiling_executing_and_looking_at_frames_after_the_load_is_blocked(unsafe, body):
    assert blocked(unsafe(call_timeout=5), body).startswith("blocked_")


def test_an_infinite_list_ends_in_memory_limit_and_hokma_goes_on(unsafe):
    sb = unsafe(call_timeout=20)
    assert blocked(sb, "xs = []\nwhile True:\n    xs.append([0] * 1000000)") == "memory_limit"
    assert not sb.alive
    assert unsafe(call_timeout=5).alive


def test_one_huge_allocation_ends_in_memory_limit(unsafe):
    assert blocked(unsafe(call_timeout=20), "xs = [0] * (10 ** 10)") == "memory_limit"


def test_an_infinite_loop_ends_in_time_limit_and_the_process_is_dead(unsafe):
    sb = unsafe(call_timeout=0.4)
    started = time.monotonic()
    assert blocked(sb, "while True:\n    pass") == "time_limit"
    assert time.monotonic() - started < 5
    assert not sb.alive
    with pytest.raises(SandboxError):
        sb.call("signal", ctx=alpha_ctx(INSIDE_UP))


def test_a_loop_inside_numpy_work_also_ends_in_time_limit(unsafe):
    body = "x = sysmod.modules['numpy'].arange(10.0)\nwhile True:\n    x = x * 1.0000001"
    assert blocked(unsafe(call_timeout=0.4), body) == "time_limit"


@POSIX
def test_the_environment_of_the_parent_does_not_reach_the_process(unsafe, monkeypatch):
    monkeypatch.setenv("HOKMA_TEST_SECRET", "s3cret-3414")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "k-3414")
    got = run(unsafe(call_timeout=5), "names = '|'.join(sorted(os.environ.keys()))\nvals = '|'.join(sorted(os.environ.values()))", view_reason="names + '#' + vals")
    text = got["reason"]
    assert "HOKMA_TEST_SECRET" not in text and "s3cret-3414" not in text and "AWS_SECRET" not in text and "k-3414" not in text
    assert "HOME" not in text.split("#")[0].split("|")


@POSIX
def test_the_process_inherits_no_open_handle_of_the_parent(unsafe, tmp_path):
    marker = os.open(str(tmp_path), os.O_RDONLY)
    os.dup2(marker, 77)
    os.set_inheritable(77, True)
    try:
        body = ("opened = []\nfor fd in range(3, 256):\n    try:\n        os.fstat(fd)\n        opened.append(str(fd))\n    except B.OSError:\n        pass")
        got = run(unsafe(call_timeout=5), body, view_reason="'|'.join(opened)")
        assert "77" not in got["reason"].split("|")
        assert marker not in [int(x) for x in got["reason"].split("|") if x]
    finally:
        os.close(77)
        os.close(marker)


@POSIX
def test_the_process_works_in_an_empty_folder(unsafe, tmp_path):
    sb = unsafe(call_timeout=5)
    assert (tmp_path / "empty").is_dir() and not any((tmp_path / "empty").iterdir())
    assert sb.layers["audit_hook"] is True


@POSIX
def test_the_limits_of_the_operating_system_are_on(unsafe):
    sb = unsafe(call_timeout=5)
    assert sb.layers["rlimits"] is True and sb.layers["no_new_privs"] is True and sb.layers["pdeathsig"] is True
    assert sb.layers["audit_hook"] is True and sb.layers["net_namespace"] in ("active", "unavailable")


@POSIX
def test_the_limits_read_from_inside_are_the_ones_of_the_spec(unsafe):
    sb = unsafe(call_timeout=5)
    assert sb.limits == {"as": 1536 * 1024 * 1024, "cpu": 90, "fsize": 0, "core": 0}


@POSIX
def test_the_network_namespace_blocks_the_network_even_without_the_hook(tmp_path):
    """The layer of the operating system on its own: the audit hook is off in this worker, so only the namespace can refuse the connection."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    nohook = [sys.executable, "-I", "-X", "utf8", str(Path(__file__).with_name("nohook_worker.py"))]
    sb = Sandbox(workdir=tmp_path / "empty", worker_argv=nohook, call_timeout=5)
    try:
        layers = sb.start()
        if layers["net_namespace"] != "active":
            pytest.skip(f"this kernel does not give user namespaces: net_namespace is {layers['net_namespace']}")
        sb.load(evil(f"s = sysmod.modules['socket'].socket()\ns.settimeout(2)\ntry:\n    s.connect(('127.0.0.1', {port}))\n    reached = '1'\nexcept B.OSError:\n    reached = '0'",
                     view_reason="reached"), "alpha")
        assert sb.call("signal", ctx=alpha_ctx(INSIDE_UP))["reason"] == "0"
        listener.settimeout(0.3)
        with pytest.raises(socket.timeout):
            listener.accept()
    finally:
        sb.close()
        listener.close()


@POSIX
def test_killing_the_parent_kills_the_process(tmp_path):
    script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from hokma.factory.usercode.sandbox import Sandbox\n"
        f"sb = Sandbox(workdir=Path({str(tmp_path / 'empty')!r}))\n"
        "sb.start()\n"
        "print(sb.pid, flush=True)\n"
        "time.sleep(60)\n")
    parent = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        pid = int(parent.stdout.readline())
        assert Path(f"/proc/{pid}").exists()
        parent.kill()
        parent.wait()
        def is_alive():
            try:
                return "State:\tZ" not in Path(f"/proc/{pid}/status").read_text()
            except OSError:                                                     # gone between the two reads
                return False
        deadline = time.monotonic() + 30                                         # generous: under load the kernel can take seconds to deliver the kill
        while time.monotonic() < deadline and is_alive():
            time.sleep(0.05)
        alive = is_alive()
        assert not alive, "the isolated process outlived Hokma"
    finally:
        parent.kill()


def test_a_violation_ends_the_process_even_if_the_rule_swallowed_the_exception(unsafe):
    sb = unsafe(call_timeout=5)
    body = "try:\n    os.system('true')\nexcept B.BaseException:\n    pass"
    assert blocked(sb, body) == "blocked_process"
    assert not sb.alive


def test_the_safe_worker_never_runs_the_same_code_because_the_static_check_refuses_it(tmp_path):
    with Sandbox(workdir=tmp_path / "empty", call_timeout=5) as sb:
        with pytest.raises(SandboxError) as caught:
            sb.load(evil("os.system('true')"), "alpha")
        assert caught.value.code == "static_check" and caught.value.findings
        assert sb.alive
