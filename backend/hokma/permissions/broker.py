"""The permission broker: the protections of Hokma for ANY engine.

Claude Code runs `guard` and `hardblock` as hooks. Other engines have no hooks, so every tool request they make
(a command to run, a file to change, an MCP call) is turned into the same payload and passed through the very same
rules here, inside Hokma's process and not inside the agent's. The rules are imported, never copied.

Order of every request: the broker first, the permission mode after. Closed on failure: any exception is a block.
"""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from typing import Any

from hokma.kit import guard, hardblock
from hokma.kit.hardblock import Block, Ctx, norm_text, short

# Names a request can use, mapped to the tool the rules know.
_ALIASES = {"read_file": "Read", "write_file": "Write", "edit_file": "Edit", "glob": "Glob", "grep": "Grep",
            "list_dir": "Glob", "run_command": "Bash"}


@dataclass
class Verdict:
    allowed: bool
    rule: str = ""
    target: str = ""

    @property
    def text(self) -> str:
        return f"HOKMA_BLOCKED {self.rule}" if not self.allowed else ""


ALLOW = Verdict(True)


@dataclass
class BrokerCtx:
    data: str
    roots: list[str]
    protect: list[str]
    conversation_id: str = ""
    task_id: str = ""
    shell: str = "Bash"          # "Bash" or "PowerShell": which analyser the command text gets

    def hardblock_ctx(self, cwd: str) -> Ctx:
        return Ctx(self.data, self.roots, self.protect, cwd=cwd, conversation_id=self.conversation_id,
                   task_id=self.task_id)


def _text_rule(text: str, ctx: Ctx) -> Block | None:
    """Free text (a URL, MCP arguments): secrets stores and real-money broker hosts."""
    low = norm_text(text)
    if hardblock.SECRET_RE.search(low) or hardblock.data_secret_in_text(low, ctx):
        return Block("secrets", short(text))
    if (ctx.host_re and ctx.host_re.search(low)) or (ctx.url_re and ctx.url_re.search(low)):
        return Block("real_money", short(text))
    return None


