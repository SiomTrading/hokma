"""PreToolUse hook: keep the hooks themselves out of the agent's reach.

Hokma has four locks (`hardblock.py`: real-money orders, secrets, deleting, writing outside the working folder).
This hook protects what keeps them on: the files of the hooks and their registration in the settings. It blocks
Edit/MultiEdit/Write/NotebookEdit on the `--protect` paths. A protected settings file may still be edited while the
result keeps the registration of both hooks (`registration_intact`), so permission and other settings can change.

Deterministic and fail-closed: any error, unreadable input or missing path for a file-editing tool exits with code 2
(blocking).

The path is resolved the way the tool does it: an absolute path as given, a relative one against the `cwd` that comes
in the hook payload. Windows device paths (`\\\\?\\`, `\\\\.\\`) and UNC paths are blocked outright (they bypass normal
path rules). The final path is checked lexically and after symlink resolution (realpath). On Windows and macOS
(case-insensitive file systems) names are compared case-folded, and every existing ancestor of the target is compared
with the protected roots by identity (st_dev, st_ino), so letter case cannot evade the check. An existing target with
more than one hard link is compared by identity with the protected files, because it may share its inode with one.

Run it by absolute path with an isolated interpreter (`python -I /abs/guard.py --protect ...`) from a protected
directory, never as `-m hokma.kit.guard`: it must not be importable from the working folder or from PYTHON*
variables. It only uses the standard library (and `hokma.i18n`).
"""
import json
import ntpath
import os
import re
import shlex
import sys

from hokma.i18n import t

FILE_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
BLOCK = 2
DEVICE_PREFIXES = ("\\\\?\\", "\\\\.\\", "//?/", "//./")
CASE_INSENSITIVE = sys.platform in ("win32", "darwin")


def _fold(s: str) -> str:
    s = os.path.normcase(s)
    return s.casefold() if CASE_INSENSITIVE else s


def is_device_or_unc(path: str) -> bool:
    """Device paths and anything ntpath sees as a drive that is not a plain `X:` letter (UNC)."""
    if path.startswith(DEVICE_PREFIXES):
        return True
    drive = ntpath.splitdrive(path)[0]
    return bool(drive) and not re.fullmatch(r"[A-Za-z]:", drive)


def _inside(path: str, roots: list[str]) -> bool:
    for root in roots:
        try:
            if os.path.commonpath([path, root]) == root:
                return True
        except ValueError:
            # commonpath cannot compare. Two different drive letters (C: vs D:) can never nest, so that root
            # does not contain the path. Anything else (mixed absolute and relative, odd types) stays protected.
            if _different_drives(path, root):
                continue
            return True
    return False


def _different_drives(a: str, b: str) -> bool:
    """True only when both paths are str, both name a drive and the drives differ (case-folded). Fail closed otherwise."""
    if not (isinstance(a, str) and isinstance(b, str)):
        return False
    drive_a, drive_b = ntpath.splitdrive(ntpath.normcase(a))[0], ntpath.splitdrive(ntpath.normcase(b))[0]
    return bool(drive_a) and bool(drive_b) and drive_a != drive_b


