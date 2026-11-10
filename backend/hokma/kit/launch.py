"""What Hokma passes to `claude` on EVERY launch.

The settings file is rebuilt from the package template each time and passed explicitly with
--settings; --setting-sources user keeps project/local settings of the working folder (which the
agent could edit) from loosening anything. The MCP servers the student approved are written to
<config>/hokma-mcp.json from the protected extensions file and passed with --mcp-config (servers the
student added themselves with `claude mcp add` keep working: they live in <config>/.claude.json). Remote servers
signed in from the panel get a `headersHelper` that reads their token from the credential store (engines/remote_mcp.py):
the file never holds a token.
"""
import json
import sys
from pathlib import Path
from dataclasses import dataclass

from hokma.core.config import claude_config_dir, hokma_data_dir, hokma_home
from hokma.kit import extensions, integrity
from hokma.kit.settings import protected_paths, write_settings


class IntegrityError(RuntimeError):
    pass


@dataclass
class LaunchSpec:
    args: list[str]
    env: dict[str, str]
    cwd: str


def check_integrity() -> None:
    if not integrity.is_installed_app():
        return  # dev checkout: no manifest
    problems = integrity.verify()
    if problems:
        raise IntegrityError("; ".join(problems[:5]))


MCP_RUN = Path(__file__).resolve().parent / "mcp_run.py"
TRASH_MCP = Path(__file__).resolve().parent / "trash_mcp.py"
HOKMA_SERVER = "hokma"


def hokma_server() -> dict:
    """Hokma's own MCP server (the trash tool). Started like the guard hooks: by absolute path, isolated interpreter.
    The engine sets HOKMA_WORK_ROOT and HOKMA_CONVERSATION_ID on the `claude` process; the environment expansion of
    the MCP config hands them to the server, and an empty or unexpanded value is ignored there."""
    args = ["-I", str(TRASH_MCP), "--data", str(hokma_data_dir()), "--workdir", str(hokma_home())]
    for p in protected_paths():
        args += ["--protect", str(p)]
    return {"type": "stdio", "command": sys.executable, "args": args,
            "env": {"HOKMA_WORK_ROOT": "${HOKMA_WORK_ROOT:-}", "HOKMA_CONVERSATION_ID": "${HOKMA_CONVERSATION_ID:-}",
                    "HOKMA_TASK_ID": "${HOKMA_TASK_ID:-}", "HOKMA_AGENT": "${HOKMA_AGENT:-}"}}


def write_mcp_config() -> str:
    """Approved servers for --mcp-config. stdio servers go through mcp_run.py so they start in a folder the agent
    cannot write (Claude's MCP config has no cwd); the real definition stays in the protected extensions file."""
    data, _ = extensions.load()
    from hokma.engines.remote_mcp import claude_entry, ready_servers

    servers = {}
    for name, cfg in data["mcpServers"].items():
        if cfg.get("type", "stdio") == "stdio":
            servers[name] = {"type": "stdio", "command": sys.executable,
                            "args": ["-I", str(MCP_RUN), "--data", str(hokma_data_dir()), "--name", name]}
        elif cfg.get("type") == "sse":
            servers[name] = cfg
    for server in ready_servers():        # http servers: only open or signed in, never one Claude would sign in to
        servers[server["name"]] = claude_entry(server)
    servers[HOKMA_SERVER] = hokma_server()                  # last: a student's server cannot take this name over
    path = claude_config_dir() / "hokma-mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}, indent=2) + "\n", encoding="utf-8")
    return str(path)


def prepare_claude_launch() -> LaunchSpec:
    check_integrity()
    path = write_settings()
    mcp = write_mcp_config()
    return LaunchSpec(
        args=["--settings", str(path), "--setting-sources", "user", "--mcp-config", mcp],
        env={"CLAUDE_CONFIG_DIR": str(claude_config_dir())},
        cwd=str(hokma_home()),
    )
