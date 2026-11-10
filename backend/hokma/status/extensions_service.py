"""Extensions as the dashboard sees them. The file `<data>/extensions.json` stays the source of truth for what is
active (hokma.kit.extensions); the database keeps the owner's decisions (`extension_requests` and `approvals`).

Every change made here is a human act: the route needs the session token, and turning a plugin on or off or
approving an agent's request writes an approval with channel `dashboard`."""
import re

from hokma.db import Actor, Database, NotFound, RuleViolation, get_database
from hokma.i18n import t
from hokma.kit import extensions, plugin_state, requests
from hokma.status import catalog, plugin_sources

LOCAL_MARKETPLACE = "local"
DB_REQUEST = re.compile(r"^db-(\d{1,9})$")


class ServiceError(Exception):
    """Carries a localized message and the HTTP status the route should answer."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def database_ready() -> bool:
    from hokma.db import current_version
    try:
        return current_version() > 0
    except Exception:
        return False


def _db() -> Database:
    if not database_ready():
        raise ServiceError(503, t("status.api.no_database"))
    return get_database()


def _owner(unit) -> str:
    return unit.profile.get_or_default().owner_name or "owner"


def _split(plugin_id: str) -> tuple[str, str]:
    name, marketplace = plugin_id.split("@", 1)
    return name, marketplace


def sync_table(unit, data: dict) -> None:
    """Make the `extensions` table match the file: one row per plugin and MCP server, off when it left the file."""
    present: set[tuple[str, str]] = set()
    for plugin_id, enabled in data["enabledPlugins"].items():
        name, marketplace = _split(plugin_id)
        unit.extensions.add(name, marketplace, "plugin", enabled=enabled)
        present.add((marketplace, name))
    for name in data["mcpServers"]:
        unit.extensions.add(name, LOCAL_MARKETPLACE, "mcp", enabled=True)
        present.add((LOCAL_MARKETPLACE, name))
    for row in unit.extensions.list(enabled=True):
        if (row.marketplace, row.name) not in present:
            unit.extensions.set_enabled(row.id, False)


def _describe_source(source: dict) -> str:
    kind = source.get("source")
    if kind == "github":
        return f"github:{source.get('repo')}"
    if kind == "directory" and extensions.shim_repo(str(source.get("path"))):
        return f"github:{extensions.shim_repo(str(source.get('path')))}"
    return str(source.get("url") or source.get("path") or kind)


def _server_target(entry: dict) -> str:
    return str(entry.get("url") if entry.get("type", "stdio") != "stdio" else entry.get("command", ""))


def _db_requests() -> list[dict]:
    if not database_ready():
        return []
    with get_database().read() as unit:
        rows = unit.extension_requests.list(status="pending")
    return [{
        "id": f"db-{r.id}", "origin": "db", "kind": r.kind,
        "name": f"{r.name}@{r.marketplace}" if r.kind == "plugin" else r.name,
        "requested_at": r.requested_at, "reason": r.reason, "risks": [],
        "summary": t("status.ext.db_summary", action=r.action, kind=r.kind, name=r.name, marketplace=r.marketplace),
    } for r in rows]


def listing() -> dict:
    extensions.migrate()                 # a repository saved under two names becomes one entry
    data, problems = extensions.load()
    if database_ready():
        db = get_database()
        with db.write() as unit:
            sync_table(unit, data)
    plugins = []
    installs = plugin_state.engine_installs()
    for plugin_id, enabled in sorted(data["enabledPlugins"].items()):
        name, marketplace = _split(plugin_id)
        summary = requests.Request("", "plugin", plugin_id, None, "").summary()
        plugins.append({"id": plugin_id, "name": name, "marketplace": marketplace, "kind": "plugin",
                        "enabled": enabled, "summary": summary,
                        **plugin_state.state_of(plugin_id, enabled, data, installs)})
    servers = [{"id": f"mcp:{name}", "name": name, "kind": "mcp", "enabled": True, "target": _server_target(entry),
                "remote": entry.get("type") == "http"}
               for name, entry in sorted(data["mcpServers"].items())]
    marketplaces = [{"name": name, "source": _describe_source(entry["source"]),
                     "repo": extensions.source_key(entry["source"]).removeprefix("github:")
                     if extensions.source_key(entry["source"]).startswith("github:") else None,
                     "plugins": sum(1 for pid in data["enabledPlugins"] if pid.endswith("@" + name))}
                    for name, entry in sorted(data["extraKnownMarketplaces"].items())]
    inbox = [{"id": r.id, "origin": "inbox", "kind": r.kind, "name": r.name, "requested_at": r.requested_at,
              "reason": None, "risks": r.risks, "summary": r.summary()} for r in requests.list_pending()]
    return {"plugins": plugins, "servers": servers, "marketplaces": marketplaces, "requests": inbox + _db_requests(),
            "problems": problems, "database": database_ready()}


def toggle_plugin(plugin_id: str, enabled: bool) -> dict:
    data, _ = extensions.load()
    name, marketplace = _split(plugin_id)
    installed = plugin_id in data["enabledPlugins"]
    if not installed and marketplace not in extensions.known_marketplaces():
        raise ServiceError(404, t("status.ext.unknown_plugin", plugin=plugin_id))
    db = _db()
    with db.write() as unit:
        owner = _owner(unit)
        sync_table(unit, data)
        row = unit.extensions.find(marketplace, name) or unit.extensions.add(name, marketplace, "plugin")
        action = ("enable" if enabled else "disable") if installed else "install"
        request = unit.extension_requests.create(
            action, "plugin", name, marketplace, f"dashboard: {action} {plugin_id}", actor=Actor.human(owner))
        unit.extension_requests.approve(request.id, decided_by=owner)
        try:
            warnings = extensions.set_plugin(plugin_id, enabled)
        except ValueError as exc:
            raise ServiceError(400, str(exc)) from exc
        unit.extensions.set_enabled(row.id, enabled)
        unit.extension_requests.mark_applied(request.id, row.id)
    return {"id": plugin_id, "enabled": enabled, "warnings": warnings}


def uninstall_plugin(plugin_id: str) -> dict:
    data, _ = extensions.load()
    name, marketplace = _split(plugin_id)
    if plugin_id not in data["enabledPlugins"]:
        raise ServiceError(404, t("status.ext.unknown_plugin", plugin=plugin_id))
    db = _db()
    with db.write() as unit:
        owner = _owner(unit)
        sync_table(unit, data)
        row = unit.extensions.find(marketplace, name) or unit.extensions.add(name, marketplace, "plugin")
        request = unit.extension_requests.create(
            "uninstall", "plugin", name, marketplace, f"dashboard: uninstall {plugin_id}", actor=Actor.human(owner))
        unit.extension_requests.approve(request.id, decided_by=owner)
        try:
            warnings = extensions.remove_plugin(plugin_id)
        except ValueError as exc:
            raise ServiceError(400, str(exc)) from exc
        unit.extensions.set_enabled(row.id, False)
        unit.extension_requests.mark_applied(request.id, row.id)
    return {"id": plugin_id, "uninstalled": True, "warnings": warnings}


def _market_plugins(marketplace: str, found: list[dict], data: dict) -> list[dict]:
    return [{**item, "id": f"{item['name']}@{marketplace}", "installed": f"{item['name']}@{marketplace}" in data["enabledPlugins"],
             "enabled": data["enabledPlugins"].get(f"{item['name']}@{marketplace}", False)} for item in found]


def _source_error(exc: plugin_sources.SourceError):
    return ServiceError(exc.status, exc.message)


def add_marketplace(repo: str) -> dict:
    """Add a GitHub repository by its address. A repository that is a marketplace is registered once (the same
    repository under another name is the same entry) and, when it has one plugin only, that plugin is installed
    right away; with several, they come back to be installed one by one. A repository that is a plugin on its own
    is installed. Needs the database like any install."""
    try:
        found = plugin_sources.inspect(repo)
    except plugin_sources.SourceError as exc:
        raise _source_error(exc) from None
    source: dict = {"source": "github", "repo": repo}
    if found["kind"] == "plugin":
        shim = plugin_sources.write_shim(repo, found["name"])
        source = {"source": "directory", "path": shim}
    install_now = found["kind"] == "plugin" or len(found["plugins"]) == 1
    if install_now:
        _db()                                        # fail before changing anything
    existing = extensions.find_marketplace(source)
    name = existing or found["name"]
    try:
        warnings = list(extensions.add_marketplace(name, source))
        if existing and existing != found["name"] and found["kind"] == "marketplace":
            renamed = extensions.rename_marketplace(existing, found["name"])
            if extensions.find_marketplace(source) == found["name"]:
                warnings += renamed
                name = found["name"]
    except extensions.NameTaken as exc:
        raise ServiceError(409, str(exc)) from exc
    except ValueError as exc:
        raise ServiceError(400, str(exc)) from exc
    names = [found["name"]] if found["kind"] == "plugin" else [p["name"] for p in found["plugins"]]
    installed: list[str] = []
    if install_now:
        plugin_id = f"{names[0]}@{name}"
        warnings += toggle_plugin(plugin_id, True)["warnings"]
        installed.append(plugin_id)
    data, _ = extensions.load()
    plugins = _market_plugins(name, found["plugins"], data) if found["kind"] == "marketplace" else []
    return {"name": name, "source": f"github:{repo}", "kind": found["kind"], "installed": installed,
            "plugins": plugins, "warnings": warnings}


def add_official() -> dict:
    """Register Anthropic's marketplace without reading it: the panel already lists it from `catalog`."""
    source = {"source": "github", "repo": catalog.REPO}
    name = extensions.find_marketplace(source) or catalog.MARKETPLACE_NAME
    try:
        warnings = extensions.add_marketplace(name, source)
    except ValueError as exc:
        raise ServiceError(400, str(exc)) from exc
    return {"name": name, "source": f"github:{catalog.REPO}", "warnings": warnings}