def _identity(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _existing_ancestors(path: str):
    """The path and its parents that exist, deepest first."""
    cur = path
    while True:
        if os.path.exists(cur):
            yield cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return
        cur = parent


def _same_identity_as_root(path: str, root_ids: list[tuple[int, int]]) -> bool:
    return any(_identity(a) in root_ids for a in _existing_ancestors(path))


def _linked_to_protected(path: str, protect: list[str]) -> bool:
    """An existing file with several hard links that shares its inode with a file under a protected path."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    if not os.path.isfile(path) or st.st_nlink < 2:
        return False
    for root in protect:
        walk = [(root, [], [""])] if os.path.isfile(root) else os.walk(os.path.realpath(root))
        for folder, _dirs, names in walk:
            for name in names:
                if _identity(os.path.join(folder, name)) == (st.st_dev, st.st_ino):
                    return True
    return False


def under_any(path: str, roots: list[str]) -> bool:
    """True if path is inside any root. THE comparator of Hokma (the guard hook and the extension validators
    use this one): realpath on both sides, case-folded names where the file system is case-insensitive, and
    identity (st_dev, st_ino) of every existing ancestor, so letter case and aliases cannot evade it."""
    real_roots = [os.path.realpath(r) for r in roots]
    folded = [_fold(r) for r in real_roots]
    ids = [i for i in (_identity(r) for r in real_roots) if i is not None]
    return any(_inside(_fold(c), folded) or _same_identity_as_root(c, ids)
               for c in {os.path.realpath(path), os.path.normpath(path)})



def is_settings_file(path: str) -> bool:
    name = os.path.basename(path).lower()
    return name.startswith("settings") and name.endswith(".json")


def _script_of(command: str, name: str) -> str | None:
    """The script of a hook command, only when the command has exactly the shape Hokma writes:
    `cd <folder> && <python> -I <script> [--flag value ...] || exit 2`, and the script is called `name`."""
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if len(words) < 8 or words[0] != "cd" or words[2] != "&&" or words[4] != "-I" or words[-3:] != ["||", "exit", "2"]:
        return None
    flags = words[6:-3]
    if len(flags) % 2 or not all(flags[k].startswith("--") and not flags[k + 1].startswith("--")
                                  for k in range(0, len(flags), 2)):
        return None
    return words[5] if os.path.basename(words[5].replace("\\", "/")) == name else None


# Settings keys that run a command outside the PreToolUse hooks, and environment names that redirect what runs.
COMMAND_KEYS = {"statusline", "apikeyhelper", "awsauthrefresh", "awscredentialexport", "otelheadershelper",
                "filesuggestion", "sessionstart", "shell"}
DANGEROUS_ENV = ("PATH", "LD_", "DYLD_", "PYTHON", "NODE_OPTIONS", "NODE_PATH", "BASH_ENV", "ENV", "SHELL",
                 "CLAUDE_CODE_SHELL", "CLAUDE_CODE_GIT_BASH_PATH", "BUN_OPTIONS", "BASH_FUNC_", "GIT_", "NPM_CONFIG_",
                 "PERL", "RUBY", "ZDOTDIR", "PROMPT_COMMAND", "UV_", "PIP_")


def _runs_outside_the_hooks(data: dict) -> bool:
    """True when the settings carry a key that executes a command other than the two PreToolUse hooks."""
    if any(str(k).lower() in COMMAND_KEYS for k in data):
        return True
    env = data.get("env")
    if isinstance(env, dict) and any(str(k).upper().startswith(DANGEROUS_ENV) for k in env):
        return True
    hooks = data.get("hooks")
    return isinstance(hooks, dict) and any(k != "PreToolUse" and v for k, v in hooks.items())


def registration_intact(text: str, protect: list[str] | None = None) -> bool:
    """The settings text still registers both hooks: `hardblock.py` on every tool and `guard.py` on the file tools, each
    as a command that runs `python -I <script>` and ends the call with `|| exit 2`, and the hooks are not switched off.
    With `protect`, each script has to be one of the protected files (not a copy of the name somewhere else)."""
    try:
        data = json.loads(text)
        if not isinstance(data, dict) or data.get("disableAllHooks") or _runs_outside_the_hooks(data):
            return False
        found = set()
        extra = False
        for group in data["hooks"]["PreToolUse"]:
            matcher = group.get("matcher", "")
            for hook in group.get("hooks", []):
                command = hook.get("command", "")
                if hook.get("type") != "command":
                    extra = True
                    continue
                before = len(found)
                for key, name, wanted in (("hardblock", "hardblock.py", matcher == ".*"),
                                          ("guard", "guard.py", "Edit" in matcher and "Write" in matcher)):
                    script = _script_of(command, name)
                    if script and wanted and (protect is None or under_any(script, protect)):
                        found.add(key)
                if len(found) == before and not any(
                        _script_of(command, n) and (protect is None or under_any(_script_of(command, n), protect))
                        for n in ("hardblock.py", "guard.py")):
                    extra = True            # a hook that is not one of ours
        return found == {"hardblock", "guard"} and not extra
    except (KeyError, TypeError, AttributeError, ValueError):
        return False


def _edit_result(tool: str, tool_input: dict, current: str) -> str | None:
    """The text of the file after an Edit or MultiEdit, or None when the edit cannot be applied here."""
    edits = tool_input.get("edits") if tool == "MultiEdit" else [tool_input]
    text = current
    for edit in edits or []:
        old, new = edit.get("old_string") if isinstance(edit, dict) else None, edit.get("new_string") if isinstance(edit, dict) else None
        if not isinstance(old, str) or not isinstance(new, str) or old == "":
            return None
        text = text.replace(old, new) if edit.get("replace_all") else text.replace(old, new, 1)
    return text


def _settings_edit_ok(tool: str, tool_input: dict, path: str, protect: list[str]) -> bool:
    """An edit of a protected settings file is allowed when what it leaves behind still registers the hooks."""
    if tool == "Write":
        content = tool_input.get("content")
        return isinstance(content, str) and registration_intact(content, protect)
    if tool in ("Edit", "MultiEdit"):
        try:
            with open(path, "rb") as f:
                current = f.read(8 * 1024 * 1024).decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        result = _edit_result(tool, tool_input, current)
        return result is not None and registration_intact(result, protect)
    return False


def decide(payload: dict, protect: list[str]) -> tuple[int, str]:
    tool = payload.get("tool_name", "")
    if tool not in FILE_TOOLS:
        return 0, ""
    tool_input = payload.get("tool_input") or {}
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not target or not isinstance(target, str):
        return BLOCK, t("guard.no_path", tool=tool)
    if is_device_or_unc(target):
        return BLOCK, t("guard.device_path", path=repr(target))
    if os.path.isabs(target):
        joined = target
    else:
        cwd = payload.get("cwd")
        if not isinstance(cwd, str) or not os.path.isabs(cwd):
            return BLOCK, t("guard.relative_no_cwd")
        if is_device_or_unc(cwd):
            return BLOCK, t("guard.device_cwd")
        joined = os.path.join(cwd, target)
    lexical = os.path.normpath(joined)

    for cand in {os.path.realpath(lexical),   # what the tool targets
                 os.path.realpath(joined),    # what the OS follows (symlink then ..)
                 lexical}:
        if under_any(cand, protect) or _linked_to_protected(cand, protect):
            if is_settings_file(cand) and under_any(cand, [r for r in protect if is_settings_file(r)]):
                if _settings_edit_ok(tool, tool_input, cand, protect):
                    continue
                return BLOCK, t("guard.registration", path=cand)
            return BLOCK, t("guard.protected", path=cand)
    return 0, ""


def main(argv: list[str] | None = None) -> int:
    try:
        args = sys.argv[1:] if argv is None else argv
        protect = [args[i + 1] for i, a in enumerate(args) if a == "--protect" and i + 1 < len(args)]
        if not protect:
            raise ValueError("no --protect paths")
        code, message = decide(json.loads(sys.stdin.read()), protect)
        if message:
            print(message, file=sys.stderr)
        return code
    except BaseException as exc:  # fail closed on anything unexpected
        print(t("guard.error", error=type(exc).__name__), file=sys.stderr)
        return BLOCK


if __name__ == "__main__":
    raise SystemExit(main())
