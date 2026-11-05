"""MCP server definitions that Hokma passes to Claude with --mcp-config.

An approved stdio MCP server is, by definition, a program that runs OUTSIDE the Bash sandbox with the
student's rights, and the student can approve it. The approval IS the control. So this module does not try to
guess what is dangerous (there is always one more flag that slips through). It does two things:

1. `validate_mcp` only checks the SHAPE of the definition (types, sizes, https for remote servers).
2. `code_risks` flags, by TEXT ONLY (never looking at the disk), the cases where the program that will run can
   be changed by the agent. The approval shows the exact command, every argument and every environment
   variable, and when there are risks it shows a clear warning and asks for an extra accept word.

Accepted shapes:
  {"command": "npx", "args": ["-y", "pkg"], "env": {"KEY": "value"}}        (stdio, `type` optional)
  {"type": "http" | "sse", "url": "https://..."}
"""
import os
import re

from hokma.i18n import t
from hokma.kit.guard import under_any
from hokma.kit.writable import agent_writable_roots

SERVER_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
HTTPS_URL = re.compile(r"^https://[^\s]+$")
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
URL_LIKE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
MAX_COMMAND, MAX_ARGS, MAX_ARG_LEN, MAX_ENV, MAX_ENV_VALUE = 500, 30, 500, 20, 1000

INTERPRETERS = {"python", "python3", "node", "bash", "sh", "zsh", "pwsh", "powershell", "cmd", "deno", "bun",
                "ruby", "perl", "npx", "uvx"}
CODE_SHORT_FLAGS = ("-c", "-e", "-m", "-r")
CODE_LONG_FLAGS = ("--require", "--import", "--loader", "--eval")
CODE_FILE_EXTENSIONS = (".js", ".mjs", ".cjs", ".ts", ".py", ".sh", ".rb", ".pl", ".ps1", ".bat", ".cmd", ".exe",
                        ".jar")


def validate_mcp(config: object) -> dict:
    """Shape check only; returns a clean copy. See the module docstring for why there is no content denylist."""
    if not isinstance(config, dict):
        raise ValueError(t("mcp.not_object"))
    kind = config.get("type", "stdio")
    if kind in ("http", "sse"):
        url = config.get("url")
        if not isinstance(url, str) or not HTTPS_URL.match(url) or len(url) > 1000:
            raise ValueError(t("mcp.remote_url"))
        return {"type": kind, "url": url}
    if kind != "stdio":
        raise ValueError(t("mcp.bad_type"))
    command = config.get("command")
    if not isinstance(command, str) or not command.strip() or len(command) > MAX_COMMAND or _has_ctrl(command):
        raise ValueError(t("mcp.bad_command"))
    args = config.get("args", [])
    if not isinstance(args, list) or len(args) > MAX_ARGS or not all(
            isinstance(a, str) and len(a) <= MAX_ARG_LEN and "\0" not in a for a in args):
        raise ValueError(t("mcp.bad_args"))
    env = config.get("env", {})
    if not isinstance(env, dict) or len(env) > MAX_ENV:
        raise ValueError(t("mcp.bad_env"))
    for k, v in env.items():
        if not (isinstance(k, str) and ENV_KEY.match(k) and isinstance(v, str) and len(v) <= MAX_ENV_VALUE
                and "\0" not in v):
            raise ValueError(t("mcp.bad_env"))
    clean = {"type": "stdio", "command": command, "args": list(args)}
    if env:
        clean["env"] = dict(env)
    return clean


def _has_ctrl(s: str) -> bool:
    return any(ch in s for ch in ("\0", "\n", "\r"))


def _path_risk(value: str) -> str | None:
    """Reason why `value` looks like a path the agent could control, judging only by its text."""
    if URL_LIKE.match(value):
        return None
    if os.path.isabs(value):
        if under_any(value, [str(r) for r in agent_writable_roots()]):
            return t("mcp.reason.writable")
        return None
    if "/" in value or "\\" in value:
        return t("mcp.reason.relative")
    if value.lower().endswith(CODE_FILE_EXTENSIONS):
        return t("mcp.reason.code_file")
    return None


def _split_flag(arg: str) -> str:
    """For `--config=/x` look at the value after the first `=`."""
    if arg.startswith("-") and "=" in arg:
        return arg.split("=", 1)[1]
    return arg


def _interpreter_name(command: str) -> str:
    base = re.split(r"[\\/]", command)[-1].lower()
    return base[:-4] if base.endswith(".exe") else base


def _loads_code(arg: str) -> bool:
    if arg in CODE_SHORT_FLAGS or any(arg == f or arg.startswith(f + "=") for f in CODE_LONG_FLAGS):
        return True
    return arg[:1] == "-" and arg[:2] in CODE_SHORT_FLAGS and not arg.startswith("--")      # -cprint(1), -rmod


def code_risks(config: dict) -> list[str]:
    """Text-only reasons why the program that will run could be changed by the agent."""
    if config.get("type", "stdio") != "stdio":
        return []
    risks: list[str] = []
    command, args, env = config["command"], config.get("args", []), config.get("env", {})
    why = _path_risk(command)
    if why:
        risks.append(t("mcp.risk.command", reason=why))
    for i, a in enumerate(args, 1):
        why = _path_risk(_split_flag(a))
        if why:
            risks.append(t("mcp.risk.argument", n=i, reason=why))
    for k, v in env.items():
        why = _path_risk(v)
        if why:
            risks.append(t("mcp.risk.variable", name=k, reason=why))
    if _interpreter_name(command) in INTERPRETERS:
        flagged = [a for a in args if _loads_code(a)]
        if flagged:
            risks.append(t("mcp.risk.interpreter", flags=", ".join(flagged[:3])))
    return risks