def marketplace_plugins(name: str) -> dict:
    """The plugins a registered marketplace offers, each with whether it is installed and on."""
    data, _ = extensions.load()
    entry = data["extraKnownMarketplaces"].get(name)
    if entry is None and name in extensions.BUILTIN_MARKETPLACES:
        entry = {"source": {"source": "github", "repo": catalog.REPO}}
    if entry is None:
        raise ServiceError(404, t("status.ext.unknown_marketplace", name=name))
    source = entry["source"]
    repo = source.get("repo") or extensions.source_key(source).removeprefix("github:")
    if not extensions.GITHUB_REPO.match(repo or ""):
        raise ServiceError(400, t("status.ext.source_unreadable", name=name))
    try:
        found = plugin_sources.inspect(repo)
    except plugin_sources.SourceError as exc:
        raise _source_error(exc) from None
    if found["kind"] == "plugin":
        found = {"plugins": [{"name": found["name"], "display_name": found["name"],
                              "description": found["description"], "category": ""}]}
    return {"name": name, "plugins": _market_plugins(name, found["plugins"], data)}


def remove_marketplace(name: str) -> dict:
    """Take a marketplace out of the list together with the plugins installed from it."""
    data, _ = extensions.load()
    if name not in data["extraKnownMarketplaces"]:
        raise ServiceError(404, t("status.ext.unknown_marketplace", name=name))
    removed = sorted(pid for pid in data["enabledPlugins"] if pid.endswith("@" + name))
    db = _db() if removed else None
    if db is None:
        gone, warnings = _remove_market(name)
    else:
        with db.write() as unit:
            owner = _owner(unit)
            sync_table(unit, data)
            requests_made = []
            for pid in removed:
                plugin, market = _split(pid)
                row = unit.extensions.find(market, plugin) or unit.extensions.add(plugin, market, "plugin")
                req = unit.extension_requests.create("uninstall", "plugin", plugin, market,
                                                     f"dashboard: remove marketplace {name}", actor=Actor.human(owner))
                unit.extension_requests.approve(req.id, decided_by=owner)
                requests_made.append((req.id, row.id))
            gone, warnings = _remove_market(name)
            for req_id, row_id in requests_made:
                unit.extensions.set_enabled(row_id, False)
                unit.extension_requests.mark_applied(req_id, row_id)
    return {"name": name, "removed": True, "plugins_removed": gone, "warnings": warnings}


