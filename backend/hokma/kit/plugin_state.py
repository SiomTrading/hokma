"""What each plugin is doing for real, read from what the engine left on disk and not only from the saved list.

The saved list (`extensions.json`) says what the owner asked for. Whether a plugin loads is decided by the engine:
Claude Code records every plugin it installed in `<claude-config>/plugins/installed_plugins.json` (version 1: one
object per id; version 2: a list of installs per id) with the folder it unpacked it to. A plugin counts as active
only when that folder is there and its manifest, if it has one, reads. Nothing here writes or runs anything.

States: `active` (turned on and the engine has it), `disabled` (turned off), `pending` (turned on and not unpacked yet),
`installing`, `claude_only` (the plugin only works with Claude Code and another engine is the default), `attention`
(unpacked but a part it needs, like claude-mem's worker, is not running) and `error` (with the real reason). The panel does
not depend on which engine is selected: the files are Hokma's own isolated Claude config, present for every engine.
"""
import json
from pathlib import Path

from hokma.core.config import claude_config_dir
from hokma.i18n import t
from hokma.kit import extensions


def _read(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def engine_installs() -> dict[str, Path]:
    """{plugin id: folder} for what the engine recorded as installed (the folder may not exist any more)."""
    raw = _read(claude_config_dir() / "plugins" / "installed_plugins.json")
    plugins = raw.get("plugins") if isinstance(raw, dict) else None
    found: dict[str, Path] = {}
    for pid, entry in (plugins.items() if isinstance(plugins, dict) else []):
        entries = entry if isinstance(entry, list) else [entry]
        for item in entries:
            where = item.get("installPath") if isinstance(item, dict) else None
            if isinstance(where, str) and where:
                found[str(pid)] = Path(where)
                break
    return found


def state_of(plugin_id: str, enabled: bool, data: dict, installs: dict[str, Path], *, failures: dict[str, str] | None = None,
             installing: set[str] | frozenset[str] = frozenset(), engine: str | None = None,
             companion=None) -> dict:
    """{"state": ..., "reason": ..., "retry": bool} for one plugin of the saved list.

    `failures` holds the real reason of installs that failed; `installing` the ids being installed now; `engine` is the
    kind of the default engine (None when unknown); `companion(plugin_id, folder)` returns why a plugin that is unpacked
    still does not work (claude-mem without its worker), or None. Only Claude Code uses plugins: with another engine
    the card says so and never reads Active."""
    market = plugin_id.split("@", 1)[1]
    if market not in data["extraKnownMarketplaces"] and market not in extensions.BUILTIN_MARKETPLACES:
        return {"state": "error", "reason": t("status.ext.state.no_marketplace", marketplace=market), "retry": False}
    folder = installs.get(plugin_id)
    if plugin_id in installing:
        return {"state": "installing", "reason": t("status.ext.state.installing"), "retry": False}
    if folder is not None:
        if not folder.is_dir():
            return {"state": "error", "reason": t("status.ext.state.files_missing"), "retry": True}
        manifest = folder / ".claude-plugin" / "plugin.json"
        if manifest.is_file() and not isinstance(_read(manifest), dict):
            return {"state": "error", "reason": t("status.ext.state.bad_manifest"), "retry": True}
    elif failures and plugin_id in failures:
        return {"state": "error", "reason": failures[plugin_id], "retry": True}
    if not enabled:
        return {"state": "disabled", "reason": "", "retry": False}
    if folder is None:
        return {"state": "pending", "reason": t("status.ext.state.pending"), "retry": True}
    if engine is not None and engine != "claude_code":
        return {"state": "claude_only", "reason": t("status.ext.state.claude_only"), "retry": False}
    problem = companion(plugin_id, folder) if companion else None
    if problem:
        return {"state": "attention", "reason": problem, "retry": False}
    return {"state": "active", "reason": "", "retry": False}
