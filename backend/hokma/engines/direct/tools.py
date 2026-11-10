"""Native tools of the direct engine. Hokma runs them, so Hokma asks and protects first (the broker sees each call).

Names and descriptions are in English: the model reads them.
"""
from __future__ import annotations

import fnmatch
import ipaddress
import os
import socket
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from hokma.core.proc import spawn_kwargs
from hokma.engines.envfilter import filtered_env

MAX_OUTPUT = 30_000
FETCH_LIMIT = 200 * 1024
READ_LIMIT = 200_000
DEFAULT_TIMEOUT = 120
READ_ONLY = {"read_file", "list_dir", "glob", "grep"}

SCHEMAS: dict[str, dict[str, Any]] = {
    "read_file": {"description": "Read a text file. Returns numbered lines.",
                  "properties": {"path": {"type": "string"}, "offset": {"type": "integer"},
                                 "limit": {"type": "integer"}}, "required": ["path"]},
    "write_file": {"description": "Create a file or replace it entirely.",
                   "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                   "required": ["path", "content"]},
    "edit_file": {"description": "Replace an exact piece of text in a file.",
                  "properties": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"},
                                 "replace_all": {"type": "boolean"}}, "required": ["path", "old", "new"]},
    "list_dir": {"description": "List the entries of a folder.", "properties": {"path": {"type": "string"}},
                 "required": ["path"]},
    "glob": {"description": "Find files by pattern, for example **/*.py.",
             "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]},
    "grep": {"description": "Search file contents with a regular expression.",
             "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "string"}},
             "required": ["pattern"]},
    "run_command": {"description": "Run a shell command (PowerShell on Windows, bash elsewhere) in the project "
                                   "folder. Output is cut at 30000 characters; default timeout 120 seconds.",
                    "properties": {"command": {"type": "string"}, "timeout_s": {"type": "integer"}}, "required": ["command"]},
    "web_fetch": {"description": "GET a URL and return its text (first 200 KB).",
                  "properties": {"url": {"type": "string"}}, "required": ["url"]},
}


def definitions(allowed: set[str] | None = None) -> list[dict[str, Any]]:
    """Tool definitions in a neutral shape: name, description, JSON Schema."""
    out = []
    for name, spec in SCHEMAS.items():
        if allowed is not None and name not in allowed:
            continue
        out.append({"name": name, "description": spec["description"],
                    "schema": {"type": "object", "properties": spec["properties"], "required": spec["required"],
                               "additionalProperties": False}})
    return out


def _path(cwd: str, value: Any) -> Path:
    p = Path(str(value or "."))
    return p if p.is_absolute() else Path(cwd) / p


def _cut(text: str, limit: int = MAX_OUTPUT) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n[cut: {len(text) - limit} more characters]"


def read_file(cwd: str, a: dict[str, Any]) -> str:
    p = _path(cwd, a.get("path"))
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(int(a.get("offset") or 0), 0)
    end = start + int(a.get("limit") or len(lines))
    body = "\n".join(f"{i + 1}\t{line}" for i, line in enumerate(lines[start:end], start))
    return _cut(body, READ_LIMIT)


def write_file(cwd: str, a: dict[str, Any]) -> str:
    p = _path(cwd, a.get("path"))
    p.parent.mkdir(parents=True, exist_ok=True)
    content = str(a.get("content", ""))
    p.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} characters to {p}"


def edit_file(cwd: str, a: dict[str, Any]) -> str:
    p = _path(cwd, a.get("path"))
    text = p.read_text(encoding="utf-8")
    old, new = str(a.get("old", "")), str(a.get("new", ""))
    count = text.count(old) if old else 0
    if count == 0:
        raise ValueError("the text to replace was not found")
    if count > 1 and not a.get("replace_all"):
        raise ValueError(f"the text appears {count} times; add context or set replace_all")
    p.write_text(text.replace(old, new) if a.get("replace_all") else text.replace(old, new, 1), encoding="utf-8")
    return f"edited {p}"