def _remove_market(name: str) -> tuple[list[str], list[str]]:
    try:
        return extensions.remove_marketplace(name)
    except ValueError as exc:
        raise ServiceError(400, str(exc)) from exc


def _request_target(req: requests.Request) -> tuple[str, str]:
    return _split(req.name) if req.kind == "plugin" else (req.name, LOCAL_MARKETPLACE)


def approve(request_id: str) -> dict:
    match = DB_REQUEST.match(request_id)
    return _approve_db(int(match.group(1))) if match else _approve_inbox(request_id)


def reject(request_id: str) -> dict:
    match = DB_REQUEST.match(request_id)
    return _reject_db(int(match.group(1))) if match else _reject_inbox(request_id)


def _approve_inbox(request_id: str) -> dict:
    try:
        req = requests.get(request_id)
    except requests.RequestError as exc:
        raise ServiceError(404, str(exc)) from exc
    name, marketplace = _request_target(req)
    db = _db()
    with db.write() as unit:
        owner = _owner(unit)
        row = unit.extension_requests.create("install", req.kind, name, marketplace, f"inbox request {req.id}",
                                             actor=Actor.agent("agent"))
        unit.extension_requests.approve(row.id, decided_by=owner)
        try:
            warnings = requests.approve_request(request_id, lambda _: True)
        except requests.RequestError as exc:
            raise ServiceError(400, str(exc)) from exc
        data, _ = extensions.load()
        sync_table(unit, data)
        ext = unit.extensions.find(marketplace, name)
        unit.extension_requests.mark_applied(row.id, ext.id)
    return {"id": request_id, "status": "applied", "warnings": warnings}


