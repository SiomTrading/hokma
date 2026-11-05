"""Path resolution of the PreToolUse guard: resolve like the tool, block anything ambiguous."""
import pytest

from hokma.kit import guard
from hokma.kit.settings import guard_command


@pytest.fixture
def dirs(tmp_path):
    data, work = tmp_path / "data", tmp_path / "work"
    (data / "claude-config").mkdir(parents=True)
    (data / "sub").mkdir()
    work.mkdir()
    return data, work


def decide(tool, target, protect, cwd):
    return guard.decide({"tool_name": tool, "tool_input": {"file_path": target}, "cwd": str(cwd)},
                        [str(p) for p in protect])[0]


@pytest.mark.parametrize("target", ["$HOME/x", "${HOME}/x", "%LOCALAPPDATA%\\Hokma\\x", "~/x", "~", "a/$X/b",
                                    "notes~1.md"])
def test_blocks_dollar_percent_tilde_even_inside_workdir(dirs, target):
    data, work = dirs
    assert decide("Write", target, [data], work) == 2
    assert decide("Edit", str(work / target), [data], work) == 2


def test_no_expansion_so_a_literal_tilde_dir_is_not_confused_with_home(dirs):
    data, work = dirs
    # the tool does not expand `~`; the guard must not either: it blocks instead of guessing
    assert decide("Write", "~/data/x", [data], work) == 2


def test_leftover_dotdot_after_normalising_is_blocked(dirs):
    data, work = dirs
    # relative cwd: normpath cannot remove the leading `..`
    payload = {"tool_name": "Write", "tool_input": {"file_path": "../../x"}, "cwd": "relative/cwd"}
    assert guard.decide(payload, [str(data)])[0] == 2


def test_relative_without_absolute_cwd_is_blocked(dirs):
    data, _ = dirs
    assert guard.decide({"tool_name": "Write", "tool_input": {"file_path": "x"}}, [str(data)])[0] == 2
    assert guard.decide({"tool_name": "Write", "tool_input": {"file_path": "x"}, "cwd": "rel"}, [str(data)])[0] == 2


def test_relative_resolves_against_payload_cwd(dirs):
    data, work = dirs
    assert decide("Write", "../data/claude-config/settings.json", [data], work) == 2
    assert decide("Write", "notes.md", [data], work) == 0
    assert decide("Write", "notes.md", [data], data / "sub") == 2   # cwd inside the protected tree


def test_symlink_then_dotdot_cannot_disagree_with_the_tool(dirs):
    data, work = dirs
    (work / "link").symlink_to(data / "sub")
    # lexically work/claude-config/x (allowed); physically data/claude-config/x (protected)
    assert decide("Write", str(work / "link" / ".." / "claude-config" / "x"), [data], work) == 2
    assert decide("Write", "link/../claude-config/x", [data], work) == 2


def test_symlinked_file_inside_workdir_pointing_to_protected_is_blocked(dirs):
    data, work = dirs
    (work / "innocent.json").symlink_to(data / "claude-config" / "settings.json")
    assert decide("Edit", "innocent.json", [data], work) == 2


def test_hook_command_uses_isolated_absolute_guard_from_protected_dir():
    cmd = guard_command()
    assert cmd.startswith("cd ") and " -I " in cmd and "-m hokma" not in cmd
    assert str(guard.__file__) in cmd and cmd.endswith("|| exit 2")
