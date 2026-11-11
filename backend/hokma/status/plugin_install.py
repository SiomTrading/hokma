"""The real installation of a plugin, done by Claude Code itself (task 3161).

Writing `enabledPlugins` and `extraKnownMarketplaces` is not an installation: Claude Code never downloads a
marketplace or a plugin on its own in non-interactive mode. So installing here means running, against Hokma's own
CLAUDE_CONFIG_DIR and with the filtered environment every engine gets:

    claude plugin marketplace add --json <source>      (only when that marketplace is not known yet)
    claude plugin install --json <plugin>@<marketplace>
    claude plugin uninstall --json <plugin>@<marketplace>

No shell, every argument validated, a timeout, the output in Hokma's log (`logs/engine.log`). Afterwards the result is
confirmed in `plugins/installed_plugins.json`; if it is not there the install failed, and the real reason is stored
so the card can show it with a Retry button. One operation at a time: two `claude plugin` processes would write the
same files.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from hokma.core.config import claude_config_dir, hokma_data_dir
from hokma.core.proc import spawn_kwargs
from hokma.engines.envfilter import filtered_env
from hokma.i18n import t
from hokma.kit import extensions, plugin_state

TIMEOUT_SECONDS = 300
REASON_CHARS = 400
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_lock = threading.Lock()                 # one `claude plugin` process at a time
_busy: set[str] = set()                  # plugin ids being installed right now (this process)
_state_lock = threading.Lock()


class InstallError(Exception):
    """The installation failed; the message is the real reason, already localized or taken from Claude Code."""


# ---------------------------------------------------------------- what runs

def claude_path() -> str | None:
    from hokma.setup.claude import claude_path as found

    return found()


def _env() -> dict[str, str]:
    env = filtered_env()
    env["CLAUDE_CONFIG_DIR"] = str(claude_config_dir())
    return env


def log(message: str) -> None:
    from hokma.core import supervisor

    supervisor.EngineLog(hokma_data_dir() / "logs" / "engine.log").write("plugins: " + message)


def run_claude(argv: list[str], timeout: float) -> tuple[int, str]:
    """Run `claude <argv>`: no shell, no stdin, never a console window. Returns (exit code, stdout + stderr).
    `runner` points here; tests replace `runner`."""
    path = claude_path()
    if path is None:
        raise InstallError(t("status.ext.install.no_claude"))
    claude_config_dir().mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run([path, *argv], capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, stdin=subprocess.DEVNULL, env=_env(), cwd=str(hokma_data_dir()),
                              **spawn_kwargs())
    except subprocess.TimeoutExpired:
        raise InstallError(t("status.ext.install.timeout", seconds=int(timeout))) from None
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError(t("status.ext.install.not_run", error=getattr(exc, "strerror", None) or str(exc))) from exc
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


runner = run_claude


# ---------------------------------------------------------------- validation

def _plain(value: str) -> bool:
    return isinstance(value, str) and bool(value) and not value.startswith("-") and not any(c in value for c in "\r\n\0")


def check_plugin_id(plugin_id: str) -> None:
    if not extensions.PLUGIN_ID.match(plugin_id or "") or not _plain(plugin_id):
        raise InstallError(t("ext.plugin_form"))


def source_argument(source: dict) -> str:
    """What `claude plugin marketplace add` receives for a saved source. Anything that is not a plain GitHub
    repository, an https/ssh git address or Hokma's own shim folder is refused."""
    kind = source.get("source")
    if kind == "github" and extensions.GITHUB_REPO.match(str(source.get("repo", ""))) and _plain(source["repo"]):
        return source["repo"]
    if kind in ("git", "url"):
        url = str(source.get("url", ""))
        if _plain(url) and (extensions.GIT_URL.match(url) or extensions.HTTPS_URL.match(url)):
            return url
    if kind == "directory":
        path = Path(str(source.get("path", "")))
        shims = extensions.shims_dir().resolve()
        try:
            if path.resolve().is_relative_to(shims) and path.is_dir():
                return str(path)
        except OSError:
            pass
    raise InstallError(t("status.ext.install.bad_source"))


# ---------------------------------------------------------------- outcome

