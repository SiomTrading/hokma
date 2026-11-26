"""Call one tool of the Hokma MCP server from a terminal, against a data folder (SPEC-032, to try library_list and library_request without a chat).

    .venv/bin/python scripts/opt_call_tool.py <folder> <tool> '<json arguments>'

`<folder>` is the folder that holds `data` and `work` (the one made by test_load_the_nq_run_for_the_screen). The call goes through the same stdio server that the
engines start (kit/trash_mcp.py), as the agent `strategy-researcher`, and prints the answer of the tool. It writes only what the tool writes (library_request saves one
request in the database of that folder). Exit 0 when the tool answered, 1 when it answered with an error, 2 for a wrong use.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "backend" / "hokma" / "kit" / "trash_mcp.py"
AGENT = "strategy-researcher"
TIMEOUT = 120


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    folder, tool, raw = Path(argv[1]).resolve(), argv[2], argv[3]
    try:
        arguments = json.loads(raw)
    except ValueError as exc:
        print(f"the arguments are not JSON: {exc}", file=sys.stderr)
        return 2
    data, work = folder / "data", folder / "work"
    if not (data / "hokma.db").is_file():
        print(f"{data / 'hokma.db'} does not exist: use the folder where the data of the journey was loaded", file=sys.stderr)
        return 2
    env = {**os.environ, "HOKMA_DATA_DIR": str(data), "HOKMA_HOME": str(work), "HOME": str(folder / "home"), "HOKMA_AGENT": AGENT, "HOKMA_LANG": "en"}
    server = subprocess.Popen([sys.executable, "-I", str(SERVER), "--data", str(data), "--workdir", str(work)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, env=env, cwd=str(work))

    def send(message: dict) -> dict | None:
        assert server.stdin is not None and server.stdout is not None
        server.stdin.write(json.dumps(message) + "\n")
        server.stdin.flush()
        return json.loads(server.stdout.readline()) if "id" in message else None

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "opt_call_tool", "version": "1"}}})
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        answer = send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": arguments}})
    finally:
        if server.stdin:
            server.stdin.close()
        server.wait(timeout=TIMEOUT)
    result = (answer or {}).get("result")
    if result is None:
        print(json.dumps((answer or {}).get("error", answer), indent=2))
        return 1
    for part in result.get("content", []):
        text = part.get("text", "")
        try:
            print(json.dumps(json.loads(text), indent=2, ensure_ascii=False))
        except ValueError:
            print(text)
    return 1 if result.get("isError") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
