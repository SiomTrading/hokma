"""The Anthropic marketplace: its real plugin list, read from its official GitHub repository.

`REPO` is also what "Add" registers with `extensions.add_marketplace`. The list comes from the repository's
`.claude-plugin/marketplace.json`; nothing is invented. The backend makes the request (the panel only talks to
the local server), with a timeout, a size limit and a short cache."""
import json
import time
import unicodedata
import urllib.error
import urllib.request

from hokma.i18n import t
from hokma.kit import extensions

REPO = "anthropics/claude-plugins-official"
MARKETPLACE_NAME = "claude-plugins-official"
URL = f"https://raw.githubusercontent.com/{REPO}/main/.claude-plugin/marketplace.json"
TIMEOUT_SECONDS = 20
MAX_BYTES = 4 * 1024 * 1024
CACHE_SECONDS = 600
MAX_TEXT = 400

_cache: tuple[float, list[dict]] | None = None


class CatalogError(Exception):
    """The list could not be read. The message is already localized."""


def clear_cache() -> None:
    global _cache
    _cache = None


def _text(value: object, limit: int = MAX_TEXT) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def parse(raw: object) -> list[dict]:
    """Plugins of a marketplace.json as [{name, display_name, description, category}], sorted by name."""
    plugins = raw.get("plugins") if isinstance(raw, dict) else None
    if not isinstance(plugins, list):
        raise CatalogError(t("status.market.invalid"))
    items = []
    for entry in plugins:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not extensions.NAME.match(name):
            continue
        items.append({"name": name, "display_name": _text(entry.get("displayName"), 80) or name,
                      "description": _text(entry.get("description")), "category": _text(entry.get("category"), 40)})
    return sorted(items, key=lambda i: i["name"])


def fetch() -> list[dict]:
    """The list from GitHub. Tests replace this function."""
    request = urllib.request.Request(URL, headers={"Accept": "application/json", "User-Agent": "hokma"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            data = response.read(MAX_BYTES + 1)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise CatalogError(t("status.market.unreachable")) from exc
    if len(data) > MAX_BYTES:
        raise CatalogError(t("status.market.invalid"))
    try:
        return parse(json.loads(data))
    except ValueError as exc:
        raise CatalogError(t("status.market.invalid")) from exc


def _cached(refresh: bool) -> list[dict]:
    global _cache
    if _cache and not refresh and time.monotonic() - _cache[0] < CACHE_SECONDS:
        return _cache[1]
    items = fetch()
    _cache = (time.monotonic(), items)
    return items


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text.lower()) if not unicodedata.combining(c))


def listing(query: str, limit: int, refresh: bool) -> dict:
    """`added` says whether the marketplace is in the owner's list; each item says whether its plugin is in the
    extensions file and on."""
    from hokma.status.extensions_service import ServiceError

    try:
        items = _cached(refresh)
    except CatalogError as exc:
        raise ServiceError(502, str(exc)) from None
    data, _ = extensions.load()
    words = _fold(query).split()
    matched = [i for i in items if all(w in _fold(f"{i['name']} {i['display_name']} {i['description']} {i['category']}")
                                       for w in words)]
    shown = []
    for item in matched[:limit]:
        plugin_id = f"{item['name']}@{MARKETPLACE_NAME}"
        shown.append({**item, "id": plugin_id, "installed": plugin_id in data["enabledPlugins"],
                      "enabled": data["enabledPlugins"].get(plugin_id, False)})
    return {"marketplace": MARKETPLACE_NAME, "repo": REPO, "added": MARKETPLACE_NAME in data["extraKnownMarketplaces"],
            "total": len(matched), "items": shown}
