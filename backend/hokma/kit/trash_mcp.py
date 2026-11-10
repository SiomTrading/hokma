"""MCP server `hokma`: `trash_move(paths, reason)` and the tools of `hokma.kit.hokma_tools` (tasks, projects, journal,
IPS, decisions, schedules, extension requests, agents, bugs).

The trash tool: the way for an agent to get rid of files.

Deleting is refused by the always-on protections, so this is the way: the files go to `<data>/trash/{id}/` and a row
in `trash_items`, and the owner restores or empties them from Settings > Trash. The tool accepts paths inside the
working folder only, never Hokma's own places. There is no tool to list, restore or empty the trash.

Run it by absolute path with an isolated interpreter, from the protected data folder:
`python -I /abs/trash_mcp.py --data D --workdir W --protect P ...`. Claude Code starts it in the folder it works in,
so that folder is also an allowed root. Protocol: newline-delimited JSON-RPC 2.0 on stdin and stdout (MCP stdio).
"""
import json
import os
import sys

SERVER_NAME = "hokma"
DEFAULT_PROTOCOL = "2025-06-18"
TOOL = {
    "name": "trash_move",
    "description": ("Move files or folders of the working folder to the Hokma trash instead of deleting them. Use it "
                    "whenever something is no longer needed. The owner can restore anything from Settings > Trash."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 50,
                      "description": "Files or folders to move, absolute or relative to the working folder."},
            "reason": {"type": "string", "maxLength": 500, "description": "Why, in one short sentence."},
        },
        "required": ["paths", "reason"],
        "additionalProperties": False,
    },
}


def arguments(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == flag and i + 1 < len(args)]


def call_trash_move(arguments_in: dict, data: str, roots: list[str], protect: list[str]) -> tuple[str, bool]:
    from hokma.db import current_version, get_database
    from hokma.i18n import t
    from hokma.permissions import trash

    paths, reason = arguments_in.get("paths"), arguments_in.get("reason")
    try:
        if current_version() == 0:
            return t("trash.err.missing"), True
        done = trash.move(paths if isinstance(paths, list) else [], reason if isinstance(reason, str) else "",
                          db=get_database(), roots=roots, protect=protect, cwd=os.getcwd(),
                          conversation_id=_conversation_id())
    except trash.TrashError as exc:
        return exc.message, True
    return "\n".join(t("trash.moved", path=d["path"], id=d["id"]) for d in done), False


def _conversation_id() -> int | None:
    value = os.environ.get("HOKMA_CONVERSATION_ID", "")
    return int(value) if value.isdigit() else None


def build_roots(args: list[str]) -> list[str]:
    roots = arguments(args, "--workdir")
    extra = os.environ.get("HOKMA_WORK_ROOT", "")
    if extra and os.path.isabs(extra):                            # an unexpanded `${...}` is not absolute
        roots.append(extra)
    roots.append(os.getcwd())
    return [r for r in roots if r]


def reply(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(message: dict, data: str, roots: list[str], protect: list[str]) -> dict | None:
    method, ident, params = message.get("method"), message.get("id"), message.get("params") or {}
    if ident is None:
        return None                                               # notifications get no answer
    if method == "initialize":
        version = params.get("protocolVersion") if isinstance(params, dict) else None
        return {"jsonrpc": "2.0", "id": ident, "result": {
            "protocolVersion": version if isinstance(version, str) else DEFAULT_PROTOCOL,
            "capabilities": {"tools": {}}, "serverInfo": {"name": SERVER_NAME, "version": "1"}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": ident, "result": {}}
    if method == "tools/list":
        from hokma.kit import hokma_tools

        return {"jsonrpc": "2.0", "id": ident, "result": {"tools": [TOOL, *hokma_tools.definitions()]}}
    if method == "tools/call":
        from hokma.kit import hokma_tools

        name = params.get("name") if isinstance(params, dict) else None
        if name in hokma_tools.TOOLS:
            text, is_error = hokma_tools.call(name, params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": ident, "result": {"content": [{"type": "text", "text": text}],
                                                              "isError": is_error}}
        if name != TOOL["name"]:
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": "unknown tool"}}
        arguments_in = params.get("arguments")
        text, is_error = call_trash_move(arguments_in if isinstance(arguments_in, dict) else {}, data, roots, protect)
        return {"jsonrpc": "2.0", "id": ident, "result": {"content": [{"type": "text", "text": text}],
                                                          "isError": is_error}}
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "method not found"}}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    data = (arguments(args, "--data") or [""])[0]
    if not data:
        print("no --data", file=sys.stderr)
        return 1
    os.environ["HOKMA_DATA_DIR"] = data
    roots, protect = build_roots(args), arguments(args, "--protect") or [data]
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
            answer = handle(message, data, roots, protect) if isinstance(message, dict) else None
        except Exception as exc:                                  # never let one bad message end the server
            answer = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": type(exc).__name__}}
        if answer is not None:
            reply(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
