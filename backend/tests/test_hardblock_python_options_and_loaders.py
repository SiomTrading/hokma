"""SPEC-015 18.7 (security review of 5280c87). Point 1: the options of the python interpreter are read the way CPython reads
them (combined short groups such as -Im, -IBm, -Ic, options with a value, the launcher, stdin, "--"), so `-m hokma.broker` and
`-c "import hokma.broker"` are caught however the flags are written. Point 2 (text): sys.path set by subscript, os.chdir with
a built path, pickle-like loads next to a sign that Hokma is the target, operator.attrgetter / methodcaller and
functools.partial(getattr ...) on built names. Everything that does not touch Hokma stays free."""
from pathlib import Path

import pytest
from hardblock_support import box  # noqa: F401  (fixture)

from hokma.kit import hardblock
from hokma.kit.hardblock import parse_python_args
from hokma.permissions.broker import BrokerCtx, check

PKG = Path(hardblock.__file__).resolve().parents[2]        # the folder that holds the package `hokma`
CODE = "import hokma.broker"


@pytest.fixture
def bctx(box):
    return BrokerCtx(data=str(box.data), roots=[str(box.work)], protect=[str(box.data)], conversation_id="7")


# ----------------------------------------------------------------------------------- the parser itself

@pytest.mark.parametrize("line, expected", [
    ("-Im x", ("m", "x", 1)), ("-IBm x", ("m", "x", 1)), ("-Ic 1", ("c", "1", 1)), ("-Bc 1", ("c", "1", 1)),
    ("-OOm x", ("m", "x", 1)), ("-Ssm x", ("m", "x", 1)), ("-uc 1", ("c", "1", 1)), ("-Sm x", ("m", "x", 1)),
    ("-Xdev -m x", ("m", "x", 2)), ("-X dev -m x", ("m", "x", 3)), ("-Wdefault -m x", ("m", "x", 2)),
    ("-W default -m x", ("m", "x", 3)), ("-W x -c 1", ("c", "1", 3)), ("-Wc -m x", ("m", "x", 2)),
    ("-cprint", ("c", "print", 0)), ("-mhokma.broker", ("m", "hokma.broker", 0)), ("-Imhokma.broker", ("m", "hokma.broker", 0)),
    ("-3.12 -m x", ("m", "x", 2)), ("-3 -Im x", ("m", "x", 2)), ("-V:Company/3.12 -m x", ("m", "x", 2)),
    ("--check-hash-based-pycs always -m x", ("m", "x", 3)), ("--version -m x", ("m", "x", 2)),
    ("-E -s -Im x", ("m", "x", 3)), ("- ", ("stdin", None, 0)), ("-I -", ("stdin", None, 1)),
    ("-- s.py", ("script", "s.py", 1)), ("-I -- s.py", ("script", "s.py", 2)), ("-E s.py -c", ("script", "s.py", 1)),
    ("-I", ("none", None, 1)), ("-c", ("c", "", 1)),
])
def test_the_parser_reads_the_options_like_cpython(line, expected):
    assert parse_python_args(line.split()) == expected


# ----------------------------------------------------------------------------------- 1: combined options and launchers

MODULE_FORMS = [
    "python -Im hokma.broker", "python -IBm hokma.schwab", "python -OOm hokma.connections", "python -Sm hokma.broker",
    "python -Ssm hokma.broker", "python -um hokma.broker", "python -Xdev -m hokma.broker", "python -X dev -m hokma.broker",
    "python -X importtime -Im hokma.broker", "python -Wdefault -m hokma.broker", "python -W ignore -m hokma.broker",
    "python -Wd -Im hokma.broker", "python -mhokma.broker", "python -Imhokma.broker", "python -E -s -I -m hokma.broker",
    "python --check-hash-based-pycs always -Im hokma.broker", "python3.12 -Im hokma.broker", "python3 -IBm hokma.schwab",
    "py -3 -Im hokma.broker", "py -3.12 -Im hokma.broker", "py -3.12-64 -m hokma.broker", "py -V:3.12 -Im hokma.broker",
    "pythonw -Im hokma.broker", "pythonw.exe -m hokma.broker", "/usr/bin/python3 -Im hokma.broker",
    "/usr/local/bin/python3.12 -IBm hokma.connections", "C:\\Python312\\python.exe -Im hokma.broker", "sudo python -Im hokma.broker",
    "env PYTHONSAFEPATH=1 python -Im hokma.broker", "python -Im hokma", "python -Im hokma.core.config", "python -Im runpy hokma.broker",
]
CODE_FORMS = [
    "python -Ic '{c}'", "python -IBc '{c}'", "python -Bc '{c}'", "python -uc '{c}'", "python -Sc '{c}'", "python -OOc '{c}'",
    "python -Xdev -c '{c}'", "python -X dev -c '{c}'", "python -Wdefault -c '{c}'", "python -W ignore -Ic '{c}'",
    "python -Wd -Ic '{c}'", "python \"-Icimport hokma.broker\"", "python \"-cimport hokma.broker\"", "python -E -s -I -c '{c}'",
    "python --check-hash-based-pycs never -Ic '{c}'", "python3.12 -Ic '{c}'", "py -3 -Ic '{c}'", "py -3.12 -Ic '{c}'",
    "pythonw -Ic '{c}'", "/usr/bin/python3 -Ic '{c}'", "sudo python -IBc '{c}'", "env A=1 python -Ic '{c}'",
    "bash -c \"python -Ic 'import hokma.broker'\"",
]


