"""Extension requests: the agent may ASK for a plugin or an MCP server; only the student applies it.

Flow
  agent:   hokma ext request plugin <name@marketplace>      ->  <requests>/<id>.json   (pending)
           hokma ext request mcp <name> '<json config>'
  student: hokma ext requests                                ->  lists what is pending, validated
           hokma ext approve <id>   (interactive terminal)   ->  shows EXACTLY what will run, asks, applies
           hokma ext reject <id>
  UI later: a permission card calls approve_request() with its own confirmation.

Where things live
  The inbox is the folder `<data>-requests` (a sibling of the data dir). It has to be writable by the agent's
  sandbox, which the protected data dir is not. Files there are UNTRUSTED proposals: whatever the agent writes
  or edits is re-read and re-validated whenever it is used, and a proposal changes nothing by itself.
  The APPLIED state lives only in the protected data dir (<data>/extensions.json), which the agent cannot write;
  approving needs a real terminal (stdin and stdout are TTYs), which the agent's non-interactive shell does not
  have, plus the student typing the request id.

The approval is the control
  An approved MCP server is a program that runs outside the Bash sandbox with the student's rights. Hokma does
  not guess what is dangerous. It guarantees that the student SEES the command, every argument and every
  environment variable exactly as they will run (one per line, escaped so nothing in them can rewrite the
  terminal), and, when the program could be changed by the agent (relative paths, paths inside folders the agent
  writes, interpreters that load code), it says so and asks for an extra accept word (see mcp_config.code_risks).
"""
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from hokma.core.config import hokma_data_dir, requests_dir
from hokma.i18n import t
from hokma.kit import extensions
from hokma.kit.mcp_config import SERVER_NAME, code_risks, validate_mcp

ID_RE = re.compile(r"^[0-9a-f]{8}$")
MAX_REQUEST_BYTES = 16 * 1024
MAX_PENDING = 50


class RequestError(ValueError):
    pass


def safe(value: object) -> str:
    """Quoted and escaped for a terminal: control characters, bidi marks and any non-ASCII become \\uXXXX."""
    return json.dumps(str(value), ensure_ascii=True)


@dataclass
class Request:
    id: str
    kind: str            # "plugin" | "mcp"
    name: str
    config: dict | None
    requested_at: str    # taken from the file's mtime, never from its content

    @property
    def risks(self) -> list[str]:
        return code_risks(self.config) if self.kind == "mcp" and self.config else []

    def summary(self) -> str:
        if self.kind == "plugin":
            return t("req.summary.plugin", name=safe(self.name))
        cfg = self.config or {}
        lines = [t("req.summary.mcp", name=safe(self.name)), t("req.summary.runs_outside")]
        if cfg.get("type", "stdio") != "stdio":
            lines.append(_line(t("req.label.type"), safe(cfg.get("type"))))
            lines.append(_line("URL", safe(cfg.get("url"))))
        else:
            lines.append(_line(t("req.label.command"), safe(cfg["command"])))
            for i, arg in enumerate(cfg.get("args", []), 1):
                lines.append(_line(t("req.label.argument", n=i), safe(arg)))
            for key, value in cfg.get("env", {}).items():
                lines.append(_line(t("req.label.variable"), f"{key} = {safe(value)}"))
            lines.append(_line(t("req.label.folder"), t("req.summary.folder")))
        if self.risks:
            lines.append("")
            lines.append(t("req.risk_warning"))
            lines += [f"  - {r}" for r in self.risks]
        return "\n".join(lines)


def _line(label: str, value: str) -> str:
    return f"  {label.ljust(10)}: {value}"


def _validate(kind: str, name: str, config: object) -> dict | None:
    if kind == "plugin":
        if not extensions.PLUGIN_ID.match(name):
            raise RequestError(t("ext.plugin_form"))
        return None
    if kind == "mcp":
        if not SERVER_NAME.match(name):
            raise RequestError(t("ext.server_name"))
        try:
            return validate_mcp(config)
        except ValueError as exc:
            raise RequestError(str(exc)) from exc
    raise RequestError(t("req.unknown_kind"))


def _stamp(path: Path) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))
    except OSError:
        return ""


def _create(kind: str, name: str, config: dict | None) -> Request:
    d = requests_dir()
    d.mkdir(parents=True, exist_ok=True)
    if len(list(d.glob("*.json"))) >= MAX_PENDING:
        raise RequestError(t("req.too_many"))
    for r in list_pending():
        if r.kind == kind and r.name == name and r.config == config:
            return r                                    # same request again: keep the one already pending
    rid = secrets.token_hex(4)
    path, tmp = d / f"{rid}.json", d / f".{rid}.tmp"
    tmp.write_text(json.dumps({"id": rid, "kind": kind, "name": name, "config": config}), encoding="utf-8")
    os.replace(tmp, path)
    return Request(rid, kind, name, config, _stamp(path))


def request_plugin(name: str) -> Request:
    _validate("plugin", name, None)
    return _create("plugin", name, None)


def request_mcp(name: str, config: object) -> Request:
    clean = _validate("mcp", name, config)
    return _create("mcp", name, clean)


def _read(path: Path) -> Request | None:
    """Parse one inbox file; anything malformed or hostile is ignored. Only id, kind, name and config are
    used; any other field (for example a forged timestamp) is ignored."""
    try:
        if not ID_RE.match(path.stem) or path.stat().st_size > MAX_REQUEST_BYTES or path.is_symlink():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("id") != path.stem:
            return None
        kind, name = raw.get("kind"), raw.get("name")
        if not isinstance(kind, str) or not isinstance(name, str):
            return None
        config = _validate(kind, name, raw.get("config"))
        return Request(path.stem, kind, name, config, _stamp(path))
    except (OSError, ValueError):
        return None


def list_pending() -> list[Request]:
    d = requests_dir()
    if not d.is_dir():
        return []
    found = [_read(p) for p in sorted(d.glob("*.json"))[:MAX_PENDING]]
    return [r for r in found if r]


def get(request_id: str) -> Request:
    if not ID_RE.match(request_id):
        raise RequestError(t("req.bad_id"))
    req = _read(requests_dir() / f"{request_id}.json")
    if req is None:
        raise RequestError(t("req.not_found", id=request_id))
    return req


def approve_request(request_id: str, confirm: Callable[[Request], bool]) -> list[str]:
    """Apply a pending request ONLY if `confirm` (the student's explicit yes) returns True.

    The request is read once here and that exact validated object is what is shown, confirmed and applied."""
    req = get(request_id)
    if req.kind == "plugin":
        marketplace = req.name.split("@", 1)[1]
        if marketplace not in extensions.known_marketplaces():
            raise RequestError(t("req.no_marketplace", marketplace=marketplace))
    if not confirm(req):
        raise RequestError(t("req.not_confirmed"))
    warnings = (extensions.set_plugin(req.name, True) if req.kind == "plugin"
                else extensions.set_mcp(req.name, req.config or {}))
    _log(req, "approved")
    (requests_dir() / f"{req.id}.json").unlink(missing_ok=True)
    return warnings


def reject_request(request_id: str) -> None:
    req = get(request_id)
    _log(req, "rejected")
    (requests_dir() / f"{req.id}.json").unlink(missing_ok=True)


def _log(req: Request, outcome: str) -> None:
    """Audit trail in the protected data dir."""
    path = hokma_data_dir() / "extension-decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "id": req.id, "kind": req.kind,
                            "name": req.name, "outcome": outcome, "risks": req.risks}) + "\n")
