"""Start an approved stdio MCP server from a working folder the agent cannot write.

Claude's MCP config has no `cwd` field (an unknown key is dropped; checked on Claude Code 2.1.317), so the
server would start in the student's working folder, where the agent can plant files that a relative command
or argument would pick up. Hokma writes this launcher into `hokma-mcp.json` instead of the raw command; it
reads the APPROVED definition from the protected `<data>/extensions.json`, moves to `<data>/mcp-cwd` and
replaces itself with the real program.

Run it by absolute path with an isolated interpreter (`python -I /abs/mcp_run.py --data D --name N`). It only
uses the standard library.
"""
import json
import os
import subprocess
import sys

from hokma.i18n import t


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        data = args[args.index("--data") + 1]
        name = args[args.index("--name") + 1]
        with open(os.path.join(data, "extensions.json"), encoding="utf-8") as f:
            cfg = json.load(f)["mcpServers"][name]
        command, argv_rest, extra_env = cfg["command"], list(cfg.get("args", [])), dict(cfg.get("env", {}))
        if not isinstance(command, str) or not all(isinstance(a, str) for a in argv_rest) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in extra_env.items()):
            raise ValueError("invalid definition")
        env = {**os.environ, **extra_env}
        cwd = os.path.join(data, "mcp-cwd")
        os.makedirs(cwd, exist_ok=True)
        os.chdir(cwd)
    except Exception as exc:
        print(t("mcp_run.failed", error=type(exc).__name__), file=sys.stderr)
        return 1
    if os.name == "posix":
        os.execvpe(command, [command, *argv_rest], env)      # does not return
    return subprocess.call([command, *argv_rest], env=env)


if __name__ == "__main__":
    raise SystemExit(main())