@pytest.mark.parametrize("command", MODULE_FORMS)
def test_a_hokma_module_run_with_any_option_form_is_real_money(box, command):
    assert box.bash(command) == "real_money", command


@pytest.mark.parametrize("command", CODE_FORMS)
def test_hokma_code_run_with_any_option_form_is_real_money(box, command):
    assert box.bash(command.replace("{c}", CODE)) == "real_money", command


@pytest.mark.parametrize("command", ["python -Ic '{c}'", "python -Xdev -Bc '{c}'", "py -3 -OOc '{c}'", "python3.12 -uc '{c}'"])
@pytest.mark.parametrize("code", ["import importlib; importlib.import_module('hok' + 'ma.broker')", "exec(chr(105) + 'mport os')"])
def test_built_code_in_a_combined_group_is_judged_too(box, command, code):
    assert box.bash(command.replace("{c}", code)) == "real_money", (command, code)


def test_a_script_given_after_options_or_double_dash_is_still_read(box):
    (box.work / "tool.py").write_text(CODE + "\n")
    for command in ("python -I tool.py", "python -Xdev -Bu tool.py", "python -- tool.py", "python -I -- tool.py",
                    "py -3 -I tool.py", "python -W ignore tool.py", "python --check-hash-based-pycs always tool.py"):
        assert box.bash(command) == "real_money", command


def test_stdin_after_options_is_still_read(box):
    for command in (f"python -I - <<'EOF'\n{CODE}\nEOF", f"python -Xdev <<'EOF'\n{CODE}\nEOF",
                    f"echo '{CODE}' | python -I -", f"echo '{CODE}' | python -Bu"):
        assert box.bash(command) == "real_money", command


def test_the_hook_process_and_the_permission_broker_agree(box, bctx):
    for command in ("python -Im hokma.broker", "python -Ic 'import hokma.schwab'", "py -3 -IBm hokma.connections"):
        done = box.run_hook({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(box.work)})
        assert done.returncode == 2 and "HOKMA_BLOCKED real_money" in done.stderr, command
        verdict = check("Bash", {"command": command}, cwd=str(box.work), ctx=bctx)
        assert (verdict.allowed, verdict.rule) == (False, "real_money"), command


FREE_PYTHON = [
    "python -Im pytest -q", "python -Im venv .venv", "python -Xdev -m pytest", "python -X dev -m pytest -q", "python -W error -m pytest",
    "python -Wdefault -m pip list", "python -OOm compileall .", "python -Sm site", "python -um http.server", "python -m hokma doctor",
    "python -Im hokma doctor", "python -mhokma doctor", "python -Ic 'print(1)'", "python -Ic 'import json'", "python -IBc 'import os'",
    "python -uc 'print(2)'", "python -Xdev -c 'print(3)'", "python -Wdefault -c 'import sys'", "python3.12 -Im json.tool",
    "py -3 -m pip list", "py -3.12 -Ic 'print(1)'", "py -V:Company/3.12 -c 'print(1)'", "pythonw -Ic 'print(1)'",
    "/usr/bin/python3 -Im pip --version", "python -V", "python --version", "python -I app.py", "python -- app.py",
    "python -Ic 'import hokma_extras'", "python -Im hokma_extras", "python -Ic 'import schwab'", "python -Im hokma.doctor", "python -Im hokma.factory.rule_check x",
    "python -Ic 'import hokma.factory.strategy'",
]


@pytest.mark.parametrize("command", FREE_PYTHON)
def test_legitimate_python_command_lines_still_pass(box, command):
    assert box.bash(command) is None, command


# ----------------------------------------------------------------------------------- 2: sys.path by subscript

PATH_SUBSCRIPT_BLOCKED = [
    "import sys\nsys.path[0:0] = ['/data/runtime/a/venv/lib/python3.12/site-packages']\n",
    "import sys, os\nsys.path[0] = os.path.join(base, 'runtime', 'a', 'venv')\n",
    "import sys\nsys.path[:] = [venv_site] + sys.path\n",
    "import sys\nsys.path[0] += ':/x/site-packages'\n",
    "import sys\nsys.path.__setitem__(0, '/x/site-packages')\n",
    "import sys\nsys.path.__iadd__(['/x/site-packages'])\n",
]


