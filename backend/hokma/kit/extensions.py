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


from hokma.core.config import claude_config_dir, extensions_mirror_path, extensions_path, hokma_data_dir, hokma_home
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
OFFICIAL_REPO = "anthropics/claude-plugins-official"


class NameTaken(ValueError):
    """A marketplace name that already belongs to another repository (or to the official marketplace)."""


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


GITHUB_URL = re.compile(r"^https://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$", re.I)


def shims_dir():
    """Folder (protected data dir) where Hokma writes a one-plugin marketplace for a repo that is a plugin only."""
    return hokma_data_dir() / "plugin-shims"


def shim_repo(path: str) -> str | None:
    """owner/repo behind a shim directory source, or None when `path` is not one of Hokma's shims."""
    try:
        root = shims_dir().resolve()
        target = os.path.realpath(path)
        if os.path.dirname(target) != str(root):
            return None
        manifest = json.loads((root / os.path.basename(target) / ".claude-plugin" / "marketplace.json")
                              .read_text(encoding="utf-8"))
        source = manifest["plugins"][0]["source"]
        repo = source.get("repo") if isinstance(source, dict) else None
        return repo if isinstance(repo, str) and GITHUB_REPO.match(repo) else None
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None


def source_key(source: dict) -> str:
    """One key per repository: `github:owner/repo` in lowercase, whether it was written as owner/repo or as a
    GitHub link. Other kinds keep their own address. Two entries with the same key are the same marketplace."""
    kind = source.get("source")
    ref = f"@{source['ref']}" if source.get("ref") else ""      # another branch or tag is other content
    if kind == "github":
        return "github:" + str(source.get("repo", "")).lower() + ref
    if kind == "git":
        found = GITHUB_URL.match(str(source.get("url", "")))
        if found:
            return f"github:{found.group(1)}/{found.group(2)}".lower() + ref
    if kind == "directory":
        repo = shim_repo(str(source.get("path", "")))
        if repo:
            return "github:" + repo.lower()
    return f"{kind}:{source.get('url') or source.get('path') or ''}".rstrip("/").lower() + ref


def _keeper(names: list[str], source: dict, referenced: set[str]) -> str:
    """Which name of several entries for one repo stays: the one plugins already use (their ids end in it), then
    a name that is not just the repo's folder name (that one is Hokma's guess, the other is the marketplace's own
    name), then the shortest."""
    repo_name = str(source.get("repo", "")).split("/")[-1].lower()
    return sorted(names, key=lambda n: (n not in referenced, n.lower() == repo_name, len(n), n))[0]


def dedupe_marketplaces(data: dict) -> dict[str, str]:
    """One entry per repository, in place. Returns {dropped name: kept name}; plugin ids that pointed at a
    dropped name are moved to the kept one (and merged when both exist: the one turned on wins)."""
    referenced = {pid.split("@", 1)[1] for pid in data["enabledPlugins"]}
    groups: dict[str, list[str]] = {}
    for name, entry in data["extraKnownMarketplaces"].items():
        groups.setdefault(source_key(entry["source"]), []).append(name)
    moved: dict[str, str] = {}
    for names in groups.values():
        if len(names) < 2:
            continue
        keep = _keeper(names, data["extraKnownMarketplaces"][names[0]]["source"], referenced)
        for name in names:
            if name != keep:
                data["extraKnownMarketplaces"].pop(name)
                moved[name] = keep
    for pid in list(data["enabledPlugins"]):
        plugin, market = pid.split("@", 1)
        if market in moved:
            enabled = data["enabledPlugins"].pop(pid)
            target = f"{plugin}@{moved[market]}"
            data["enabledPlugins"][target] = enabled or data["enabledPlugins"].get(target, False)
    return moved


def _empty() -> dict:
    return {"enabledPlugins": {}, "extraKnownMarketplaces": {}, "mcpServers": {}}


def load() -> tuple[dict, list[str]]:
    """Return (clean extensions from the AUTHORITATIVE copy, problems). Invalid entries are dropped and a
    repository saved under two names is one entry (`dedupe_marketplaces`)."""
    clean, problems, _ = _load()
    return clean, problems


