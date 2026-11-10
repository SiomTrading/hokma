"""kind -> adapter, tier names and which CLIs are installed."""
from __future__ import annotations

import shutil
from typing import Any

from hokma.engines import catalog
from hokma.engines.base import EngineAdapter, EngineError

_MODEL_TIER = {"haiku": "fast", "sonnet": "balanced", "opus": "deep"}
CLIS = {"claude_code": "claude", "codex": "codex", "gemini_cli": "gemini", "kimi_code": "kimi"}


def tier_of(model: str | None) -> str:
    """A saved conversation model (haiku, sonnet, opus, a tier name or an exact model) as a tier or the exact name."""
    value = (model or "").strip()
    if value in _MODEL_TIER:
        return _MODEL_TIER[value]
    return value if value else "balanced"


def installed_clis(path: str | None = None) -> dict[str, str | None]:
    return {kind: shutil.which(binary, path=path) for kind, binary in CLIS.items()}


def build_adapter(kind: str, owner: Any) -> EngineAdapter:
    if kind == "claude_code":
        from hokma.engines.claude_code import ClaudeCodeAdapter

        return ClaudeCodeAdapter(owner)
    if kind == "api":
        from hokma.engines.direct.loop import DirectAdapter

        from hokma.engines.hokma_mcp import hokma_toolbox

        return DirectAdapter(hokma_toolbox)
    if kind == "codex":
        from hokma.engines.codex import CodexAdapter

        return CodexAdapter(owner)
    if kind == "gemini_cli":
        from hokma.engines.acp import AcpAdapter

        return AcpAdapter(owner)
    raise EngineError("unsupported_engine", f"engine {kind} is not available in this version")


_ = catalog
