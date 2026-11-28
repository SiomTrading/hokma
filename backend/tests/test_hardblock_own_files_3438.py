"""Task 3438 (Hokma 1.3.3): an agent deletes ONLY what it created in the same chat session. Everything else about the delete rule stays as it was."""
import json
import os
from pathlib import Path

import pytest
from hardblock_support import box  # noqa: F401  (fixture)

from hokma.kit import hardblock


def call(box, tool, session="s1", cwd=None, **tool_input):
    """One call of the hook, the way it runs: a new context each time, the session file is the only memory."""
    payload = {"tool_name": tool, "tool_input": tool_input, "cwd": str(cwd or box.work), "session_id": session}
    block = hardblock.evaluate(payload, box.ctx(cwd))
    return block.rule if block else None


def bash(box, command, session="s1", cwd=None):
    return call(box, "Bash", session, cwd, command=command)


def listed(box, session="s1"):
    f = box.data / "guard" / "created" / (session + ".jsonl")
    return [json.loads(x)["p"] for x in f.read_text().splitlines()] if f.exists() else []


def make(path: Path, text="x\n"):
    """What the tool that was allowed would have done on disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def fresh(box, path: Path, session="s1"):
    """The file is really gone from the disk (a delete that the hook allowed would have removed it), and the session creates it again."""
    if path.exists():
        path.unlink()
    write_new(box, path, session)


def write_new(box, path: Path, session="s1"):
    assert call(box, "Write", session, file_path=str(path), content="x\n") is None
    make(path)


def test_the_real_case_write_a_temp_file_outside_the_workdir_then_rm_f(box):
    lab = box.tmp / "lab" / "orderflow" / "tools"
    lab.mkdir(parents=True)
    f = lab / "_sdx_before_t66.tmp"
    write_new(box, f)
    assert listed(box) == [os.path.realpath(f)]
    assert bash(box, f"rm -f {f}") is None
    g = lab / "_sdx_after_t66.tmp"
    write_new(box, g)
    assert bash(box, "rm -f ../lab/orderflow/tools/_sdx_after_t66.tmp") is None


def test_the_real_case_through_the_real_hook_process(box):
    f = box.tmp / "lab" / "_sdx.tmp"
    f.parent.mkdir()
    ok = box.run_hook({"tool_name": "Write", "tool_input": {"file_path": str(f), "content": "x"}, "cwd": str(box.work), "session_id": "abc"})
    assert ok.returncode == 0, ok.stderr
    make(f)
    ok = box.run_hook({"tool_name": "Bash", "tool_input": {"command": f"rm -f {f}"}, "cwd": str(box.work), "session_id": "abc"})
    assert ok.returncode == 0, ok.stderr
    other = box.run_hook({"tool_name": "Bash", "tool_input": {"command": f"rm -f {f}"}, "cwd": str(box.work), "session_id": "zzz"})
    assert other.returncode == 2 and "HOKMA_BLOCKED delete" in other.stderr


def test_a_file_that_already_existed_stays_protected(box):
    assert bash(box, "rm notes.md") == "delete"
    assert bash(box, "rm -f f") == "delete"
    assert call(box, "Write", file_path=str(box.work / "notes.md"), content="y") is None     # overwriting is not creating
    assert listed(box) == []
    assert bash(box, "rm notes.md") == "delete"


def test_a_file_created_in_another_session_stays_protected(box):
    f = box.work / "mine.txt"
    write_new(box, f, session="s1")
    assert bash(box, "rm mine.txt", session="s1") is None
    assert bash(box, "rm mine.txt", session="s2") == "delete"
    assert bash(box, "rm mine.txt", session="") == "delete"
    assert call(box, "Bash", session="../x", command="rm mine.txt") == "delete"


def test_a_folder_with_something_not_created_in_the_session_stays_protected(box):
    write_new(box, box.work / "newdir" / "a.txt")
    make(box.work / "newdir" / "foreign.txt")                       # appears by other means
    assert bash(box, "rm -rf newdir") == "delete"
    assert bash(box, "rm -rf d") == "delete"                         # existed before, has an inner file of the owner
    assert bash(box, "rm -rf informes") == "delete"
    assert bash(box, "rm -rf .") == "delete"
    assert bash(box, "rm -rf ..") == "delete"
    assert bash(box, "rm -rf /") == "delete"


def test_a_folder_that_holds_only_what_the_session_created_can_go(box):
    write_new(box, box.work / "tmpdir" / "a.txt")
    write_new(box, box.work / "tmpdir" / "sub" / "b.txt")
    assert bash(box, "rm -rf tmpdir") is None
    assert bash(box, "rm -rf d/inner.txt") == "delete"


def test_a_pattern_is_read_against_the_disk(box):
    write_new(box, box.work / "p1.log")
    write_new(box, box.work / "p2.log")
    assert bash(box, "rm p*.log") is None
    write_new(box, box.work / "o1.log")
    write_new(box, box.work / "o2.log")
    make(box.work / "o3.log")                                        # not created by the session
    assert bash(box, "rm o*.log") == "delete"
    assert bash(box, "rm o1.log o3.log") == "delete"
    assert bash(box, "rm o1.log o2.log") is None
    assert bash(box, "rm *.md") == "delete"
    assert bash(box, "rm *") == "delete"
    assert bash(box, "rm nothing-*.zzz") == "delete"
    assert bash(box, "rm {o1,o3}.log") == "delete"


def test_names_built_at_run_time_stay_protected(box):
    write_new(box, box.work / "own.txt")
    for cmd in ("rm $x", 'rm "$(echo own.txt)"', "x=own.txt; rm $x", "rm own.txt $HOME", "rm $(ls)", "eval rm own.txt", "rm", "rm -rf", "rm -f --",
                "rm --no-preserve-root own.txt", "rm -z own.txt", "find . -name own.txt -delete", "shred own.txt", "mv own.txt /dev/null"):
        assert bash(box, cmd) == "delete", cmd


def test_the_protected_places_stay_protected_even_when_listed(box):
    guard_file = box.data / "guard" / "x.txt"
    hook = box.hooks / "hardblock.py"
    for victim in (guard_file, box.data / "database.json", hook, box.data / "bin" / "b", box.data / "tools" / "t"):
        victim.parent.mkdir(parents=True, exist_ok=True)
        victim.write_text("x")
        made = box.data / "guard" / "created"
        made.mkdir(exist_ok=True)
        with open(made / "s1.jsonl", "a") as f:
            f.write(json.dumps({"p": os.path.realpath(victim)}) + "\n")
        assert bash(box, f"rm -f {victim}") in ("delete", "secrets"), victim
    for folder in (box.data / "guard", box.hooks, box.data):
        assert bash(box, f"rm -rf {folder}") in ("delete", "secrets"), folder


def test_a_call_that_is_refused_notes_nothing(box):
    before = listed(box)
    assert bash(box, f"cat {box.data}/identity.key > {box.work}/leak.txt") == "secrets"
    assert listed(box) == before
    assert bash(box, "rm leak.txt") == "delete"


def test_what_a_shell_creates_is_noted(box):
    assert bash(box, "echo hi > a.out") is None
    assert bash(box, "echo hi >> b.out") is None
    assert bash(box, "echo hi | tee c.out") is None
    assert bash(box, "touch d.out") is None
    for n in "abcd":
        make(box.work / f"{n}.out")
    assert bash(box, "cp notes.md e.out") is None
    make(box.work / "e.out")
    assert bash(box, "mkdir fresh") is None
    (box.work / "fresh").mkdir()
    for cmd in ("rm a.out b.out c.out e.out", "rmdir fresh", "unlink d.out"):
        assert bash(box, cmd) is None, cmd
    assert bash(box, "mkdir fresh2") is None
    (box.work / "fresh2").mkdir()
    assert bash(box, "rm -rf fresh2") is None
    assert bash(box, "cp notes.md d/inner.txt") is None
    assert bash(box, "rm d/inner.txt") == "delete"                   # it existed: it was overwritten, not created
    assert bash(box, "touch notes.md") is None
    assert bash(box, "rm notes.md") == "delete"


def test_moving_a_file_of_the_owner_does_not_make_it_the_agents(box):
    assert bash(box, "mv notes.md stolen.md") is None
    make(box.work / "stolen.md")
    assert bash(box, "rm stolen.md") == "delete"
    write_new(box, box.work / "mine.txt")
    assert bash(box, "mv mine.txt moved.txt") is None
    make(box.work / "moved.txt")
    assert bash(box, "rm moved.txt") is None


def test_create_and_delete_in_the_same_line(box):
    # 1.3.3 simplified: a line that mixes a delete with anything else is judged as before (refused)
    assert bash(box, "echo x > t.tmp && cat t.tmp && rm t.tmp") == "delete"
    assert bash(box, "echo x > notes.md; rm notes.md") == "delete"
    assert bash(box, "mkdir w && cd w && echo x > i.txt && cd .. && rm -rf w") == "delete"
    assert bash(box, "bash -c 'echo x > u.tmp; rm u.tmp'") == "delete"
    assert bash(box, "rm f; echo x > f") == "delete"


def test_other_delete_forms(box):
    write_new(box, box.work / "p.txt")
    assert bash(box, "sudo rm -f p.txt") == "delete"            # a wrapper is another command in the line
    assert bash(box, "bash -c 'rm -f p.txt'") == "delete"
    assert bash(box, "xargs rm < p.txt") == "delete"
    assert bash(box, "ls | xargs rm") == "delete"
    assert bash(box, "find . -name p.txt -exec rm {} ;") == "delete"
    assert bash(box, "del p.txt") is None
    fresh(box, box.work / "p.txt")
    assert call(box, "PowerShell", command="Remove-Item -Force p.txt") is None
    fresh(box, box.work / "p.txt")
    assert call(box, "PowerShell", command="Remove-Item -Path p.txt -Force") is None
    assert call(box, "PowerShell", command="Remove-Item notes.md") == "delete"
    assert call(box, "PowerShell", command="Remove-Item -Recurse -Force d") == "delete"
    assert call(box, "PowerShell", command="[System.IO.File]::Delete('p.txt')") == "delete"


def test_python_never_deletes_not_even_its_own(box):
    write_new(box, box.work / "q.txt")
    for code in ("import os; os.remove('q.txt')", "import os; os.remove('notes.md')", "import os; p='q.txt'; os.remove(p)",
                 "import shutil; shutil.rmtree('d')", "import os; os.unlink('q.txt')"):
        assert bash(box, f"python -c \"{code}\"") == "delete", code
    (box.work / "s.py").write_text("import os\nos.remove('q.txt')\n")
    assert bash(box, "python s.py") == "delete"


def test_only_deletes_chained_with_semicolon_or_and_are_judged_as_own(box):
    def both():
        fresh(box, box.work / "a.tmp")
        fresh(box, box.work / "b.tmp")

    for ok in ("rm a.tmp && rm b.tmp", "rm a.tmp; rm b.tmp", "rm a.tmp\nrm b.tmp"):
        both()
        assert bash(box, ok) is None, ok
    for cmd in ("rm a.tmp; ls", "rm a.tmp | cat", "rm a.tmp > o.txt", "rm a.tmp && mv -t d notes.md", "rm -rf d && cp -a f g", "X=1 rm a.tmp",
                "rm $(echo a.tmp)"):
        both()
        assert bash(box, cmd) == "delete", cmd


def test_a_link_inside_an_own_folder_blocks_its_delete(box):
    write_new(box, box.work / "w" / "i.txt")
    (box.work / "carpeta_ajena").mkdir()
    os.symlink(box.work / "carpeta_ajena", box.work / "w" / "l")
    assert bash(box, "rm -rf w") == "delete"
    (box.work / "w" / "l").unlink()
    assert call(box, "Write", file_path=str(box.work / "w" / "i.txt"), content="y") is None
    assert bash(box, "rm -rf w") is None


def test_without_a_session_nothing_is_noted_and_nothing_is_allowed(box):
    f = box.work / "z.txt"
    assert call(box, "Write", session="", file_path=str(f), content="x") is None
    make(f)
    assert not (box.data / "guard" / "created").exists()
    assert bash(box, "rm z.txt", session="") == "delete"


def test_the_agent_cannot_write_the_list(box):
    where = str(box.data) + "/guard/created/s1.jsonl"
    for cmd in ("echo x >> " + where, "touch " + where, "rm " + where):
        assert bash(box, cmd) in ("hokma_internal", "delete"), cmd


# ---- identity of the created file (Vera, round 2): a file put over a listed path is not the one the session created

def test_a_foreign_file_moved_over_an_own_file_is_not_deleted(box):
    sub = box.work / "sub"
    sub.mkdir()
    precious = box.work / "mi_indicador.cs"
    make(precious, "precious")
    write_new(box, sub / "tmp.txt")
    assert bash(box, "mv ../mi_indicador.cs tmp.txt", cwd=sub) == "delete"  # refused up front; the swap below is done by hand, as another tool would
    os.replace(precious, sub / "tmp.txt")
    assert bash(box, "rm tmp.txt", cwd=sub) == "delete"
    assert bash(box, "rm -f sub/tmp.txt") == "delete"
    assert (sub / "tmp.txt").read_text() == "precious"


def test_a_copy_over_an_own_file_keeps_the_identity_and_can_go(box):
    make(box.work / "ajeno.cs", "precious")
    write_new(box, box.work / "t2.txt")
    assert bash(box, "cp ajeno.cs t2.txt") is None
    (box.work / "t2.txt").write_text("precious")                            # cp opens the same inode
    assert bash(box, "rm t2.txt") is None
    assert (box.work / "ajeno.cs").read_text() == "precious"


def test_a_link_put_over_an_own_file_is_not_followed_or_deleted(box):
    make(box.work / "ajena" / "keep.txt")
    write_new(box, box.work / "propio.txt")
    assert bash(box, "ln -sf ajena propio.txt") == "delete"                 # refused up front; the swap below is done by hand
    os.replace(box.work / "propio.txt", box.work / "old.txt")
    os.symlink(box.work / "ajena", box.work / "propio.txt")
    assert bash(box, "rm -rf propio.txt/") == "delete"
    assert bash(box, "rm -rf propio.txt") == "delete"
    assert bash(box, "rm propio.txt/keep.txt") == "delete"
    assert (box.work / "ajena" / "keep.txt").exists()


def test_a_link_to_a_foreign_folder_is_never_deleted_through(box):
    make(box.work / "dirf" / "x.txt")
    os.symlink(box.work / "dirf", box.work / "lnk")
    assert bash(box, "rm -rf lnk/") == "delete"
    assert bash(box, "rm -rf lnk/*") == "delete"
    assert bash(box, "rm lnk") == "delete"


def test_a_folder_with_a_foreign_file_moved_inside_stays_protected(box):
    make(box.work / "pre.txt", "precious")
    write_new(box, box.work / "od" / "a.txt")
    write_new(box, box.work / "od" / "b.txt")
    assert bash(box, "ls") is None                                          # the hook takes the identity of what exists
    os.replace(box.work / "pre.txt", box.work / "od" / "b.txt")
    assert bash(box, "rm -r od") == "delete"
    assert bash(box, "rm od/a.txt") is None


def test_deleting_strikes_the_entry_off_the_list(box):
    f = box.work / "gone.txt"
    write_new(box, f)
    assert bash(box, "rm gone.txt") is None
    f.unlink()
    make(f, "someone else's")                                               # same name, another file, after the delete
    assert bash(box, "rm gone.txt") == "delete"
    assert bash(box, "touch gone.txt") is None
    assert bash(box, "rm gone.txt") == "delete"


def test_recreating_after_delete_makes_it_own_again(box):
    f = box.work / "again.txt"
    write_new(box, f)
    assert bash(box, "rm again.txt") is None
    f.unlink()
    write_new(box, f)
    assert bash(box, "rm again.txt") is None


def test_editing_an_own_file_that_the_tool_replaces_keeps_it_own(box):
    f = box.work / "ed.txt"
    write_new(box, f)
    assert bash(box, "ls") is None                                          # identity taken
    assert call(box, "Edit", file_path=str(f), old_string="x", new_string="y") is None
    tmp = box.work / "ed.tmp.swap"
    tmp.write_text("y\n")
    os.replace(tmp, f)                                                      # the tool wrote it with an atomic rename
    assert bash(box, "rm ed.txt") is None


# ---- second round: a move or a link whose destination is on the list and whose source is not the session's own file

def test_one_line_mv_foreign_over_own_then_rm_is_blocked(box):
    sub = box.work / "sub"
    make(box.work / "mi_indicador.cs", "precious")
    make(box.work / "x.cs", "precious")
    write_new(box, sub / "tmp.txt")
    assert bash(box, "mv ../mi_indicador.cs tmp.txt; rm tmp.txt", cwd=sub) == "delete"
    assert bash(box, "mv ../x.cs tmp.txt && rm tmp.txt", cwd=sub) == "delete"
    assert bash(box, "mv ../x.cs tmp.txt", cwd=sub) == "delete"
    assert bash(box, "mv -f ../x.cs sub/tmp.txt") == "delete"
    assert bash(box, "mv -t . ../x.cs", cwd=sub) is None                    # the target folder itself is not on the list
    make(sub / "x.cs")
    make(box.work / "z.cs")
    assert bash(box, "mv -t sub z.cs") is None                              # foreign to a path that is not on the list


def test_powershell_move_item_foreign_over_own_is_blocked(box):
    make(box.work / "mi_indicador.cs", "precious")
    write_new(box, box.work / "tmp.txt")
    for cmd in ("Move-Item ../mi_indicador.cs tmp.txt -Force; Remove-Item tmp.txt",
                "Move-Item -Path mi_indicador.cs -Destination tmp.txt -Force",
                "mi mi_indicador.cs tmp.txt", "Move-Item -LiteralPath mi_indicador.cs -Destination:tmp.txt"):
        assert call(box, "PowerShell", command=cmd) == "delete", cmd
    assert call(box, "PowerShell", command="Rename-Item mi_indicador.cs -NewName tmp.txt") == "delete"


def test_ln_force_over_own_is_blocked(box):
    write_new(box, box.work / "propio.txt")
    assert bash(box, "ln -sf ajena propio.txt; rm -rf propio.txt/") == "delete"
    assert bash(box, "ln -sfn ajena propio.txt") == "delete"
    assert bash(box, "ln -s ajena nuevo") is None


def test_mv_own_to_own_and_foreign_to_new_stay_free(box):
    make(box.work / "ajeno.cs", "precious")
    write_new(box, box.work / "a.txt")
    write_new(box, box.work / "b.txt")
    assert bash(box, "mv a.txt b.txt") is None                              # own over own
    assert bash(box, "mv ajeno.cs nuevo.cs") is None                        # foreign to a new path
    assert call(box, "PowerShell", command="Move-Item ajeno.cs otro.cs") is None
    assert bash(box, "mv ajeno.cs b.txt") == "delete"
    assert bash(box, "mv ajeno.cs $d") == "delete"                          # unknown destination while the session has a list


def test_python_replace_over_own_is_blocked(box):
    make(box.work / "mi_indicador.cs", "precious")
    write_new(box, box.work / "tmp.txt")
    for code in ("import os; os.replace('mi_indicador.cs', 'tmp.txt')", "import os; os.rename('mi_indicador.cs','tmp.txt')",
                 "import shutil; shutil.move('mi_indicador.cs', 'tmp.txt')", "import os; os.replace(a, b)"):
        assert bash(box, f"python -c \"{code}\"") == "delete", code
    assert bash(box, "python -c \"import os; os.replace('mi_indicador.cs', 'otro.cs')\"") is None
    write_new(box, box.work / "t3.txt")
    assert bash(box, "python -c \"import os; os.replace('t3.txt', 'tmp.txt')\"") is None


# ---- same-line links: nothing that this line links is deleted through

def test_same_line_link_then_recursive_delete_is_blocked(box):
    make(box.work / "carpeta_ajena" / "keep.txt")
    sub = box.work / "sub"
    sub.mkdir()
    for cmd in ("ln -s ../carpeta_ajena x && rm -rf x/", "ln -s ../carpeta_ajena x; rm -rf x", "ln -s ../carpeta_ajena x && rm x/",
                "mkdir w && ln -s ../carpeta_ajena w/l && rm -rf w", "ln -sfn ../carpeta_ajena x && rm -r x/"):
        assert bash(box, cmd, cwd=sub) == "delete", cmd
    assert call(box, "PowerShell", cwd=sub, command="New-Item -ItemType SymbolicLink -Path x -Target ../carpeta_ajena; Remove-Item -Recurse -Force x") == "delete"
    assert call(box, "PowerShell", cwd=sub, command="New-Item -ItemType Junction -Path x -Target ../carpeta_ajena; Remove-Item -Force x/") == "delete"
    assert call(box, "PowerShell", cwd=sub, command="ni x -ItemType SymbolicLink -Value ../carpeta_ajena; Remove-Item -Recurse x") == "delete"
    assert call(box, "Bash", cwd=sub, command="cmd /c mklink /d x ..\\carpeta_ajena && rd /s /q x") == "delete"
    assert (box.work / "carpeta_ajena" / "keep.txt").exists()


def test_same_line_create_and_delete_without_links_stays_free(box):
    assert bash(box, "echo x > t.tmp && rm t.tmp") == "delete"
    assert bash(box, "mkdir w2 && echo x > w2/i.txt && rm -rf w2") == "delete"
