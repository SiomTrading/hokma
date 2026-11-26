"""PreToolUse hook, the first gate of the strategy creator (SPEC-031 6.6): without a numbered spec.md nothing is programmed.

It blocks, with exit code 2, a write of strategy.py or test_strategy.py under service/strategies/ unless it goes in STR-{NNN}-{slug}/versions/v{N}/ of a strategy folder that already holds its spec.md
(the folder is made with the card of the bank: spec.md, checklist.md, plan.md, tasks.md, adr/, results/ and versions/). The documents of the flow are free. Since 1.2.3 any agent or chat may take any step of the
flow and only the order is checked: what is written, not who writes. Outside service/strategies/ it does not interfere.

It only looks at the file system: the spec is a file, so a spec that cannot be found is a block.

Run it by absolute path with an isolated interpreter (`python -I creator_gate.py --data <data> --workdir <work>`), registered in the settings next to the other two hooks and
protected like them: the agent cannot edit this file nor take its registration out.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

BLOCK = 2
FILE_TOOLS = ("Edit", "MultiEdit", "Write", "NotebookEdit")
CODE_NAMES = ("strategy.py", "test_strategy.py")
FOLDER = re.compile(r"^(STR-\d{3})-([a-z][a-z0-9_-]{1,59})$")
VERSION = re.compile(r"^v[1-9][0-9]{0,3}$")
CODER = "strategy-coder"


class Denied(Exception):
    def __init__(self, rule: str, reason: str) -> None:
        self.rule, self.reason = rule, reason
        super().__init__(reason)


def _arguments(args, flag):
    return [args[i + 1] for i, a in enumerate(args) if a == flag and i + 1 < len(args)]


def has_spec(folder: str) -> bool:
    """Whether the strategy folder holds its spec.md (spec-four-phases, written like Diego writes his)."""
    path = os.path.join(folder, "spec.md")
    return os.path.isfile(path) and not os.path.islink(path)


def decide(payload: dict, data: str, workdir: str) -> None:
    """Raise Denied when the call must stop; return when it may go on."""
    tool = payload.get("tool_name", "")
    agent = payload.get("agent_type")
    agent = agent if isinstance(agent, str) and agent else None
    if tool not in FILE_TOOLS:
        return
    tool_input = payload.get("tool_input") or {}
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(target, str) or not target:
        return                                           # the guard hook is the one that refuses a write with no path
    cwd = payload.get("cwd")
    joined = target if os.path.isabs(target) else os.path.join(cwd if isinstance(cwd, str) else os.getcwd(), target)
    folder = os.path.join(os.path.realpath(workdir), "service", "strategies")
    candidates = {os.path.realpath(joined), os.path.normpath(joined)}
    inside = [c for c in candidates if os.path.commonpath([c, folder]) == folder] if all(os.path.isabs(c) for c in candidates) else []
    if not inside:
        return
    if len(inside) != len(candidates):
        raise Denied("file_not_allowed", "the path leads out of strategies/ by a link")
    real = os.path.realpath(joined)
    parts = os.path.relpath(real, folder).replace("\\", "/").split("/")
    if parts[-1] not in CODE_NAMES:
        return                                           # the documents of the flow (spec, checklist, plan, tasks, adr, results) are free: only the code waits for the spec
    match = FOLDER.match(parts[0]) if len(parts) == 4 and parts[1] == "versions" and VERSION.match(parts[2]) else None
    if match is None:
        raise Denied("file_not_allowed", "the code of a strategy goes in service/strategies/STR-{NNN}-{slug}/versions/v{N}/strategy.py or test_strategy.py")
    if not has_spec(os.path.join(folder, parts[0])):
        raise Denied("no_requirement", f"there is no spec for {match.group(1)}: write service/strategies/{parts[0]}/spec.md first (spec-four-phases), and without it nothing is programmed")


def log_block(data: str, tool: str, target: str, rule: str, reason: str) -> None:
    """One JSON line in <data>/guard/blocks.jsonl, the format of the other hooks. A problem writing it never turns a block into an allow."""
    try:
        folder = os.path.join(data, "guard")
        os.makedirs(folder, exist_ok=True)
        line = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "rule": rule, "tool": tool, "target": target[:200],
                "conversation_id": os.environ.get("HOKMA_CONVERSATION_ID", ""), "reason": reason[:200]}
        if os.environ.get("HOKMA_TASK_ID"):
            line["task_id"] = os.environ["HOKMA_TASK_ID"]
        fd = os.open(os.path.join(folder, "blocks.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    data = (_arguments(args, "--data") or [""])[0]
    try:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError, OSError):
                pass
        work = (_arguments(args, "--workdir") or [""])[0]
        if not data or not work:
            raise ValueError("no --data or --workdir")
        raw = sys.stdin.buffer.read().decode("utf-8-sig") if getattr(sys.stdin, "buffer", None) else sys.stdin.read()
        payload = json.loads(raw)
        try:
            decide(payload, data, work)
            return 0
        except Denied as denied:
            tool_input = payload.get("tool_input") or {}
            log_block(data, str(payload.get("tool_name", "")), str(tool_input.get("file_path") or tool_input.get("notebook_path") or ""), denied.rule, denied.reason)
            print(f"HOKMA_BLOCKED {denied.rule}", file=sys.stderr)
            print(denied.reason, file=sys.stderr)
            return BLOCK
    except BaseException as exc:                          # noqa: BLE001  fail closed on anything unexpected
        try:
            print("HOKMA_BLOCKED error", file=sys.stderr)
            print(f"{type(exc).__name__}: {exc}"[:500], file=sys.stderr)
        except BaseException:                             # noqa: BLE001
            pass
        return BLOCK


if __name__ == "__main__":
    raise SystemExit(main())