def _reject_inbox(request_id: str) -> dict:
    try:
        req = requests.get(request_id)
    except requests.RequestError as exc:
        raise ServiceError(404, str(exc)) from exc
    name, marketplace = _request_target(req)
    db = _db()
    with db.write() as unit:
        owner = _owner(unit)
        row = unit.extension_requests.create("install", req.kind, name, marketplace, f"inbox request {req.id}",
                                             actor=Actor.agent("agent"))
        unit.extension_requests.reject(row.id, decided_by=owner)
        try:
            requests.reject_request(request_id)
        except requests.RequestError as exc:
            raise ServiceError(400, str(exc)) from exc
    return {"id": request_id, "status": "rejected"}


def _approve_db(number: int) -> dict:
    db = _db()
    error: str | None = None
    with db.write() as unit:
        try:
            row = unit.extension_requests.get(number)
        except NotFound as exc:
            raise ServiceError(404, t("status.ext.unknown_request")) from exc
        if row.status != "pending":
            raise ServiceError(409, t("status.ext.not_pending"))
        owner = _owner(unit)
        unit.extension_requests.approve(number, decided_by=owner)
        data, _ = extensions.load()
        if row.kind != "plugin":
            error = t("status.ext.db_cannot_apply")
        elif row.marketplace not in extensions.known_marketplaces():
            error = t("req.no_marketplace", marketplace=row.marketplace)
        else:
            plugin_id = f"{row.name}@{row.marketplace}"
            try:
                extensions.set_plugin(plugin_id, row.action not in ("disable", "uninstall"))
            except ValueError as exc:
                error = str(exc)
        if error is None:
            data, _ = extensions.load()
            sync_table(unit, data)
            ext = unit.extensions.find(row.marketplace, row.name)
            unit.extension_requests.mark_applied(number, ext.id)
        else:
            unit.extension_requests.mark_failed(number, error)
    if error:
        raise ServiceError(400, error)
    return {"id": f"db-{number}", "status": "applied", "warnings": []}


def _reject_db(number: int) -> dict:
    db = _db()
    with db.write() as unit:
        try:
            unit.extension_requests.reject(number, decided_by=_owner(unit))
        except NotFound as exc:
            raise ServiceError(404, t("status.ext.unknown_request")) from exc
        except RuleViolation as exc:
            raise ServiceError(409, t("status.ext.not_pending")) from exc
    return {"id": f"db-{number}", "status": "rejected"}