def _load() -> tuple[dict, list[str], dict[str, str]]:
    path = extensions_path()
    clean, problems = _empty(), []
    if not path.is_file():
        return clean, problems, {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("not an object")
    except (ValueError, OSError) as exc:
        return clean, [t("ext.unreadable", error=exc)], {}
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
    return clean, problems, dedupe_marketplaces(clean)


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


def find_marketplace(source: dict, data: dict | None = None) -> str | None:
    """Name of the entry that already holds this repository, or None."""
    if data is None:
        data, _ = load()
    key = source_key(source)
    for name, entry in data["extraKnownMarketplaces"].items():
        if source_key(entry["source"]) == key:
            return name
    return None


def add_marketplace(name: str, source: dict) -> list[str]:
    """Add a marketplace. A repository already in the list is not added again, whatever name it came with."""
    if not NAME.match(name):
        raise ValueError(t("ext.marketplace_name"))
    clean = validate_source(source)
    data, _ = load()
    if find_marketplace(clean, data) is None:
        taken = data["extraKnownMarketplaces"].get(name)
        official = {"source": "github", "repo": OFFICIAL_REPO}
        if (taken is not None and source_key(taken["source"]) != source_key(clean)) \
                or (name in BUILTIN_MARKETPLACES and source_key(clean) != source_key(official)):
            raise NameTaken(t("ext.marketplace_taken", name=name))   # a name never moves to another repository
        data["extraKnownMarketplaces"][name] = {"source": clean}
    return _save(data)


def rename_marketplace(old: str, new: str) -> list[str]:
    """Give a marketplace the name its own manifest declares (plugin ids are `plugin@name`); its plugins follow."""
    if not NAME.match(old) or not NAME.match(new):
        raise ValueError(t("ext.marketplace_name"))
    data, _ = load()
    if old == new or old not in data["extraKnownMarketplaces"] or new in data["extraKnownMarketplaces"] \
            or new in BUILTIN_MARKETPLACES:
        return []
    data["extraKnownMarketplaces"][new] = data["extraKnownMarketplaces"].pop(old)
    for pid in [p for p in data["enabledPlugins"] if p.endswith("@" + old)]:
        data["enabledPlugins"][pid.rsplit("@", 1)[0] + "@" + new] = data["enabledPlugins"].pop(pid)
    warnings = _save(data)
    _drop_live_marketplace(old)
    return warnings


def remove_marketplace(name: str) -> tuple[list[str], list[str]]:
    """Remove a marketplace and the plugins that came from it. Returns (plugin ids removed, warnings)."""
    if not NAME.match(name):
        raise ValueError(t("ext.marketplace_name"))
    data, _ = load()
    data["extraKnownMarketplaces"].pop(name, None)
    gone = sorted(pid for pid in data["enabledPlugins"] if pid.endswith("@" + name))
    for pid in gone:
        data["enabledPlugins"].pop(pid)
    warnings = _save(data)
    _drop_live_marketplace(name)  # otherwise the next launch would absorb it back from the live files
    return gone, warnings


def migrate() -> list[str]:
    """Write the cleaned list back when the saved file still has the same repository twice. No-op otherwise."""
    path = extensions_path()
    if not path.is_file():
        return []
    try:
        data, _, moved = _load()
        if moved:
            return _save(data)
    except (OSError, ValueError):
        pass
    return []


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


def remove_mcp(name: str) -> list[str]:
    if not SERVER_NAME.match(name):
        raise ValueError(t("ext.server_name"))
    data, _ = load()
    data["mcpServers"].pop(name, None)
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
                source = validate_source(entry.get("source"))
            except ValueError as exc:
                problems.append(t("ext.ignored_marketplace", name=repr(name), error=exc))
                continue
            added["extraKnownMarketplaces"][name] = {"source": source}
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
        dedupe_marketplaces(data)       # the live name of a repository wins when its plugins already use it
        return problems + _save(data)
    except (OSError, ValueError) as exc:
        return [t("ext.unreadable", error=exc)]


def _write_object(path, data: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _drop_live_marketplace(name: str) -> None:
    """Forget a removed marketplace (and its plugins) in Claude's live files. Best effort, like `_drop_live_plugin`."""
    path = _live_settings_path()
    live = _read_object(path)
    changed = False
    markets = live.get("extraKnownMarketplaces")
    if isinstance(markets, dict) and markets.pop(name, None) is not None:
        changed = True
    plugins = live.get("enabledPlugins")
    if isinstance(plugins, dict):
        for pid in [p for p in plugins if str(p).endswith("@" + name)]:
            plugins.pop(pid)
            changed = True
    if changed:
        _write_object(path, live)
    path = _live_marketplaces_path()
    known = _read_object(path)
    if known.pop(name, None) is not None:
        _write_object(path, known)


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