def _reason(output: str) -> str:
    """The real reason out of `--json` output (its last line) or, failing that, the last words printed."""
    text = _ANSI.sub("", output or "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for line in reversed(lines):
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except ValueError:
                break
            if isinstance(data, dict) and data.get("message"):
                return " ".join(str(data["message"]).split())[-REASON_CHARS:]
            break
    return " ".join(" ".join(lines[-3:]).split())[-REASON_CHARS:] or "-"


def _run(argv: list[str], timeout: float = TIMEOUT_SECONDS) -> tuple[int, str]:
    log("claude " + " ".join(argv))
    try:
        code, output = runner(argv, timeout)
    except InstallError as exc:
        log(f"failed: {exc}")
        raise
    log(f"exit {code}: {_reason(output)}")
    return code, output


def known_marketplaces() -> set[str]:
    raw = plugin_state._read(claude_config_dir() / "plugins" / "known_marketplaces.json")
    return {str(k) for k in raw} if isinstance(raw, dict) else set()


def _source_for(market: str, data: dict) -> dict:
    entry = data["extraKnownMarketplaces"].get(market)
    if isinstance(entry, dict) and isinstance(entry.get("source"), dict):
        return entry["source"]
    if market in extensions.BUILTIN_MARKETPLACES:
        return {"source": "github", "repo": extensions.OFFICIAL_REPO}
    raise InstallError(t("status.ext.state.no_marketplace", marketplace=market))


def installed_folder(plugin_id: str) -> Path | None:
    folder = plugin_state.engine_installs().get(plugin_id)
    return folder if folder is not None and folder.is_dir() else None


# ---------------------------------------------------------------- failures and migration, kept in the data folder

def _state_path() -> Path:
    return hokma_data_dir() / "plugin-installs.json"


def _read_state() -> dict:
    raw = plugin_state._read(_state_path())
    state = raw if isinstance(raw, dict) else {}
    if not isinstance(state.get("failed"), dict):
        state["failed"] = {}
    return state


def _write_state(state: dict) -> None:
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass                                  # the card then loses the stored reason, nothing else


def failures() -> dict[str, str]:
    with _state_lock:
        return {str(k): str(v) for k, v in _read_state()["failed"].items()}


def _set_failure(plugin_id: str, reason: str | None) -> None:
    with _state_lock:
        state = _read_state()
        if reason is None:
            state["failed"].pop(plugin_id, None)
        else:
            state["failed"][plugin_id] = reason
        _write_state(state)


def installing() -> set[str]:
    return set(_busy)


# ---------------------------------------------------------------- the operations

def install(plugin_id: str) -> None:
    """Really install `plugin_id` with Claude Code and confirm it in installed_plugins.json. Raises InstallError with
    the real reason (also stored, so the card shows Error and Retry). Blocks until done or timed out."""
    check_plugin_id(plugin_id)
    _busy.add(plugin_id)
    try:
        with _lock:
            try:
                _install(plugin_id)
            except InstallError as exc:
                _set_failure(plugin_id, str(exc))
                raise
            _set_failure(plugin_id, None)
    finally:
        _busy.discard(plugin_id)


def _install(plugin_id: str) -> None:
    name, market = plugin_id.split("@", 1)
    data, _ = extensions.load()
    was_known = market in known_marketplaces()
    if not was_known:
        code, output = _run(["plugin", "marketplace", "add", "--json", source_argument(_source_for(market, data))])
        if code != 0:
            raise InstallError(_reason(output))
    code, output = _run(["plugin", "install", "--json", plugin_id])
    if code != 0 and was_known and '"not_found"' in output:        # an old local copy of the catalog: refresh once
        _run(["plugin", "marketplace", "update", market])
        code, output = _run(["plugin", "install", "--json", plugin_id])
    if code != 0:
        raise InstallError(_reason(output))
    if installed_folder(plugin_id) is None:
        raise InstallError(t("status.ext.install.not_recorded", plugin=plugin_id))


def uninstall(plugin_id: str) -> list[str]:
    """`claude plugin uninstall` for a plugin the engine has. Returns warnings; never raises (Hokma's own list is
    updated by the caller either way)."""
    try:
        check_plugin_id(plugin_id)
        if plugin_id not in plugin_state.engine_installs():
            _set_failure(plugin_id, None)
            return []
        with _lock:
            code, output = _run(["plugin", "uninstall", "--json", plugin_id])
    except InstallError as exc:
        return [str(exc)]
    if code != 0:
        return [t("status.ext.install.uninstall_failed", plugin=plugin_id, reason=_reason(output))]
    _set_failure(plugin_id, None)
    return []


def forget_marketplace(name: str) -> None:
    """Best effort: take a removed marketplace out of Claude Code's own list too."""
    if name not in known_marketplaces() or not _plain(name) or not extensions.NAME.match(name):
        return
    try:
        with _lock:
            _run(["plugin", "marketplace", "remove", name])
    except InstallError:
        pass


# ---------------------------------------------------------------- migration (0.0.20)

MIGRATION = "0.0.20"


def owner_approved() -> set[str] | None:
    """Plugin ids with an applied install or enable request: the owner (or an agent request the owner approved) asked
    for them. None when the database is not ready. An entry that only appeared in Claude's live settings.json
    (`absorb_live`) has no such request and is never downloaded here."""
    from hokma.db import current_version, get_database

    try:
        if current_version() <= 0:
            return None
        with get_database().read() as unit:
            rows = unit.extension_requests.list(status="applied", limit=100000)
    except Exception:
        return None
    return {f"{r.name}@{r.marketplace}" for r in rows if r.kind == "plugin" and r.action in ("install", "enable")}


def pending_migration() -> list[str]:
    """Plugins that are on in the saved list, that the owner approved, and that have no files on disk, unless the
    migration already ran."""
    if _read_state().get("migrated") == MIGRATION:
        return []
    approved = owner_approved()
    if approved is None:
        return []
    data, _ = extensions.load()
    return sorted(pid for pid, on in data["enabledPlugins"].items()
                  if on and pid in approved and installed_folder(pid) is None)


def migrate() -> dict[str, str]:
    """Once: really install what 0.0.19 only wrote to enabledPlugins. {plugin id: "ok" or the reason}. The run is
    logged and recorded; a plugin that fails stays with its Error and Retry button."""
    if owner_approved() is None:
        return {}                                    # no database yet: the next start tries again
    todo = pending_migration()
    outcome: dict[str, str] = {}
    log(f"migration {MIGRATION}: {len(todo)} plugin(s) without files: {', '.join(todo) or '-'}")
    for plugin_id in todo:
        try:
            install(plugin_id)
            outcome[plugin_id] = "ok"
        except InstallError as exc:
            outcome[plugin_id] = str(exc)
    with _state_lock:
        state = _read_state()
        state["migrated"] = MIGRATION
        state["migrated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_state(state)
    return outcome


def start_migration() -> None:
    """At the start of the engine, off the request path (a download can take minutes)."""
    def run() -> None:
        try:
            migrate()
        except Exception as exc:                  # best effort; the cards show the state and Retry
            try:
                log(f"migration stopped: {exc}")
            except Exception:
                pass

    threading.Thread(target=run, daemon=True, name="hokma-plugin-migration").start()


# ---------------------------------------------------------------- plugins that need more than their files

def _home_setting(path: str, key: str, default: str) -> str:
    raw = plugin_state._read(Path(os.path.expanduser("~")) / path)
    value = raw.get(key) if isinstance(raw, dict) else None
    return str(value) if isinstance(value, (str, int)) and str(value).isdigit() else default


def worker_port() -> int:
    return int(os.environ.get("CLAUDE_MEM_WORKER_PORT") or _home_setting(".claude-mem/settings.json", "CLAUDE_MEM_WORKER_PORT", "37777"))


def worker_alive(port: int) -> bool:
    """claude-mem answers on /api/health when its worker is up. Tests replace this function."""
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.5) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def companion_problem(plugin_id: str, folder: Path) -> str | None:
    """Why a plugin that is unpacked still does not work, or None. Only for plugins known to need a background
    process: claude-mem keeps its memory in a worker started by its hooks."""
    if plugin_id.split("@", 1)[0] != "claude-mem":
        return None
    if not (folder / "hooks" / "hooks.json").is_file():
        return t("status.ext.state.hooks_missing", plugin="claude-mem")
    port = worker_port()
    if worker_alive(port):
        return None
    return t("status.ext.state.worker_down", plugin="claude-mem", port=port)
