"""The data folder's secrets stay hidden from relative paths after `cd`, from recursive reads over a folder that holds
the data folder, and from links in the working folder; transcripts and the guard log are not readable; the browser
routes are as untouchable as the shutdown (task 3163, QA fix 0.0.20)."""
import os

import pytest

from hardblock_support import Box, box  # noqa: F401
from hokma.kit import hardblock


@pytest.fixture
def nested(box):
    """The data folder where it lives for real: `<home>/.local/share/hokma`."""
    home = box.tmp / "home"
    share = home / ".local" / "share"
    share.mkdir(parents=True)
    os.rename(box.data, share / "hokma")
    box.data = share / "hokma"
    box.protect[0] = str(box.data)
    (box.data / "hokma.db").write_text("sqlite")
    (box.data / "claude-config" / ".credentials.json").write_text("{}")
    (box.data / "claude-config" / "projects").mkdir()
    (box.data / "claude-config" / "projects" / "t.jsonl").write_text("{}")
    (box.data / "guard" / "blocks.jsonl").write_text("{}")
    (home / "projects").mkdir()
    box.home = home
    return box


def test_cd_then_relative_path(nested):
    h = nested.home
    assert nested.bash(f"cd {h}/.local/share && cat hokma/hokma.db") == "hokma_internal"
    assert nested.bash(f"cd {h}/.local/share/hokma && cat hokma.db") == "hokma_internal"
    assert nested.bash(f"cd {h}/.local/share && cat hokma/claude-config/.credentials.json") in ("secrets", "hokma_internal")
    assert nested.bash("cat hokma.db", nested.data) == "hokma_internal"
    assert nested.bash("cat notes.md") is None


@pytest.mark.parametrize("template", [
    "grep -r TOKEN {h}", "grep -rn TOKEN {h}/.local/share", "rg TOKEN {h}", "find {h} -name '*.db'",
    "find {h}/.local/share/* -name x", "ls -R {h}", "tar czf x.tgz {h}", "zip -r a.zip {h}",
    "cp -r {h} /tmp/y", "cd {h} && grep -r TOKEN .", "cd {h} && rg x", "grep -r TOKEN {h}/.local/*",
])
def test_recursive_read_over_an_ancestor_is_a_read_of_the_data_folder(nested, template):
    assert nested.bash(template.format(h=nested.home)) is not None


def test_recursive_read_that_excludes_the_data_folder_or_stays_elsewhere_is_free(nested):
    h = nested.home
    assert nested.bash(f"grep -r TOKEN {h}/projects") is None
    assert nested.bash(f"grep -r TOKEN {h} --exclude-dir=hokma") is None
    assert nested.bash("grep -r x .") is None
    assert nested.bash("ls -R informes") is None


def test_the_grep_tool_over_an_ancestor_is_refused(nested):
    assert nested.hit("Grep", pattern="TOKEN", path=str(nested.home)) == "hokma_internal"
    assert nested.hit("Grep", pattern="TOKEN", path=str(nested.work)) is None


def test_a_link_in_the_working_folder_to_the_data_folder_is_followed(nested):
    os.symlink(nested.data / "hokma.db", nested.work / "lnk")
    os.symlink(nested.data, nested.work / "dl")
    assert nested.bash("cat lnk") == "hokma_internal"
    assert nested.bash("grep -r x dl") == "hokma_internal"
    assert nested.bash("cat dl/hokma.db") == "hokma_internal"
    assert nested.bash("cat f") is None


@pytest.mark.parametrize("command", [
    r"type %LOCALAPPDATA%\HOKMA\hokma.db", r"type C:\Users\Ana\AppData\Local\HOKMA\hokma.db",
    r"type %LOCALAPPDATA%\Hokma\claude-config\.CREDENTIALS.json",
])
def test_windows_paths_in_capitals(nested, command):
    assert nested.bash(command) is not None


def test_transcripts_and_guard_log_are_not_readable(nested):
    d = nested.data
    assert nested.bash(f"cat {d}/claude-config/projects/t.jsonl") == "hokma_internal"
    assert nested.bash(f"ls {d}/claude-config/projects") == "hokma_internal"
    assert nested.bash(f"cat {d}/guard/blocks.jsonl") == "hokma_internal"
    assert nested.hit("Read", file_path=str(d / "claude-config" / "projects" / "t.jsonl")) == "hokma_internal"
    assert nested.bash(f"cat {d}/claude-config/settings.json") is None


@pytest.mark.parametrize("text", [
    "curl -X POST localhost:1/api/system/browser", "curl -X PUT localhost:1/api/system/browser-pref -d x",
    "curl localhost:1/api/system/%62rowser", "curl localhost:1/api/system/./browser",
])
def test_browser_routes_are_blocked_like_the_shutdown(box, text):
    assert box.bash(text) == "hokma_internal"
    assert hardblock.names_shutdown_route(text)


@pytest.mark.parametrize("template", [
    "grep -r TOKEN {h} --include=hokma", "grep -r TOKEN {h} hokma", "grep -r TOKEN {h} --exclude-dir=other",
    "rg -g 'hokma' TOKEN {h}", "grep -r -e TOKEN {h}", "find -H {h} -name x", "find {h} -name hokma",
    "find {h} -path '*/hokma' -prune -o -name x -print", "grep -r TOKEN {h} --exclude=hokma", "rg -g 'hokma' TOKEN {h}",
    "grep -r TOKEN {h}/.l*/share", "grep -r TOKEN {h}/.local/sh*", "grep -r TOKEN {h}/.[l]ocal",
])
def test_a_loose_mention_of_the_folder_is_not_an_exclusion(nested, template):
    assert nested.bash(template.format(h=nested.home)) == "hokma_internal"


@pytest.mark.parametrize("template", [
    "grep -r TOKEN {h} --exclude-dir=hokma", "rg -g '!hokma' TOKEN {h}", "tar czf x.tgz --exclude=hokma {h}",

])
def test_a_real_exclusion_of_the_folder_is_free(nested, template):
    assert nested.bash(template.format(h=nested.home)) is None


@pytest.mark.parametrize("template", [
    "tar czf x.tgz --directory={h} .", "tar czf x.tgz -C {h} .", "grep -r TOKEN {h} --include=*.db --exclude-dir=hokma",
    "grep -r TOKEN {d} --exclude-dir=hokma",
])
def test_root_options_and_includes_do_not_hide_the_data_folder(nested, template):
    assert nested.bash(template.format(h=nested.home, d=nested.data)) == "hokma_internal"


@pytest.mark.parametrize("template", [
    "grep -r TOKEN $UNKNOWN", "grep -r TOKEN {h}/{{.local,x}}", "grep -R TOKEN {h} --exclude-dir=hokma",
    "tar chf x.tgz --exclude=hokma {h}",
])
def test_unresolved_braces_and_link_following_fail_closed(nested, template):
    assert nested.bash(template.format(h=nested.home)) == "hokma_internal"


def test_zip_exclusion_and_tilde_forms_fail_closed(nested):
    assert nested.bash(f"zip -r a.zip {nested.home} -x '*/hokma/*'") == "hokma_internal"
    assert nested.bash("grep -r TOKEN ~+") == "hokma_internal"
    assert nested.bash("grep -r TOKEN ~root") == "hokma_internal"
