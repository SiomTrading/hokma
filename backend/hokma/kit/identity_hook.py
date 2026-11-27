"""PreToolUse hook that tells the Hokma MCP server WHICH agent is calling (task 3429, Hokma 1.2.7).

The server of Hokma is one process per Claude Code session, started with the environment of the session (HOKMA_AGENT). The agents launched
with the Agent tool (Ian, Sam, Ben, ...) share that process, so by the environment alone every one of them would act as the agent of the session.
Claude Code sends `agent_type` (the slug of the agent) and `agent_id` in the input of the hooks of a call that comes from a launched agent, and
lets a PreToolUse hook rewrite the input of any tool, MCP tools included (`updatedInput`, checked on Claude Code 2.1.332 with a real run).

So this hook ALWAYS sets the field `_hokma_agent` of the call of every tool of the server (matcher `mcp__hokma__.*`):
the `agent_type` of the one that calls, or HOKMA_AGENT of the session when it is the main agent, or an empty text when neither exists. What the model wrote in that field is
overwritten, never trusted: the model cannot take another identity. The server takes the field off the arguments before it validates them (kit/trash_mcp.py).

It answers with no `permissionDecision`, so the permission flow of the mode is the same as without the hook. It fails CLOSED: anything unexpected ends with exit code 2.

Run it by absolute path with an isolated interpreter (`python -I identity_hook.py`): it uses only the standard library, so the constant below is a copy of
kit/hokma_tools.IDENTITY_FIELD (a test keeps both equal). Its file is among the protected paths: the agent cannot edit it nor take its registration out.
"""
import json
import os
import sys

BLOCK = 2
IDENTITY_FIELD = "_hokma_agent"


def decide(payload: dict, env: dict) -> dict:
    """The answer of the hook for one call: the same input plus the identity of who calls."""
    if not isinstance(payload, dict) or not isinstance(payload.get("tool_input"), dict):
        raise ValueError("no tool_input")
    agent = payload.get("agent_type")
    who = agent if isinstance(agent, str) and agent.strip() else env.get("HOKMA_AGENT", "")
    updated = {**payload["tool_input"], IDENTITY_FIELD: who.strip() if isinstance(who, str) else ""}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": updated}}


def main() -> int:
    try:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError, OSError):
                pass
        raw = sys.stdin.buffer.read().decode("utf-8-sig") if getattr(sys.stdin, "buffer", None) else sys.stdin.read()
        answer = decide(json.loads(raw), os.environ)
        sys.stdout.write(json.dumps(answer, ensure_ascii=True))
        sys.stdout.flush()
        return 0
    except BaseException as exc:                          # noqa: BLE001  fail closed on anything unexpected
        try:
            print("HOKMA_BLOCKED identity", file=sys.stderr)
            print(f"{type(exc).__name__}: {exc}"[:500], file=sys.stderr)
        except BaseException:                             # noqa: BLE001
            pass
        return BLOCK


if __name__ == "__main__":
    raise SystemExit(main())
