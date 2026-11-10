"""Prints the API key of one engine connection from the system credential store, for Claude Code's `apiKeyHelper`.
Started by the CLI with an isolated interpreter (-I). The key never goes through the environment the agent's
commands inherit. Prints nothing and exits 1 when there is no key."""
import sys


def main(argv: list[str]) -> int:
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
