"""Project .git/config is untrusted: only a short list of plain keys keeps Git on; nothing may run code."""
import subprocess

import pytest

from hokma.projects import gitops

PLAIN = "[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n"


def repo(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    subprocess.run(["git", "init", "-q", str(p)], check=True)
    (p / "a.txt").write_text("hi")
    return p


CASES = {
    "indented_filter": lambda w: f'  [filter "x"]\n  clean = touch {w}\n',
    "filter_spaces": lambda w: f'[ FILTER   "x" ]\n\tClean = touch {w}\n',
    "mixed_case": lambda w: f'[CoRe]\n\tSshCommand = touch {w}\n',
    "include": lambda w: f"[include]\n\tpath = {w}.cfg\n",
    "includeif": lambda w: f'[includeIf "gitdir:/"]\n\tpath = {w}.cfg\n',
    "alias": lambda w: f"[alias]\n\tx = !touch {w}\n",
    "sshcommand": lambda w: f"[core]\n\tsshCommand = touch {w}\n",
    "editor": lambda w: f"[core]\n\teditor = touch {w}\n",
    "diff_external": lambda w: f"[diff]\n\texternal = touch {w}\n",
    "hookspath": lambda w: f"[core]\n\thooksPath = {w}.d\n",
    "fsmonitor": lambda w: f"[core]\n\tfsmonitor = touch {w}\n",
    "multiline": lambda w: f'[filter "x"]\n\tclean = touch \\\n{w}\n',
    "credential": lambda w: f"[credential]\n\thelper = !touch {w}\n",
    "url": lambda w: '[url "http://evil/"]\n\tinsteadOf = https://x/\n',
}


@pytest.mark.parametrize("name", CASES)
def test_dangerous_config_disables_git_and_runs_nothing(tmp_path, name):
    p = repo(tmp_path)
    pwned = tmp_path / "pwned"
    (p / ".git" / "config").write_text(PLAIN + CASES[name](pwned))
    (p / ".gitattributes").write_text("* filter=x\n")
    assert gitops.repo_state(p) == gitops.GitResult(False, "unsafe")
    assert gitops.commit(p, ["."], "m") == gitops.GitResult(False, "unsafe")
    assert not pwned.exists()


def test_unparseable_config_disables_git(tmp_path):
    p = repo(tmp_path)
    (p / ".git" / "config").write_text("[core\n\tbare = false\n")
    assert gitops.repo_state(p) == gitops.GitResult(False, "unsafe")


def test_global_filter_never_runs(tmp_path, monkeypatch):
    pwned = tmp_path / "pwned"
    cfg = tmp_path / "global.cfg"
    cfg.write_text(f'[filter "x"]\n\tclean = touch {pwned}\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("HOME", str(tmp_path))
    p = repo(tmp_path)
    (p / ".gitattributes").write_text("* filter=x\n")
    assert gitops.commit(p, ["."], "m").ok
    assert not pwned.exists()


def test_plain_git_init_config_works(tmp_path):
    p = repo(tmp_path)
    assert gitops.repo_state(p).ok
    assert gitops.commit(p, ["."], "first").ok
    assert gitops.log(p) == ["first"]


def test_identity_and_branch_keys_allowed(tmp_path):
    p = repo(tmp_path)
    with open(p / ".git" / "config", "a") as f:
        f.write("[user]\n\tname = A\n\temail = a@b.c\n[init]\n\tdefaultBranch = main\n")
    assert gitops.repo_state(p).ok
