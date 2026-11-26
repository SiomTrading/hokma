"""Pure helpers for the stream-json lines the CLI prints."""
import json
from typing import Any

MAX_TARGET = 500
MAX_DESK_TARGET = 6000          # a desk request is shown whole: what the owner approves is what runs (mandate up to 4000)
MAX_RESULT_TEXT = 4000
MAX_AGENT_DESCRIPTION = 200
SUBAGENT_TOOLS = {"Agent", "Task"}      # the tool that starts a subagent (recorded: "Agent" in CLI 2.1.331, `Task` in its tool list)

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


def _desk_target(name: str, tool_input: Any) -> str:
    """What a desk tool is about to create or change, in neutral `key=value` terms, so the owner sees it in the
    permission request (the panel shows it as the target). Empty for any other tool."""
    short = name.rsplit("__", 1)[-1]
    if short in ("today_configure", "account_create", "account_reset", "desk_add_demo_account", "desk_add_account", "desk_remove_account", "desk_bind_strategy", "desk_money_set", "gmail_send",
                 "order_place", "order_replace", "order_cancel", "desk_create_swo") and isinstance(tool_input, dict):
        return " | ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in tool_input.items())
    if short not in ("desk_create", "desk_configure") or not isinstance(tool_input, dict):
        return ""
    parts = []
    if short == "desk_configure":
        parts.append(f"desk {tool_input.get('desk_id')}")
    for key in ("name", "template", "capital", "assets", "mandate", "token_budget", "agent", "model"):
        value = tool_input.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        text = " ".join(str(value).split())
        parts.append(f"{key}={text}")
    return " | ".join(parts)


def summarize_tool(name: str, tool_input: Any) -> dict[str, str]:
    """`{kind, target}`: the action in neutral terms; the panel writes the sentence in the user's language."""
    desk = _desk_target(name, tool_input)
    if desk:
        return {"kind": "other", "target": desk[:MAX_DESK_TARGET]}
    target = ""
    if isinstance(tool_input, dict):
        for key in _TARGET_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                target = value
                break
    return {"kind": _KINDS.get(name, "other"), "target": target[:MAX_TARGET]}


def agent_call(name: str, tool_input: Any) -> dict[str, str] | None:
    """`{slug, description}` of a subagent launch, None for any other tool. The slug is the `subagent_type` the CLI got."""
    if name not in SUBAGENT_TOOLS or not isinstance(tool_input, dict):
        return None
    slug = tool_input.get("subagent_type")
    description = tool_input.get("description") or tool_input.get("prompt") or ""
    return {"slug": slug if isinstance(slug, str) else "",
            "description": " ".join(str(description).split())[:MAX_AGENT_DESCRIPTION]}


def task_event(msg: dict[str, Any]) -> dict[str, Any] | None:
    """The start or end of a background task (a subagent or a shell command) from a `system` line, or None."""
    if msg.get("type") != "system" or not isinstance(msg.get("tool_use_id"), str) or not msg["tool_use_id"]:
        return None
    if msg.get("subtype") == "task_started":
        return {"type": "task_start", "id": msg["tool_use_id"], "task_type": str(msg.get("task_type") or ""),
                "text": str(msg.get("description") or "")}
    if msg.get("subtype") == "task_notification":
        return {"type": "task_end", "id": msg["tool_use_id"], "status": str(msg.get("status") or ""),
                "text": str(msg.get("summary") or "")[:MAX_RESULT_TEXT]}
    return None


def task_track(msg: dict[str, Any]) -> tuple[str, str] | None:
    """What a `system` line says about the background tasks of the conversation itself (bug 1257), or None.

    `("add", id)`: a task of the main thread was started in the background. `("done", id)`: the CLI reported its end
    (`task_notification`), which is what makes it open an automatic turn to tell the model. The `task_updated` line
    that comes just before it is not an end: the turn that tells the model has not begun. A task a subagent owns
    (`owned_by_subagent`) belongs to that subagent: its end does not wake the main thread, so it is not followed here."""
    if msg.get("type") != "system" or msg.get("owned_by_subagent"):
        return None
    task_id = msg.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        return None
    subtype = msg.get("subtype")
    if subtype == "task_started" and msg.get("is_backgrounded") is True:
        return ("add", task_id)
    if subtype == "task_notification":
        return ("done", task_id)
    return None


def launch_receipt(msg: dict[str, Any]) -> bool:
    """True when a `user` line carries the receipt of a subagent started in the background (its answer comes later)."""
    result = msg.get("tool_use_result")
    return isinstance(result, dict) and bool(result.get("isAsync"))


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
