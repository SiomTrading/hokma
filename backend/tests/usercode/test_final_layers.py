"""Last round of the sandbox (task 3414): the native loaders and the silent signals leave the process, the module level of a rule cannot call the
builders of the dataclass, and the static check (the main wall) refuses closures, cells, underscore names and pidfd. Each process-level check runs in its own
interpreter because it changes the interpreter."""
import os
import subprocess
import sys

import pytest

from hokma.factory.usercode import static_check, worker
from usercode.support import alpha

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX layers")


def run_python(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60, env={"PYTHONPATH": os.pathsep.join(sys.path)})


def test_ctypes_is_not_importable_and_no_module_holds_it_after_the_trusted_phase():
    done = run_python(
        "import ctypes, sys, types, os, signal\n"
        "import numpy as np\n"
        "from hokma.factory.usercode import worker\n"
        "box = [ctypes]\n"
        "info = worker._drop_native_loaders()\n"
        "assert sys.modules['ctypes'] is None and sys.modules['_ctypes'] is None, 'sys.modules'\n"
        "try:\n    import ctypes\nexcept ImportError:\n    pass\nelse:\n    raise SystemExit('ctypes imported')\n"
        "held = [(n, k) for n, m in list(sys.modules.items()) if isinstance(m, types.ModuleType) for k, v in vars(m).items() if v is box[0]]\n"
        "assert not held, held\n"
        "internal = [m for n, m in sys.modules.items() if n in ('numpy._core._internal', 'numpy.core._internal') and m is not None]\n"
        "assert internal and all(m.ctypes is None for m in internal), 'numpy internal'\n"
        "assert float(np.arange(5.0).sum()) == 10.0 and float(np.percentile(np.arange(5.0), 50)) == 2.0\n"
        "print('OK', info['ctypes_dropped'])\n")
    assert done.returncode == 0 and "OK True" in done.stdout, (done.stdout, done.stderr[-500:])


@POSIX
def test_the_silent_signals_do_not_exist_after_the_trusted_phase():
    done = run_python(
        "import os, signal, sys\n"
        "from hokma.factory.usercode import worker\n"
        "worker._drop_native_loaders()\n"
        "for mod in (os, signal, sys.modules['posix']):\n"
        "    assert not hasattr(mod, 'pidfd_open') and not hasattr(mod, 'pidfd_send_signal'), mod\n"
        "print('OK')\n")
    assert done.returncode == 0 and "OK" in done.stdout, (done.stdout, done.stderr[-500:])


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux layers")
def test_the_worker_is_in_its_own_process_group_on_linux():
    done = run_python(
        "import os\n"
        "from hokma.factory.usercode import worker\n"
        "layers = worker._linux_layers(1536, 90)\n"
        "print('GROUP', layers['own_group'], os.getpgrp() == os.getpid())\n")
    assert "GROUP True True" in done.stdout, (done.stdout, done.stderr[-500:])


def loader_script(direct: bool) -> str:
    """Real hook, real `_load`, the static check turned off: the rule is the only variable. `direct` makes its module level call dataclasses._create_fn, which
    a test put on user_api as `direct` (a rule cannot reach dataclasses another way)."""
    rule = alpha("return ctx.abstain('x')", extra_import="from hokma.factory.user_api import direct" if direct else "",
                 module_extra="direct('f', ('a',), ['return 1'], globals={}, locals={})" if direct else "")
    return ("import dataclasses, sys\n"
            "from hokma.factory import user_api\n"
            "from hokma.factory.usercode import static_check, worker\n"
            "user_api.direct = dataclasses._create_fn\n"
            "worker._warm_up()\n"
            "state = worker._new_state()\n"
            "sys.addaudithook(worker._make_hook(state, worker._make_report(1)))\n"
            f"answer, rule = worker._load({{'source': {rule!r}, 'kind': 'alpha'}}, lambda s, kind: static_check.CheckResult(True, []), state)\n"
            "print('LOADED', sorted(answer), flush=True)\n"
            "import os\nos._exit(0)\n")


def test_the_rule_loads_through_the_decorator_of_the_dataclass():
    done = run_python(loader_script(direct=False))
    assert done.returncode == 0 and "LOADED ['loaded']" in done.stdout, (done.stdout, done.stderr[-500:])


def test_the_module_level_of_the_rule_cannot_call_create_fn_directly():
    done = run_python(loader_script(direct=True))
    assert done.returncode == 3 and "LOADED" not in done.stdout and '"blocked_import"' in done.stdout, (done.returncode, done.stdout, done.stderr[-500:])


@pytest.mark.parametrize("body", [
    "return fact.__closure__", "return fact.__closure__[0].cell_contents", "return ctx.abstain.__closure__", "f = ctx.abstain\nreturn f.__closure__",
    "return ctx.cell_contents", "c = ctx.abstain\nreturn c.cell_contents", "return ctx._create_fn", "return ctx._process_class", "return _create_fn",
    "return ctx.pidfd_open", "return ctx.pidfd_send_signal", "return pidfd_open", "return os.pidfd_open(1)", "return ctx._x", "x = ctx._x\nreturn x",
"return ctx.__x", "return np._core._internal.ctypes",
])
def test_the_static_check_refuses_closures_cells_builders_pidfd_and_underscore_names(body):
    result = static_check.check(alpha(body), kind="alpha")
    assert not result.ok, body


@pytest.mark.parametrize("line", ["import dataclasses", "from dataclasses import _create_fn", "from dataclasses import _process_class", "import signal", "import os"])
def test_the_static_check_refuses_importing_the_builders_and_the_signal_modules(line):
    assert not static_check.check(alpha("return ctx.abstain('x')", extra_import=line), kind="alpha").ok
