"""PreToolUse hook: block Edit/Write/NotebookEdit on Hokma's protected paths.

Deterministic and fail-closed: any error, unreadable input or missing path for a file-editing tool
exits with code 2 (blocking).

The path is resolved the way the tool does it: an absolute path as given, a relative one against
the `cwd` that comes in the hook payload. No `~` or `$VAR` expansion happens here, because the tool
does not expand them either; a target that contains `$`, `%` or `~` is blocked outright, and so is
one that still has `..` segments after normalising. Windows device paths (`\\\\?\\`, `\\\\.\\`) and UNC
paths are blocked outright (they bypass normal path rules). The final path is checked lexically and
after symlink resolution (realpath). On Windows and macOS (case-insensitive file systems) names are
compared case-folded, and every existing ancestor of the target is compared with the protected roots
by identity (st_dev, st_ino), so letter case cannot evade the check. An existing target with more
than one hard link is blocked, because it may share its inode with a protected file.

Run it by absolute path with an isolated interpreter (`python -I /abs/guard.py --protect ...`) from
a protected directory, never as `-m hokma.kit.guard`: it must not be importable from the working
folder or from PYTHON* variables. It only uses the standard library.
"""
import json
import ntpath
import os
import re
import sys

from hokma.i18n import t

FILE_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
BLOCK = 2
SUSPICIOUS = ("$", "%", "~")
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


def _is_multi_linked_file(path: str) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    return os.path.isfile(path) and st.st_nlink > 1

def under_any(path: str, roots: list[str]) -> bool:
    """True if path is inside any root. THE comparator of Hokma (the guard hook and the extension validators
    use this one): realpath on both sides, case-folded names where the file system is case-insensitive, and
    identity (st_dev, st_ino) of every existing ancestor, so letter case and aliases cannot evade it."""
    real_roots = [os.path.realpath(r) for r in roots]
    folded = [_fold(r) for r in real_roots]
    ids = [i for i in (_identity(r) for r in real_roots) if i is not None]
    return any(_inside(_fold(c), folded) or _same_identity_as_root(c, ids)
               for c in {os.path.realpath(path), os.path.normpath(path)})



def decide(payload: dict, protect: list[str]) -> tuple[int, str]:
    tool = payload.get("tool_name", "")
    if tool not in FILE_TOOLS:
        return 0, ""
    tool_input = payload.get("tool_input") or {}
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not target or not isinstance(target, str):
        return BLOCK, t("guard.no_path", tool=tool)
    if any(ch in target for ch in SUSPICIOUS):
        return BLOCK, t("guard.suspicious", path=repr(target))
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
    if ".." in lexical.replace("\\", "/").split("/"):
        return BLOCK, t("guard.dotdot", path=repr(target))

    for cand in {os.path.realpath(lexical),   # what the tool targets
                 os.path.realpath(joined),    # what the OS follows (symlink then ..)
                 lexical}:
        if under_any(cand, protect):
            return BLOCK, t("guard.protected", path=cand)
        if _is_multi_linked_file(cand):
            return BLOCK, t("guard.hardlink", path=cand)
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
