"""SPEC-015 18.7 (security review of 5280c87), reduced to what is free in 1.3.1 (task 3436): the options of the python interpreter are read the way CPython reads them
(combined short groups such as -Im, -IBm, -Ic, options with a value, the launcher, stdin, "--") so the code of `-c` and of a script is still read for what stays refused (delete, the
stores of keys, the hosts of brokers); and what the hook refused in 1.3.0 about Hokma (its modules, sys.path, chdir, pickle, attrgetter, partial) is free."""
from pathlib import Path

import pytest
from hardblock_support import box  # noqa: F401  (fixture)

from hokma.kit import hardblock
from hokma.kit.hardblock import parse_python_args

PKG = Path(hardblock.__file__).resolve().parents[2]        # the folder that holds the package `hokma`


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


@pytest.mark.parametrize("body", [
    "import sys\nsys.path[0] = 'src'\n", "import sys\nsys.path[:] = ['lib'] + sys.path\n", f"import sys\nsys.path[0] = '{PKG}'\n", f"import sys\nsys.path[1:1] = ['{PKG}']\n",          # 1.2.9: the folder of the package is free to put on the path
    "import sys, os\nsys.path[0] = os.path.dirname(__file__)\n", "import sys\nsys.path.insert(0, 'src')\n",
    "import sys\nprint(sys.path[0])\nx = sys.path[1] == 'a'\n"])
def test_a_student_own_path_changes_stay_free(box, body):
    assert box.hit("Write", file_path=str(box.work / "a.py"), content=body) is None


# ----------------------------------------------------------------------------------- 2: os.chdir


@pytest.mark.parametrize("body", [
    "import os\nos.chdir('data')\n", "import os\nos.chdir('..')\n", "import os\nos.chdir('app/data')\n",
    "import os\nos.chdir(os.path.dirname(__file__))\n", "import os\nos.chdir(os.path.dirname(os.path.abspath(__file__)))\n",
    "import os\nfrom pathlib import Path\nos.chdir(Path(__file__).resolve().parent)\n",
    "def chdir(p):\n    return p\n", "def test_x(monkeypatch, tmp_path):\n    monkeypatch.chdir(tmp_path)\n",
    "print('chdir is a word')\n"])
def test_a_literal_or_own_file_chdir_stays_free(box, body):
    assert box.hit("Write", file_path=str(box.work / "a.py"), content=body) is None


# ----------------------------------------------------------------------------------- 2: pickle and friends


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


@pytest.mark.parametrize("body", [
    "import operator\nrows.sort(key=operator.attrgetter('price'))\n", "from operator import attrgetter\nkey = attrgetter('a.b')\n",
    "from operator import attrgetter\nkey = attrgetter('price', 'volume')\n", "import operator\nf = operator.methodcaller('strip')\n",
    "import operator\nf = operator.methodcaller('split', ',')\n", "import operator\nf = operator.itemgetter(1)\n",
    "from functools import partial\ng = partial(getattr, obj, 'x')\n", "from functools import partial\ng = partial(print, 'a')\n",
    "from functools import partial\ng = partial(int, base=2)\n", "def attrgetter(x):\n    return x\n"])
def test_plain_literal_use_of_those_helpers_stays_free(box, body):
    assert box.hit("Write", file_path=str(box.work / "a.py"), content=body) is None


