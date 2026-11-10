"""What a GitHub repository is: a marketplace (it lists plugins) or a plugin on its own.

Read from the repository's own manifests, `.claude-plugin/marketplace.json` and `.claude-plugin/plugin.json`, over
raw.githubusercontent.com. The backend makes the requests (timeout, size limit); nothing is cloned or run. For a
repository that is only a plugin, Hokma writes a one-plugin marketplace in its protected data folder (a "shim")
that points at the repo, because the engine installs plugins from marketplaces only. `fetch_json` is what tests replace."""
import hashlib
import json
import urllib.error
import urllib.request

from hokma.i18n import t
from hokma.kit import extensions
from hokma.status import catalog

RAW = "https://raw.githubusercontent.com/{repo}/HEAD/.claude-plugin/{file}"
TIMEOUT_SECONDS = 20
MAX_BYTES = 2 * 1024 * 1024


class SourceError(Exception):
    """The repository could not be read as a marketplace or a plugin. `status` is the HTTP code the route answers."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def fetch_json(url: str) -> object | None:
    """The JSON at `url`, or None when the file does not exist (404). Raises SourceError for anything else."""
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "hokma"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            data = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise SourceError(502, t("status.source.unreachable")) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise SourceError(502, t("status.source.unreachable")) from exc
    if len(data) > MAX_BYTES:
        raise SourceError(422, t("status.source.invalid"))
    try:
        return json.loads(data)
    except ValueError as exc:
        raise SourceError(422, t("status.source.invalid")) from exc


def _name(raw: object, fallback: str) -> str:
    return raw if isinstance(raw, str) and extensions.NAME.match(raw) else fallback


def inspect(repo: str) -> dict:
    """{"kind": "marketplace", "name", "plugins": [...]} or {"kind": "plugin", "name", "description"}.

    `name` of a marketplace is the one its manifest declares: plugin ids are `plugin@name`, so the engine and Hokma
    must use the same one. The plugins have the shape of `catalog.parse`."""
    fallback = repo.split("/", 1)[1]
    manifest = fetch_json(RAW.format(repo=repo, file="marketplace.json"))
    if manifest is not None:
        try:
            plugins = catalog.parse(manifest)
        except catalog.CatalogError as exc:
            raise SourceError(422, str(exc)) from None
        return {"kind": "marketplace", "name": _name(manifest.get("name"), fallback), "plugins": plugins}
    plugin = fetch_json(RAW.format(repo=repo, file="plugin.json"))
    if isinstance(plugin, dict):
        name = _name(plugin.get("name"), fallback)
        return {"kind": "plugin", "name": name, "description": " ".join(str(plugin.get("description", "")).split())[:400]}
    raise SourceError(422, t("status.source.neither", repo=repo))


def write_shim(repo: str, name: str) -> str:
    """Create the one-plugin marketplace for a repository that is only a plugin; returns its folder. The marketplace
    carries the plugin's own name, so the plugin id is `name@name`."""
    root = extensions.shims_dir()
    folder = f"{name[:60]}-{hashlib.sha256(repo.lower().encode()).hexdigest()[:16]}"   # one folder per repository, no collisions
    manifest = {"name": name, "owner": {"name": repo.split("/", 1)[0]},
                "plugins": [{"name": name, "source": {"source": "github", "repo": repo}}]}
    target = root / folder / ".claude-plugin" / "marketplace.json"
    try:                                  # the data folder is protected from the agent, so a plain write is enough
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        raise SourceError(500, t("ext.mirror_failed", error=exc)) from exc
    return str(root / folder)
