"""The command line of Hokma (bug 1228) is not judged by the hook since 1.3.1 (task 3436): what stays free here stays free, and what was refused (`hokma engine`, the server
entry points, `partial` and `marshal`) is free too. Only the calls to a broker are `real_money`."""
import pytest
from hardblock_support import box  # noqa: F401  (fixture)



@pytest.mark.parametrize("command", [
    "hokma doctor", "hokma version", "hokma ext request plugin foo@bar", "hokma ext request mcp x", "hokma ext requests",
    "hokma --help", "hokma -h", "hokma --version", "hokma -V", "hokma", "python -m hokma_cli.main --version", "uv run hokma doctor",
    "uv run pytest -q", "HOKMA DOCTOR", "hokma.exe doctor", "/usr/local/bin/hokma doctor", "./hokma version",
    "python -m hokma_cli doctor", "python -m hokma_cli.main version", "python -m hokma_cli ext request plugin a@b",
    "python -m hokma doctor", "sudo hokma doctor", "env X=1 hokma version", "timeout 5 hokma doctor",
    "bash -c 'hokma doctor'", "hokma doctor | head -5", "hokma version && echo ok", "python /x/hokma doctor",
    "python -c 'import subprocess; subprocess.run([\"hokma\", \"doctor\"])'",
    "python -c 'import subprocess; subprocess.run([\"hokma\", \"ext\", \"requests\"])'",
    "python -c 'import os; os.system(\"hokma version\")'"])
def test_the_read_only_commands_stay_free(box, command):
    assert box.bash(command) is None


@pytest.mark.parametrize("command", [
    "./hokma_notes.sh", "bash hokma_notes.sh", "cat hokma_notes.md", "echo hokma engine", "printf 'hokma engine\\n'",
    "git status", "git commit -m 'run hokma engine later'", "node app.js", "python script.py", "python3 -m pytest",
    "python -c 'print(1)'", "ls", "grep -r engine .", "python engine.py", "python main.py engine", "python my_engine_main.py",
    "python -m http.server 8000", "echo trash_mcp is a word", "python hokma_tool.py engine", "ls engine_main.py.bak",
    "python -c 'import subprocess; subprocess.run([\"git\", \"status\"])'", "mkdir hokma_notes", "pip install hokma-extras"])
def test_things_that_are_not_the_cli_stay_free(box, command):
    (box.work / "hokma_notes.sh").write_text("echo notes\n")
    (box.work / "script.py").write_text("print(1)\n")
    assert box.bash(command) is None


# ------------------------------------------------------------------------------------- partial(getattr ...) and marshal


@pytest.mark.parametrize("body", [
    "import pickle\nmodel = pickle.load(open('model.pkl', 'rb'))\n", "import joblib\nmodel = joblib.load('m.joblib')\n",
    "from functools import partial\ng = partial(getattr, obj, 'x')\n", "from functools import partial\ng = partial(print, 'a')\n",
    "from functools import partial\ng = partial(int, base=2)\n", "import functools\nf = functools.partial(max, key=len)\n",
    "from functools import partial as p\ng = p(round, ndigits=2)\n", "print('marshaling the data')\n",
    "# marshal is not used here\nx = 1\n", "import json\nx = json.loads(s)\n",
    "def partial(x):\n    return x\n", "partial_sum = sum(xs)\n"])
def test_the_students_own_models_and_plain_partials_stay_free(box, body):
    assert box.hit("Write", file_path=str(box.work / "a.py"), content=body) is None
    (box.work / "b.py").write_text(body)
    assert box.bash("python b.py") is None
