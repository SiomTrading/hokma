"""Student extensions: plugins and marketplaces that Hokma merges into the Claude settings.

AUTHORITATIVE copy: <data>/extensions.json, in the protected data dir (the agent cannot write there).
INFORMATIONAL mirror: <workdir>/.hokma/extensions.json, versioned in the student's Git. It is rewritten
from the authoritative copy on every change and is never read to build settings. The whole
<workdir>/.hokma folder is protected from the agent (guard hook and sandbox denyWrite).

Only `enabledPlugins`, `extraKnownMarketplaces` and `mcpServers` are accepted. Every other key is ignored: the security
locks (permissions, sandbox, hooks) always win and cannot be switched off from here. Every marketplace
source is rebuilt from an allow-list of fields and validated (see `validate_source`).
"""
import json
import os
import re


from hokma.core.config import claude_config_dir, extensions_mirror_path, extensions_path, hokma_home
from hokma.i18n import t
from hokma.kit import safefs
from hokma.kit.guard import under_any
from hokma.kit.mcp_config import SERVER_NAME, validate_mcp
from hokma.kit.writable import agent_writable_roots

ALLOWED_KEYS = ("enabledPlugins", "extraKnownMarketplaces", "mcpServers")
PLUGIN_ID = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+$")
NAME = re.compile(r"^[A-Za-z0-9._-]+$")
GITHUB_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REF = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,99}$")
GIT_URL = re.compile(r"^(https://[^\s]+|git@[A-Za-z0-9_.-]+:[^\s]+)$")
HTTPS_URL = re.compile(r"^https://[^\s]+$")
SOURCE_KINDS = ("github", "git", "url", "directory", "file")
SHELL_CHARS = ("$", "%", "~", "`", "\n", "\r", "\0")
BUILTIN_MARKETPLACES = frozenset({"claude-plugins-official"})


def validate_source(source: object) -> dict:
    """Return a clean copy of a marketplace source or raise ValueError. Unknown fields are dropped.

    `directory` and `file` sources must be absolute and must resolve (realpath) OUTSIDE the working folder
    and the temp folders, otherwise the agent could write its own plugin there and have it loaded.
    [ASSUMED] Without the sandbox (native Windows) any folder the user can write is agent-writable too;
    there the only barrier is that the student adds the source by hand."""
    if not isinstance(source, dict) or source.get("source") not in SOURCE_KINDS:
        raise ValueError(t("ext.unknown_source"))
    kind = source["source"]

    def text(key: str, pattern: re.Pattern, required: bool = True) -> str | None:
        value = source.get(key)
        if value is None and not required:
            return None
        if not isinstance(value, str) or not pattern.match(value) or value.startswith("-"):
            raise ValueError(t("ext.bad_field", field=key))
        return value

    clean: dict = {"source": kind}
    if kind == "github":
        clean["repo"] = text("repo", GITHUB_REPO)
    elif kind == "git":
        clean["url"] = text("url", GIT_URL)
        if "ext::" in clean["url"]:
            raise ValueError(t("ext.git_transport"))
    elif kind == "url":
        clean["url"] = text("url", HTTPS_URL)
    else:
        path = source.get("path")
        if not isinstance(path, str) or not path or any(c in path for c in SHELL_CHARS) or not os.path.isabs(path):
            raise ValueError(t("ext.path_absolute"))
        if under_any(path, [str(r) for r in agent_writable_roots()]):
            raise ValueError(t("ext.path_writable"))
        clean["path"] = path
    if kind in ("github", "git"):
        ref = text("ref", REF, required=False)
        if ref is not None:
            clean["ref"] = ref
    return clean


def _empty() -> dict:
    return {"enabledPlugins": {}, "extraKnownMarketplaces": {}, "mcpServers": {}}


def load() -> tuple[dict, list[str]]:
    """Return (clean extensions from the AUTHORITATIVE copy, problems). Invalid entries are dropped."""
    path = extensions_path()
    clean, problems = _empty(), []
    if not path.is_file():
        return clean, problems
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("not an object")
    except (ValueError, OSError) as exc:
        return clean, [t("ext.unreadable", error=exc)]
    for key in raw:
        if key not in ALLOWED_KEYS:
            problems.append(t("ext.ignored_key", key=key))
    plugins = raw.get("enabledPlugins")
    for pid, enabled in (plugins.items() if isinstance(plugins, dict) else []):
        if PLUGIN_ID.match(str(pid)) and isinstance(enabled, bool):
            clean["enabledPlugins"][pid] = enabled
        else:
            problems.append(t("ext.ignored_plugin", plugin=repr(pid)))
    markets = raw.get("extraKnownMarketplaces")
    for name, entry in (markets.items() if isinstance(markets, dict) else []):
        try:
            if not NAME.match(str(name)) or not isinstance(entry, dict):
                raise ValueError(t("ext.invalid_name"))
            clean["extraKnownMarketplaces"][name] = {"source": validate_source(entry.get("source"))}
        except ValueError as exc:
            problems.append(t("ext.ignored_marketplace", name=repr(name), error=exc))
    servers = raw.get("mcpServers")
    for name, entry in (servers.items() if isinstance(servers, dict) else []):
        try:
            if not SERVER_NAME.match(str(name)):
                raise ValueError(t("ext.invalid_name"))
            clean["mcpServers"][name] = validate_mcp(entry)
        except ValueError as exc:
            problems.append(t("ext.ignored_server", name=repr(name), error=exc))
    return clean, problems


def _write_mirror(data: dict) -> str | None:
    """Informational copy in the workdir. Returns a warning instead of failing the change."""
    payload = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()
    try:
        safefs.write_atomic(hokma_home(), os.path.relpath(extensions_mirror_path(), hokma_home()), payload)
    except (safefs.UnsafePath, OSError) as exc:
        return t("ext.mirror_failed", error=exc)
    return None