@pytest.mark.parametrize("body", PATH_SUBSCRIPT_BLOCKED)
def test_sys_path_set_by_subscript_to_hokma_folders_is_real_money(box, body):
    assert box.hit("Write", file_path=str(box.work / "a.py"), content=body) == "real_money"
    (box.work / "b.py").write_text(body)
    assert box.bash("python b.py") == "real_money"


@pytest.mark.parametrize("body", [
    "import sys\nsys.path[0] = 'src'\n", "import sys\nsys.path[:] = ['lib'] + sys.path\n", f"import sys\nsys.path[0] = '{PKG}'\n", f"import sys\nsys.path[1:1] = ['{PKG}']\n",          # 1.2.9: the folder of the package is free to put on the path
    "import sys, os\nsys.path[0] = os.path.dirname(__file__)\n", "import sys\nsys.path.insert(0, 'src')\n",
    "import sys\nprint(sys.path[0])\nx = sys.path[1] == 'a'\n"])
def test_a_student_own_path_changes_stay_free(box, body):
    assert box.hit("Write", file_path=str(box.work / "a.py"), content=body) is None


# ----------------------------------------------------------------------------------- 2: os.chdir

CHDIR_BLOCKED = [
    "import os\nos.chdir(target)\n", "import os\nos.chdir(os.environ['HOME'] + '/x')\n",
    "import os\nos.chdir(os.path.join(a, b))\n", "import os\nos.chdir(sys.argv[1])\n", "from os import chdir\nchdir(p)\n",
    "import contextlib\nwith contextlib.chdir(p):\n    pass\n", "import os\nos.fchdir(fd)\n",
    f"import os\nos.chdir('{PKG}')\n", "import os\nos.chdir('/data/runtime/a/venv')\n",
    "import os\nos.chdir('x/site-packages')\n", "import os\nos.chdir(os.path.join(os.path.dirname(__file__), 'x'))\n",
]


@pytest.mark.parametrize("body", CHDIR_BLOCKED)
def test_chdir_with_a_built_path_or_into_hokma_is_real_money(box, body):
    assert box.hit("Write", file_path=str(box.work / "a.py"), content=body) == "real_money"
    assert box.bash("python -c '" + body.replace("'", "'\"'\"'") + "'") == "real_money"


@pytest.mark.parametrize("body", [
    "import os\nos.chdir('data')\n", "import os\nos.chdir('..')\n", "import os\nos.chdir('app/data')\n",
    "import os\nos.chdir(os.path.dirname(__file__))\n", "import os\nos.chdir(os.path.dirname(os.path.abspath(__file__)))\n",
    "import os\nfrom pathlib import Path\nos.chdir(Path(__file__).resolve().parent)\n",
    "def chdir(p):\n    return p\n", "def test_x(monkeypatch, tmp_path):\n    monkeypatch.chdir(tmp_path)\n",
    "print('chdir is a word')\n"])
def test_a_literal_or_own_file_chdir_stays_free(box, body):
    assert box.hit("Write", file_path=str(box.work / "a.py"), content=body) is None


# ----------------------------------------------------------------------------------- 2: pickle and friends

PICKLE_BLOCKED = [
    "import pickle, base64\nobj = pickle.loads(base64.b64decode(blob))\n",
    "import pickle\nobj = pickle.load(open('/x/hokma/broker.pkl', 'rb'))\n",
    "import marshal\ncode = marshal.loads(bytes.fromhex('00'))\n", "import marshal\nblob = marshal.loads(raw)\n", "import dill\nobj = dill.loads(b'\\x80\\x04')\n",
    "import cloudpickle, zlib\nobj = cloudpickle.loads(zlib.decompress(data))\n",
    "from pickle import loads\nobj = loads(base64.b64decode(x))\n", "import _pickle\nobj = _pickle.loads(binascii.unhexlify(h))\n",
    "import pickle\nobj = pickle.loads(b''.join(parts))\n", "import cPickle\nobj = cPickle.loads(codecs.decode(s, 'rot13'))\n",
    "import shelve\ndb = shelve.open('/x/runtime/a/venv/db')\n", "import pickle\nobj = pickle.Unpickler(f).load()  # hokma\n",
    "import pickle\nobj = pickle.loads(bytes([99, 104]))\n", "import pickle\nobj = pickle.loads(bytearray(b))\n",
    "import pickle\nobj = pickle.loads(chr(99).encode())\n",
]


@pytest.mark.parametrize("body", PICKLE_BLOCKED)
def test_pickle_like_loads_with_a_sign_of_hokma_are_real_money(box, body):
    assert box.hit("Write", file_path=str(box.work / "a.py"), content=body) == "real_money"
    (box.work / "b.py").write_text(body)
    assert box.bash("python b.py") == "real_money"


@pytest.mark.parametrize("body", [
    "import pickle\nmodel = pickle.load(open('model.pkl', 'rb'))\n", "import pickle\nobj = pickle.loads(data)\n",
    "import pickle, os\nmodel = pickle.load(open(os.path.join('models', 'm.pkl'), 'rb'))\n",
    "import joblib\nmodel = joblib.load('model.joblib')\n", "import pickle\nwith open('o.pkl', 'wb') as f:\n    pickle.dump(x, f)\n",
    "import shelve\ndb = shelve.open('cache')\n",
    "import pickle, gzip\nmodel = pickle.load(gzip.open('m.pkl.gz'))\n", "import dill\nfn = dill.load(open('f.dill', 'rb'))\n",
    "from pickle import load\nmodel = load(open('m.pkl', 'rb'))\n", "import pickle\nrows = pickle.load(f)\nprint(', '.join(rows))\n"])
def test_loading_the_student_own_model_stays_free(box, body):
    assert box.hit("Write", file_path=str(box.work / "a.py"), content=body) is None
    (box.work / "b.py").write_text(body)
    assert box.bash("python b.py") is None


# ----------------------------------------------------------------------------------- 2: attrgetter, methodcaller, partial

CALLER_BLOCKED = [
    "import operator\nf = operator.attrgetter(name)\nf(obj)\n", "from operator import attrgetter\nf = attrgetter(name)\n",
    "import operator\nf = operator.attrgetter('a.' + part)\n", "import operator\nf = operator.methodcaller(fn, 1)\n",
    "import operator\nf = operator.attrgetter('import_module')\n", "import operator\nf = operator.attrgetter('__import__')\n",
    "import operator\nf = operator.methodcaller('eval', 'x')\n", "import operator\nf = operator.methodcaller('exec', c)\n",
    "import operator\nf = operator.attrgetter('__globals__.__builtins__')\n", "import operator\nf = operator.attrgetter('importlib.util')\n",
    "import operator\nf = operator.attrgetter(*names)\n", "import operator\nf = operator.attrgetter('x.hokma')\n",
    "import functools\ng = functools.partial(getattr, importlib)\n", "from functools import partial\ng = partial(getattr, mod)\n",
    "from functools import partial\ng = partial(getattr, mod, name)\n", "from functools import partial\ng = partial(eval, src)\n",
    "from functools import partial\ng = partial(exec, src)\n", "from functools import partial\ng = partial(__import__, n)\n",
    "from functools import partial\ng = partial(getattr, importlib, 'import_module')\n", "import functools\ng = functools.partial(getattr)\n",
    "import functools, builtins\ng = functools.partial(builtins.eval, s)\n",
]


@pytest.mark.parametrize("body", CALLER_BLOCKED)
def test_attrgetter_methodcaller_and_partial_on_built_names_are_real_money(box, body):
    assert box.hit("Write", file_path=str(box.work / "a.py"), content=body) == "real_money"
    (box.work / "b.py").write_text(body)
    assert box.bash("python b.py") == "real_money"


@pytest.mark.parametrize("body", [
    "import operator\nrows.sort(key=operator.attrgetter('price'))\n", "from operator import attrgetter\nkey = attrgetter('a.b')\n",
    "from operator import attrgetter\nkey = attrgetter('price', 'volume')\n", "import operator\nf = operator.methodcaller('strip')\n",
    "import operator\nf = operator.methodcaller('split', ',')\n", "import operator\nf = operator.itemgetter(1)\n",
    "from functools import partial\ng = partial(getattr, obj, 'x')\n", "from functools import partial\ng = partial(print, 'a')\n",
    "from functools import partial\ng = partial(int, base=2)\n", "def attrgetter(x):\n    return x\n"])
def test_plain_literal_use_of_those_helpers_stays_free(box, body):
    assert box.hit("Write", file_path=str(box.work / "a.py"), content=body) is None


def test_the_new_rules_also_reach_mcp_arguments_through_the_broker(box, bctx):
    for tool, arguments in (("mcp__other__run", {"code": "import os\nos.chdir(p)\n"}),
                            ("mcp__other__run", {"code": "import pickle\npickle.loads(base64.b64decode(b))"}),
                            ("mcp__other__run", {"code": "import operator\noperator.attrgetter(n)(o)"}),
                            ("mcp__other__run", {"code": "import sys\nsys.path[0] = venv_site"})):
        verdict = check(tool, arguments, cwd=str(box.work), ctx=bctx)
        assert (verdict.allowed, verdict.rule) == (False, "real_money"), (tool, arguments)
