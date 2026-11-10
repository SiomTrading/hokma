"""What each plugin is doing for real, read from what the engine left on disk and not only from the saved list.

The saved list (`extensions.json`) says what the owner asked for. Whether a plugin loads is decided by the engine:
Claude Code records every plugin it installed in `<claude-config>/plugins/installed_plugins.json` (version 1: one
object per id; version 2: a list of installs per id) with the folder it unpacked it to. A plugin counts as active
only when that folder is there and its manifest, if it has one, reads. Nothing here writes or runs anything.

States: `active` (turned on and the engine has it), `disabled` (turned off), `pending` (turned on and the engine has not
unpacked it yet: it does at the next start of a conversation) and `error` (with the reason in words). The panel does
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


def state_of(plugin_id: str, enabled: bool, data: dict, installs: dict[str, Path]) -> dict:
    """{"state": ..., "reason": ...} for one plugin of the saved list."""
    market = plugin_id.split("@", 1)[1]
    if market not in data["extraKnownMarketplaces"] and market not in extensions.BUILTIN_MARKETPLACES:
        return {"state": "error", "reason": t("status.ext.state.no_marketplace", marketplace=market)}
    folder = installs.get(plugin_id)
    if folder is not None:
        if not folder.is_dir():
            return {"state": "error", "reason": t("status.ext.state.files_missing")}
        manifest = folder / ".claude-plugin" / "plugin.json"
        if manifest.is_file() and not isinstance(_read(manifest), dict):
            return {"state": "error", "reason": t("status.ext.state.bad_manifest")}
    if not enabled:
        return {"state": "disabled", "reason": ""}
    if folder is None:
        return {"state": "pending", "reason": t("status.ext.state.pending")}
    return {"state": "active", "reason": ""}