def _flatten(value: Any, out: list[str], depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for k, v in value.items():
            out.append(str(k))
            _flatten(v, out, depth + 1)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _flatten(v, out, depth + 1)


def _canonical(tool: str) -> str:
    """Case-insensitive alias, then the exact name the rules know. Unknown names stay as given."""
    low = tool.strip().lower()
    if low in _ALIASES:
        return _ALIASES[low]
    for known in (*guard.FILE_TOOLS, *hardblock.READ_TOOLS, *hardblock.SHELL_TOOLS):
        if known.lower() == low:
            return known
    return low if low in ("web_fetch", "mcp") or low.startswith("mcp__") else tool.strip()


_MCP_PART = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MCP_KEYS = {"server", "tool", "arguments"}


def _mcp_name(tool: str, tool_input: dict[str, Any]) -> str | None:
    """The real `mcp__<server>__<tool>` name of an MCP request. A name that already is one is used as is. The generic
    `mcp` form is accepted only with a strict shape: exactly the keys server and tool (plus arguments), both plain
    identifiers. Anything ambiguous returns None and the request is denied."""
    if tool.startswith("mcp__"):
        return tool
    if not set(tool_input) <= _MCP_KEYS or not {"server", "tool"} <= set(tool_input):
        return None
    server, name = tool_input["server"], tool_input["tool"]
    if not (isinstance(server, str) and isinstance(name, str) and _MCP_PART.match(server) and _MCP_PART.match(name)):
        return None
    return f"mcp__{server}__{name}"


def _evaluate(tool: str, tool_input: dict[str, Any], cwd: str, bctx: BrokerCtx) -> Verdict:
    tool = _canonical(tool)
    if tool == "Bash" and bctx.shell == "PowerShell":
        tool = "PowerShell"
    ctx = bctx.hardblock_ctx(cwd)
    payload = {"tool_name": tool, "tool_input": tool_input, "cwd": cwd}
    known = (tool in guard.FILE_TOOLS or tool in hardblock.READ_TOOLS or tool in hardblock.SHELL_TOOLS
             or tool == "web_fetch" or tool == "mcp" or tool.startswith("mcp__"))
    if not known:                                              # deny by default: a tool the rules do not know
        return Verdict(False, "hokma_internal", short(tool))
    if tool in guard.FILE_TOOLS:
        code, _ = guard.decide(payload, bctx.protect)
        if code:
            return Verdict(False, "hokma_internal", short(str(tool_input.get("file_path") or "")))
    if tool == "web_fetch":
        block = _text_rule(str(tool_input.get("url", "")), ctx)
    elif tool == "mcp" or tool.startswith("mcp__"):
        name = _mcp_name(tool, tool_input)
        if name is None:                                       # deny the generic form unless it is unambiguous
            return Verdict(False, "hokma_internal", "mcp")
        parts: list[str] = [name]
        _flatten(tool_input, parts)
        block = hardblock.evaluate({"tool_name": name, "tool_input": {}}, ctx)
        if block is None:
            hit = next((x for x in parts if hardblock.ORDER_TOOL.search(x)), None)
            if hit is not None:
                block = Block("real_money", short(hit))
        block = block or _text_rule("\n".join(parts), ctx)
    else:
        block = hardblock.evaluate(payload, ctx)
    return Verdict(False, block.rule, block.target) if block else ALLOW


def check(tool: str, tool_input: dict[str, Any], *, cwd: str, ctx: BrokerCtx) -> Verdict:
    """Allow or block one tool request. Writes the block to `<data>/guard/blocks.jsonl` (the same log the hook
    writes), so the engine shows the `guard_block` card and stores the `guard_events` row as it does today."""
    try:
        if not isinstance(tool_input, dict):
            raise ValueError("tool_input is not an object")
        cwd = cwd if isinstance(cwd, str) and os.path.isabs(cwd) else (ctx.roots[0] if ctx.roots else os.getcwd())
        verdict = _evaluate(tool, tool_input, cwd, ctx)
        if not verdict.allowed:
            hardblock.log_block(ctx.hardblock_ctx(cwd), tool, Block(verdict.rule, verdict.target))
        return verdict
    except Exception as exc:                                   # closed on failure
        try:
            hardblock.log_block(ctx.hardblock_ctx(ctx.roots[0] if ctx.roots else os.getcwd()), tool,
                                Block("hokma_internal", f"broker error {type(exc).__name__}"))
        except Exception:
            pass
        return Verdict(False, "hokma_internal", f"broker error {type(exc).__name__}")


_DEST_KEYS = ("move_path", "new_path", "dest", "destination", "to", "target_path", "rename_to")
_WRITE_KINDS = ("add", "create", "update", "modify", "write", "edit", "replace", "patch")
_DELETE_KINDS = ("delete", "remove", "deleted", "removed")
_MOVE_KINDS = ("move", "rename", "moved", "renamed")


def file_change_payloads(changes: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """File changes -> broker requests. Delete, and move or rename (which removes the source), are the `delete`
    rule (deleting goes through `trash_move`). Every destination field is checked as a `Write` target. An
    unrecognised kind becomes a request the broker refuses."""
    out: list[tuple[str, dict[str, Any]]] = []
    for ch in changes:
        path, kind = str(ch.get("path", "")), str(ch.get("kind", "update")).lower()
        dests = [str(ch[k]) for k in _DEST_KEYS if ch.get(k) and str(ch[k]) != path]
        if kind in _DELETE_KINDS or kind in _MOVE_KINDS or dests:
            out.append(("Bash", {"command": f"rm {shlex.quote(path)}"}))
        elif kind in _WRITE_KINDS:
            tool_input: dict[str, Any] = {"file_path": path}
            if isinstance(ch.get("content"), str):
                tool_input["content"] = ch["content"]
            out.append(("Write", tool_input))
        else:
            out.append(("unknown_change_kind", {"kind": kind}))
        for dest in dests:
            out.append(("Write", {"file_path": dest}))
    return out
