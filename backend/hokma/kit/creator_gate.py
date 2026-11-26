"""PreToolUse hook, the first gate of the strategy creator (SPEC-031 6.6): without a current requirement nothing is programmed.

It blocks, with exit code 2, a write to strategies/<slug>/ when the base has no current requirement for that slug, and a write to anything under strategies/ that is
not strategy.py or test_strategy.py of a slug folder (the delivery of the creator is the code, never a document). Since 1.2.2 any agent or chat may take any step of the flow: the gate no longer looks at who writes, only at what is written. Outside strategies/ it does not interfere.

It fails CLOSED: a base it cannot read is a block. It only reads: SQLite opens in read only mode and Postgres sets the session read only. Standard library plus psycopg when the base
is Postgres; it must not import the database layer of Hokma (only the server may), so it reads the two columns it needs with its own query.

Run it by absolute path with an isolated interpreter (`python -I creator_gate.py --data <data> --workdir <work>`), registered in the settings next to the other two hooks and
protected like them: the agent cannot edit this file nor take its registration out.
"""
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

BLOCK = 2
FILE_TOOLS = ("Edit", "MultiEdit", "Write", "NotebookEdit")
ALLOWED_NAMES = ("strategy.py", "test_strategy.py")
SLUG = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
CODER = "strategy-coder"
DB_FILE = "hokma.db"
QUERY = "SELECT 1 FROM user_rules r JOIN user_rule_briefs b ON b.rule_id = r.id AND b.status = 'current' WHERE r.slug = {mark} AND r.kind = 'strategy' LIMIT 1"


class Denied(Exception):
    def __init__(self, rule: str, reason: str) -> None:
        self.rule, self.reason = rule, reason
        super().__init__(reason)


def _arguments(args, flag):
    return [args[i + 1] for i, a in enumerate(args) if a == flag and i + 1 < len(args)]


def has_requirement(data: str, slug: str) -> bool:
    """Whether the base keeps a current requirement for this strategy. Any problem reading it raises: the gate then blocks."""
    config = {}
    try:
        with open(os.path.join(data, "database.json"), encoding="utf-8") as handle:
            config = json.load(handle)
    except FileNotFoundError:
        config = {}
    if isinstance(config, dict) and config.get("engine") == "postgres" and config.get("url"):
        import psycopg

        with psycopg.connect(config["url"], autocommit=True, connect_timeout=5) as conn:
            conn.execute("SET default_transaction_read_only = on")
            return conn.execute(QUERY.format(mark="%s"), (slug,)).fetchone() is not None
    path = os.path.join(data, DB_FILE)
    if not os.path.isfile(path):
        raise OSError("the database file does not exist")
    uri = "file:" + path.replace("\\", "/").replace("?", "%3f").replace("#", "%23") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        return conn.execute(QUERY.format(mark="?"), (slug,)).fetchone() is not None
    finally:
        conn.close()


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
    folder = os.path.join(os.path.realpath(workdir), "strategies")
    candidates = {os.path.realpath(joined), os.path.normpath(joined)}
    inside = [c for c in candidates if os.path.commonpath([c, folder]) == folder] if all(os.path.isabs(c) for c in candidates) else []
    if not inside:
        return
    if len(inside) != len(candidates):
        raise Denied("file_not_allowed", "the path leads out of strategies/ by a link")
    real = os.path.realpath(joined)
    parts = os.path.relpath(real, folder).replace("\\", "/").split("/")
    if len(parts) != 2 or parts[1] not in ALLOWED_NAMES or not SLUG.match(parts[0]):
        raise Denied("file_not_allowed", "strategies/<slug>/ holds strategy.py and test_strategy.py and nothing else: the delivery is the code, not a document")
    try:
        found = has_requirement(data, parts[0])
    except BaseException as exc:                          # noqa: BLE001  fail closed: a base that cannot be read is a block
        raise Denied("no_requirement", f"the requirement of {parts[0]} cannot be checked ({type(exc).__name__})") from None
    if not found:
        raise Denied("no_requirement", f"there is no current requirement for {parts[0]}: the requirement is written first, and without it nothing is programmed")


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
