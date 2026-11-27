"""PreToolUse hook that tells the Hokma MCP server WHICH agent is calling (task 3429, Hokma 1.2.7; rebuilt in 1.2.8, task 3430).

The server of Hokma is one process per Claude Code session, started with the environment of the session (HOKMA_AGENT). The agents launched
with the Agent tool (Ian, Sam, Ben, ...) share that process, so by the environment alone every one of them would act as the agent of the session.
Claude Code sends `agent_type` (the slug of the agent that Claude Code launched) and `tool_use_id` in the input of the hooks of a call that comes from a launched agent,
and hands the same `tool_use_id` to the MCP server in `params._meta["claudecode/toolUseId"]` (checked on Claude Code 2.1.332 with a real run, subagent included).

1.2.7 stamped the identity in the arguments of the call (`updatedInput`). That left a way open: if the stamp was ever lost (another hook answering in the same call, a change of
Claude Code), the server would have taken whatever the model wrote in that field. 1.2.8 sends nothing through the arguments. This hook writes ONE small file per call,
`<data>/guard/identity/<tool_use_id>.json`, with the agent that makes it, and the server reads it (and deletes it) with the `tool_use_id` that Claude Code itself put in the call.
The model cannot forge it: it does not know the id of a call before it makes it, it does not write the call's `_meta`, and the folder is among the protected paths (the guard
and the hardblock refuse any write there). No hook, no file: the server then acts as the principal of the session, never as what the arguments say.

The hook prints nothing and gives no `permissionDecision`, so the permission flow of the mode and the answer of every other hook are untouched. It fails CLOSED: anything
unexpected ends with exit code 2. A call without `agent_type` (the main agent) or without a usable `tool_use_id` leaves no file.

Run it by absolute path with an isolated interpreter (`python -I identity_hook.py --data D --protect P ...`): it uses only the standard library. Its file is among the protected paths:
the agent cannot edit it nor take its registration out.
"""
import json
import os
import re
import sys
import time

BLOCK = 2
FOLDER = ("guard", "identity")                        # under the data folder (a copy of the names lives in kit/hokma_tools.py: a test keeps both equal)
MAX_AGE = 24 * 3600                                    # a call that waited a day for the owner is long gone; older files are swept
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def identity_dir(data: str) -> str:
    return os.path.join(data, *FOLDER)


def record(payload: dict, data: str) -> str | None:
    """Write the file of this call when a launched agent makes it. Returns its path, or None when there is nothing to say."""
    if not isinstance(payload, dict) or not isinstance(payload.get("tool_input"), dict):
        raise ValueError("no tool_input")
    agent, call = payload.get("agent_type"), payload.get("tool_use_id")
    if not (isinstance(agent, str) and agent.strip() and isinstance(call, str) and _ID.match(call)):
        return None
    folder = identity_dir(data)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    sweep(folder)
    final = os.path.join(folder, call + ".json")
    tmp = os.path.join(folder, f".{call}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"agent": agent.strip()}, f)
    os.replace(tmp, final)
    return final


def sweep(folder: str) -> None:
    """Delete the files of calls that never reached the server (denied, cancelled): never fails the call."""
    try:
        limit = time.time() - MAX_AGE
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            try:
                if os.path.getmtime(path) < limit:
                    os.unlink(path)
            except OSError:
                pass
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    try:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError, OSError):
                pass
        args = sys.argv[1:] if argv is None else argv
        data = next((args[i + 1] for i, a in enumerate(args) if a == "--data" and i + 1 < len(args)), "")
        if not data:
            raise ValueError("no --data")
        raw = sys.stdin.buffer.read().decode("utf-8-sig") if getattr(sys.stdin, "buffer", None) else sys.stdin.read()
        record(json.loads(raw), data)
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
