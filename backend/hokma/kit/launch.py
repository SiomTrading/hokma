"""What Hokma passes to `claude` on EVERY launch.

The settings file is rebuilt from the package template each time and passed explicitly with
--settings; --setting-sources user keeps project/local settings of the working folder (which the
agent could edit) from loosening anything. The MCP servers the student approved are written to
<config>/hokma-mcp.json from the protected extensions file and passed with --mcp-config (servers the
student added themselves with `claude mcp add` keep working: they live in <config>/.claude.json).
"""
import json
import sys
from pathlib import Path
from dataclasses import dataclass

from hokma.core.config import claude_config_dir, hokma_data_dir, hokma_home
from hokma.kit import extensions, integrity
from hokma.kit.settings import write_settings


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


def write_mcp_config() -> str:
    """Approved servers for --mcp-config. stdio servers go through mcp_run.py so they start in a folder the agent
    cannot write (Claude's MCP config has no cwd); the real definition stays in the protected extensions file."""
    data, _ = extensions.load()
    servers = {}
    for name, cfg in data["mcpServers"].items():
        if cfg.get("type", "stdio") == "stdio":
            servers[name] = {"type": "stdio", "command": sys.executable,
                            "args": ["-I", str(MCP_RUN), "--data", str(hokma_data_dir()), "--name", name]}
        else:
            servers[name] = cfg
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