def _save(data: dict) -> list[str]:
    path = extensions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    warning = _write_mirror(data)
    return [warning] if warning else []


def set_plugin(plugin_id: str, enabled: bool) -> list[str]:
    if not PLUGIN_ID.match(plugin_id):
        raise ValueError(t("ext.plugin_form"))
    data, _ = load()
    data["enabledPlugins"][plugin_id] = enabled
    return _save(data)


def remove_plugin(plugin_id: str) -> list[str]:
    if not PLUGIN_ID.match(plugin_id):
        raise ValueError(t("ext.plugin_form"))
    data, _ = load()
    data["enabledPlugins"].pop(plugin_id, None)
    warnings = _save(data)
    _drop_live_plugin(plugin_id)  # otherwise the next launch would absorb it back from the live settings
    return warnings


def add_marketplace(name: str, source: dict) -> list[str]:
    if not NAME.match(name):
        raise ValueError(t("ext.marketplace_name"))
    data, _ = load()
    data["extraKnownMarketplaces"][name] = {"source": validate_source(source)}
    return _save(data)


def mirror_differs() -> bool:
    """True when the informational copy in the workdir does not match the authoritative one."""
    data, _ = load()
    try:
        mirror = safefs.read_regular(hokma_home(), os.path.relpath(extensions_mirror_path(), hokma_home()))
        return mirror is None or json.loads(mirror) != data
    except (safefs.UnsafePath, ValueError, OSError):
        return True


def set_mcp(name: str, config: dict) -> list[str]:
    if not SERVER_NAME.match(name):
        raise ValueError(t("ext.server_name"))
    data, _ = load()
    data["mcpServers"][name] = validate_mcp(config)
    return _save(data)


def known_marketplaces() -> set[str]:
    """Marketplaces a plugin may come from: the student's own plus Claude's built-in official one."""
    data, _ = load()
    return set(data["extraKnownMarketplaces"]) | set(BUILTIN_MARKETPLACES)


# Plugins installed from inside Claude Code (`/plugin install`, `/plugin marketplace add`).
#
# Claude Code records them in the files of the isolated CLAUDE_CONFIG_DIR: `enabledPlugins` (and sometimes
# `extraKnownMarketplaces`) in settings.json, marketplaces in plugins/known_marketplaces.json. Hokma rebuilds
# settings.json at every launch, so without this step those plugins vanished on the next start or update.
# The live files are read ONLY for these two keys, every entry goes through the same validation as the
# dashboard (PLUGIN_ID, NAME, validate_source) and only ADDS entries the authoritative copy does not have:
# what the student set in Hokma always wins, and no lock (permissions, sandbox, hooks) is read from there.

def _live_settings_path():
    return claude_config_dir() / "settings.json"


def _live_marketplaces_path():
    return claude_config_dir() / "plugins" / "known_marketplaces.json"


def _read_object(path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return raw if isinstance(raw, dict) else {}


def live_additions(data: dict | None = None) -> tuple[dict, list[str]]:
    """Plugins and marketplaces present in Claude's live files and missing from `data` (default: the
    authoritative copy). Read-only. Returns ({"enabledPlugins": ..., "extraKnownMarketplaces": ...}, problems)."""
    if data is None:
        data, _ = load()
    added: dict = {"enabledPlugins": {}, "extraKnownMarketplaces": {}}
    problems: list[str] = []
    live = _read_object(_live_settings_path())
    for markets in (live.get("extraKnownMarketplaces"), _read_object(_live_marketplaces_path())):
        if not isinstance(markets, dict):
            continue
        for name, entry in markets.items():
            if name in BUILTIN_MARKETPLACES or name in data["extraKnownMarketplaces"] \
                    or name in added["extraKnownMarketplaces"]:
                continue
            try:
                if not NAME.match(str(name)) or not isinstance(entry, dict):
                    raise ValueError(t("ext.invalid_name"))
                added["extraKnownMarketplaces"][name] = {"source": validate_source(entry.get("source"))}
            except ValueError as exc:
                problems.append(t("ext.ignored_marketplace", name=repr(name), error=exc))
    plugins = live.get("enabledPlugins")
    for pid, enabled in (plugins.items() if isinstance(plugins, dict) else []):
        if pid in data["enabledPlugins"]:
            continue
        if PLUGIN_ID.match(str(pid)) and isinstance(enabled, bool):
            added["enabledPlugins"][pid] = enabled
        else:
            problems.append(t("ext.ignored_plugin", plugin=repr(pid)))
    return added, problems


def absorb_live() -> list[str]:
    """Persist `live_additions` into the authoritative copy (and its mirror) so the dashboard sees them and
    they survive the regeneration of settings.json. Never raises: a problem only becomes a warning."""
    try:
        data, _ = load()
        added, problems = live_additions(data)
        if not added["enabledPlugins"] and not added["extraKnownMarketplaces"]:
            return problems
        data["enabledPlugins"].update(added["enabledPlugins"])
        data["extraKnownMarketplaces"].update(added["extraKnownMarketplaces"])
        return problems + _save(data)
    except (OSError, ValueError) as exc:
        return [t("ext.unreadable", error=exc)]


def _drop_live_plugin(plugin_id: str) -> None:
    """Remove an uninstalled plugin from the live settings.json too. Best effort: settings.json is rebuilt at
    the next launch anyway; this only stops `absorb_live` from bringing the plugin back."""
    path = _live_settings_path()
    live = _read_object(path)
    plugins = live.get("enabledPlugins")
    if not isinstance(plugins, dict) or plugin_id not in plugins:
        return
    plugins.pop(plugin_id)
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(live, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass
