"""Pure helpers for the stream-json lines the CLI prints."""
import json
from typing import Any

MAX_TARGET = 500
MAX_RESULT_TEXT = 4000

_KINDS = {"Write": "write_file", "Edit": "edit_file", "MultiEdit": "edit_file", "NotebookEdit": "edit_file",
          "Read": "read_file", "NotebookRead": "read_file", "Bash": "run_command", "PowerShell": "run_command",
          "WebFetch": "fetch_url", "WebSearch": "search", "Grep": "search", "Glob": "search"}
_TARGET_KEYS = ("file_path", "notebook_path", "command", "url", "query", "pattern", "path")


def parse_line(line: bytes | str) -> dict[str, Any] | None:
    try:
        msg = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    return msg if isinstance(msg, dict) and isinstance(msg.get("type"), str) else None


def summarize_tool(name: str, tool_input: Any) -> dict[str, str]:
    """`{kind, target}`: the action in neutral terms; the panel writes the sentence in the user's language."""
    target = ""
    if isinstance(tool_input, dict):
        for key in _TARGET_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                target = value
                break
    return {"kind": _KINDS.get(name, "other"), "target": target[:MAX_TARGET]}


def text_delta(msg: dict[str, Any]) -> str | None:
    if msg.get("type") != "stream_event":
        return None
    event = msg.get("event") or {}
    delta = event.get("delta") or {}
    if event.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
        return delta.get("text") or ""
    return None


def is_message_start(msg: dict[str, Any]) -> bool:
    return msg.get("type") == "stream_event" and (msg.get("event") or {}).get("type") == "message_start"


def tool_uses(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """Complete tool_use blocks of an `assistant` message."""
    if msg.get("type") != "assistant":
        return []
    content = (msg.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]


def tool_results(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """`{tool_use_id, is_error, text}` for each tool_result block of a `user` message."""
    if msg.get("type") != "user":
        return []
    content = (msg.get("message") or {}).get("content")
    out = []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            raw = block.get("content")
            if isinstance(raw, list):
                raw = "".join(p.get("text", "") for p in raw if isinstance(p, dict))
            out.append({"tool_use_id": block.get("tool_use_id", ""), "is_error": bool(block.get("is_error")),
                        "text": str(raw or "")[:MAX_RESULT_TEXT]})
    return out


def assistant_text(msg: dict[str, Any]) -> str:
    if msg.get("type") != "assistant":
        return ""
    content = (msg.get("message") or {}).get("content")
    if not isinstance(content, list):
        return ""
    return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


def usage_of(result: dict[str, Any]) -> dict[str, Any]:
    u = result.get("usage") or {}
    return {"tokens_in": int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0),
            "tokens_out": int(u.get("output_tokens") or 0),
            "cache_read": int(u.get("cache_read_input_tokens") or 0),
            "cost_usd": float(result.get("total_cost_usd") or 0)}


def is_aborted(result: dict[str, Any]) -> bool:
    return result.get("terminal_reason") == "aborted_streaming"


def user_message(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": blocks}}


def permission_reply(request_id: str, allow: bool, tool_input: Any, reason: str) -> dict[str, Any]:
    if allow:
        body = {"behavior": "allow", "updatedInput": tool_input if isinstance(tool_input, dict) else {}}
    else:
        body = {"behavior": "deny", "message": reason}
    return {"type": "control_response", "response": {"subtype": "success", "request_id": request_id,
                                                      "response": body}}


def control_error(request_id: str, error: str) -> dict[str, Any]:
    return {"type": "control_response", "response": {"subtype": "error", "request_id": request_id, "error": error}}


def interrupt_request(request_id: str) -> dict[str, Any]:
    return {"type": "control_request", "request_id": request_id, "request": {"subtype": "interrupt"}}
