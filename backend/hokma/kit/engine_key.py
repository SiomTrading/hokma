"""Prints the API key of one engine connection from the system credential store, for Claude Code's `apiKeyHelper`.
Started by the CLI with an isolated interpreter (-I). The key never goes through the environment the agent's
commands inherit. Prints nothing and exits 1 when there is no key.

`--mcp NAME` is Claude Code's `headersHelper` for a remote MCP server signed in from the panel: it prints
{"Authorization": "Bearer <token>"} from the service `hokma-engine-mcp` (status/mcp_oauth.py stores long tokens in
chunks: the head entry says `chunks:N`). The server's refresher keeps that token fresh."""
import re
import sys

MCP_SERVICE = "hokma-engine-mcp"
MCP_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def mcp_header(name: str) -> int:
    if not MCP_NAME.match(name):
        return 1
    try:
        import json

        import keyring

        account = f"{name}:access"
        value = keyring.get_password(MCP_SERVICE, account)
        if value and value.startswith("chunks:"):
            parts = [keyring.get_password(MCP_SERVICE, f"{account}#{i}") for i in range(int(value[7:]))]
            value = None if any(p is None for p in parts) else "".join(parts)
    except Exception:
        return 1
    if not value:
        return 1
    sys.stdout.write(json.dumps({"Authorization": f"Bearer {value}"}))
    return 0


def main(argv: list[str]) -> int:
    if "--mcp" in argv:
        i = argv.index("--mcp") + 1
        return mcp_header(argv[i]) if i < len(argv) else 1
    if "--conn" not in argv or argv.index("--conn") + 1 >= len(argv):
        return 1
    conn = argv[argv.index("--conn") + 1]
    if not conn.isdigit():
        return 1
    try:
        import keyring

        value = keyring.get_password("hokma-engine", conn)
    except Exception:
        return 1
    if not value:
        return 1
    if value.startswith("{"):                       # an imported setup: one JSON entry; --var picks the secret
        import json

        try:
            env = (json.loads(value).get("env") or {})
        except ValueError:
            return 1
        var = argv[argv.index("--var") + 1] if "--var" in argv and argv.index("--var") + 1 < len(argv) else ""
        value = str(env.get(var) or "")
        if not value:
            return 1
    sys.stdout.write(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
