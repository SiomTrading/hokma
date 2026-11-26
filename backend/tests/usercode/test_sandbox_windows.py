"""The adversarial tests of Windows (SPEC-031 sections 6.3, 6.4 and 10): the Job Object and the hook, with no operating system network layer.

They run ONLY on Windows. The author of this session had no Windows machine: they were written and NOT executed. Run them with
`pytest backend/tests/usercode/test_sandbox_windows.py` on a Windows machine and attach the output to the PR.
"""
import sys
from pathlib import Path

import pytest

from hokma.factory.usercode.sandbox import Sandbox, SandboxError
from usercode.support import INSIDE_UP, alpha_ctx
from usercode.test_sandbox_adversarial import UNSAFE, evil

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only: Job Object layers (NOT run by the author, who had no Windows)")


@pytest.fixture
def box(tmp_path):
    sb = Sandbox(workdir=tmp_path / "empty", worker_argv=UNSAFE, call_timeout=5)
    sb.start()
    yield sb
    sb.close()


def blocked(sb, body):
    sb.load(evil(body), "alpha")
    with pytest.raises(SandboxError) as caught:
        sb.call("signal", ctx=alpha_ctx(INSIDE_UP))
    return caught.value.code


def test_the_layers_say_job_object_and_no_network_namespace(box):
    assert box.layers["job_object"] is True and box.layers["audit_hook"] is True
    assert box.layers["net_namespace"] == "not_supported" and "rlimits" not in box.layers or box.layers.get("rlimits") is False


def test_the_process_is_in_the_job_before_the_rule_loads(box):
    assert box.in_job is True


def test_a_second_process_is_refused_by_the_job_and_by_the_hook(box):
    assert blocked(box, "os.system('cmd /c echo x')") == "blocked_process"


def test_a_socket_and_a_file_are_blocked_by_the_hook(tmp_path):
    for body, code in (("sysmod.modules['socket'].socket()", "blocked_network"), ("sysmod.modules['io'].open('C:/Windows/win.ini').read()", "blocked_file")):
        with Sandbox(workdir=tmp_path / "e", worker_argv=UNSAFE, call_timeout=5) as sb:
            assert blocked(sb, body) == code


def test_memory_and_time_are_limited_by_the_job(tmp_path):
    with Sandbox(workdir=tmp_path / "e", worker_argv=UNSAFE, call_timeout=30) as sb:
        assert blocked(sb, "xs = []\nwhile True:\n    xs.append([0] * 1000000)") == "memory_limit"
    with Sandbox(workdir=tmp_path / "e2", worker_argv=UNSAFE, call_timeout=0.4) as sb:
        assert blocked(sb, "while True:\n    pass") == "time_limit"


def test_without_a_job_object_the_sandbox_refuses_to_run_generated_code(tmp_path, monkeypatch):
    from hokma.core import winjob

    def refuse(self, handle):
        raise OSError("no job")

    monkeypatch.setattr(winjob.SandboxJob, "assign", refuse)
    sb = Sandbox(workdir=tmp_path / "e", call_timeout=5)
    with pytest.raises(SandboxError) as caught:
        sb.start()
    assert caught.value.code == "unavailable" and "cannot be limited" in caught.value.message
    assert not sb.alive


def test_the_environment_has_only_what_windows_needs(box):
    body = "names = '|'.join(sorted(os.environ.keys()))"
    sb = box
    sb.load(evil(body).replace("return view(ctx, 'u_evil', 0.0, (), x)", "return view(ctx, 'u_evil', 0.0, (), names)"), "alpha")
    names = sb.call("signal", ctx=alpha_ctx(INSIDE_UP))["reason"].split("|")
    assert set(n.upper() for n in names) <= {"PYTHONIOENCODING", "PYTHONDONTWRITEBYTECODE", "PYTHONHASHSEED", "SYSTEMROOT", "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"}