def list_dir(cwd: str, a: dict[str, Any]) -> str:
    p = _path(cwd, a.get("path"))
    return "\n".join(sorted(e.name + ("/" if e.is_dir() else "") for e in p.iterdir())) or "(empty)"


PROTECTED: list[str] = []          # set by the engine: folders no result may come from


def _inside(path: str, roots: list[str]) -> bool:
    real = os.path.realpath(path)
    return any(real == r or real.startswith(r.rstrip(os.sep) + os.sep) for r in (os.path.realpath(x) for x in roots))


def glob(cwd: str, a: dict[str, Any]) -> str:
    pattern = str(a.get("pattern", ""))
    if ".." in pattern.replace("\\", "/").split("/") or os.path.isabs(pattern) or pattern.startswith(("/", "\\")) \
            or re.match(r"^[A-Za-z]:", pattern):
        raise ValueError("the pattern must be relative and without ..")
    root = _path(cwd, a.get("path"))
    hits = []
    for x in root.glob(pattern):
        if x.is_file() and _inside(str(x), [str(root)]) and not _inside(str(x), PROTECTED):
            hits.append(str(x.relative_to(root)))
    return _cut("\n".join(sorted(hits)[:500]) or "(no matches)")


def grep(cwd: str, a: dict[str, Any]) -> str:
    root = _path(cwd, a.get("path"))
    rx = re.compile(str(a.get("pattern")))
    name_filter = a.get("glob")
    files = [root] if root.is_file() else [f for f in root.rglob("*") if f.is_file() and ".git" not in f.parts]
    out: list[str] = []
    for f in files:
        if name_filter and not fnmatch.fnmatch(f.name, str(name_filter)):
            continue
        try:
            for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if rx.search(line):
                    out.append(f"{f}:{i}:{line[:300]}")
                    if len(out) >= 300:
                        return _cut("\n".join(out))
        except (OSError, UnicodeDecodeError):
            continue
    return _cut("\n".join(out) or "(no matches)")


def run_command(cwd: str, a: dict[str, Any], env: dict[str, str] | None = None) -> str:
    where = cwd                                                  # always the folder the broker checked
    timeout = min(max(int(a.get("timeout_s") or DEFAULT_TIMEOUT), 1), 600)
    if sys.platform == "win32":
        argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", str(a.get("command", ""))]
    else:
        argv = ["bash", "-c", str(a.get("command", ""))]
    try:
        done = subprocess.run(argv, cwd=where, env=env if env is not None else filtered_env(), capture_output=True,
                              timeout=timeout, **spawn_kwargs())
    except subprocess.TimeoutExpired:
        raise ValueError(f"the command did not finish in {timeout} seconds") from None
    text = (done.stdout + done.stderr).decode("utf-8", "replace")
    return _cut(f"{text}\n[exit code {done.returncode}]")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):          # noqa: D401
        raise ValueError(f"the server redirected to {newurl[:120]}; fetch that address directly")


def _public_host(host: str) -> None:
    """Refuse loopback, link-local, private, multicast and reserved addresses, for every address the name resolves to."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise ValueError(f"cannot resolve {host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip.is_multicast:
            raise ValueError("addresses on this computer or a private network are not fetched")


def web_fetch(cwd: str, a: dict[str, Any]) -> str:
    url = str(a.get("url", ""))
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ValueError("only http and https URLs")
    _public_host(parsed.hostname)
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": "Hokma"})
    with opener.open(req, timeout=30) as resp:                                              # noqa: S310
        return resp.read(FETCH_LIMIT).decode("utf-8", "replace")


RUNNERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "read_file": read_file, "write_file": write_file, "edit_file": edit_file, "list_dir": list_dir, "glob": glob,
    "grep": grep, "run_command": run_command, "web_fetch": web_fetch}


def run(name: str, cwd: str, args: dict[str, Any], env: dict[str, str] | None = None) -> tuple[str, bool]:
    """(text, is_error). A tool failure is a result the model sees, not a crash."""
    runner = RUNNERS.get(name)
    if runner is None:
        return f"unknown tool {name}", True
    try:
        if name == "run_command":
            return run_command(cwd, args, env), False
        return runner(cwd, args), False
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}", True


_ = os
