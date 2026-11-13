"""Connection management: create, test (a real short turn), list models, import what the student already has.

Nothing here prints or returns a secret. Secrets go to the system credential store (`hokma-engine`) and nowhere
else."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from hokma.db import get_database
from hokma.engines import effective
from hokma.engine.dbstore import DbChatStore
from hokma.engines import catalog, diagnose, secrets
from hokma.engines.base import EngineContext, EngineError
from hokma.engines.registry import build_adapter, installed_clis
from hokma.engines.direct import wire
from hokma.connections.store import StoreUnavailable

PING_SECONDS = 30
# The only keys read from the student's own Claude settings (env block) plus apiKeyHelper.
IMPORT_ENV_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
                   "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                   "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "AWS_REGION", "ANTHROPIC_VERTEX_PROJECT_ID",
                   "CLOUD_ML_REGION", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")
SECRET_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


class ServiceError(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status, self.code = status, code


def _clean_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ServiceError(422, "engines.bad_url")
    return url.strip().rstrip("/")


def public_view(c) -> dict[str, Any]:
    """A connection as the panel sees it: never a secret, the host instead of the full URL."""
    host = urlparse(c.base_url).hostname if c.base_url else None
    return {"id": c.id, "engine": c.engine, "label": c.label, "auth_kind": c.auth_kind, "provider": c.provider,
            "wire": c.wire, "host": host, "tier_models": c.tier_models or {}, "status": c.status,
            "status_detail": c.status_detail, "is_default": c.is_default, "checked_at": c.checked_at,
            "has_secret": bool(secrets.get(c.id)) if c.auth_kind not in ("subscription", "local") else False}


def detect() -> dict[str, Any]:
    """Binaries on PATH and the signs of an existing sign-in. Reads state only, never starts a login."""
    found = {k: bool(v) for k, v in installed_clis().items()}
    return {"cli": found, "codex_login": (Path.home() / ".codex" / "auth.json").is_file(),
            "claude_import": bool(claude_import_preview()["found"]), "providers": [
                {"id": k, "label": v["label"], "wire": v["wire"], "base_url": v.get("base_url", ""),
                 "local": bool(v.get("local")), "tiers": v.get("tiers", {})}
                for k, v in catalog.load()["providers"].items() if k != "gemini_cli"]}


def list_connections() -> list[dict[str, Any]]:
    with get_database().read() as u:
        return [public_view(c) for c in u.engine_connections.list()]


def add_api(provider: str, key: str | None, *, label: str | None = None, base_url: str | None = None,
            model: str | None = None) -> dict[str, Any]:
    info = catalog.provider(provider)
    if not info:
        raise ServiceError(422, "engines.bad_provider")
    url = _clean_url(base_url or info["base_url"] or "")
    local = bool(info.get("local"))
    if not local and not (key or "").strip():
        raise ServiceError(422, "engines.key_required")
    tiers = {}
    if model:                                                  # one exact model for every level
        tiers = {t: model for t in catalog.TIERS}
    with get_database().write() as u:
        conn = u.engine_connections.add("api", label or info["label"], "local" if local else "api_key",
                                        provider=provider, wire=info["wire"], base_url=url, tier_models=tiers or None,
                                        auto_default=False)
    conn = _default_if_needed(conn.id)
    if not local:
        try:
            secrets.put(conn.id, key.strip())                  # type: ignore[union-attr]
        except StoreUnavailable:
            with get_database().write() as u:
                u.engine_connections.remove(conn.id)
            raise ServiceError(503, "engines.store_unavailable") from None
    return public_view(conn)


def add_cli(engine: str, auth_kind: str, label: str, key: str | None = None) -> dict[str, Any]:
    if engine not in ("claude_code", "codex", "gemini_cli"):
        raise ServiceError(422, "engines.bad_engine")
    with get_database().write() as u:
        conn = u.engine_connections.add(engine, label, auth_kind, auto_default=False)
    conn = _default_if_needed(conn.id)
    if key and auth_kind in ("api_key", "oauth_token"):
        try:
            secrets.put(conn.id, key.strip())
        except StoreUnavailable:
            with get_database().write() as u:
                u.engine_connections.remove(conn.id)
            raise ServiceError(503, "engines.store_unavailable") from None
    return public_view(conn)


def _default_if_needed(connection_id: int):
    """A new connection becomes the default only when it is Claude Code itself, or when Claude Code cannot be used
    (bug 1095: a Gemini row added by a click, then not installed, used to take every chat from a signed-in Claude)."""
    with get_database().write() as u:
        new = u.engine_connections.get(connection_id)
        if u.engine_connections.default() is None and (new.engine == "claude_code" or not effective.claude_connected()):
            new = u.engine_connections.set_default(connection_id, chosen=False)
        return new


def _views() -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    store = DbChatStore()
    with get_database().read() as u:
        default = u.engine_connections.default()
    return (store._conn(default) if default else None), store.connections()


def active_engine(conversation_id: str | None = None) -> dict[str, Any]:
    """The engine that WILL answer, by the rule the chat itself uses: the screen shows this and nothing else."""
    store = DbChatStore()
    default, others = _views()
    conn = store.connection_for(conversation_id) if conversation_id else default
    chosen = effective.resolve(conn, others)
    stuck = effective.is_stuck(conn, others)
    shown = chosen.conn
    return {"id": shown.get("id") if shown else None, "engine": shown.get("engine") if shown else "claude_code",
            "label": effective.label_of(shown), "replaced": chosen.fallback_from is not None, "available": not stuck,
            "saved": effective.label_of(conn) if chosen.fallback_from is not None else None}


_CLAUDE_MODELS = (("haiku", "fast", "Haiku"), ("sonnet", "balanced", "Sonnet"), ("opus", "deep", "Opus"))


def model_menu() -> list[dict[str, Any]]:
    """What the model button of the chat lists: one group per engine that is connected AND was proved to work, each with
    its three models from the catalog Hokma already has per engine (no list of ours). The engine that answers is always
    among them: when nothing is verified and a saved one is tried anyway, it is listed too (bug 1104, one rule).
    `id` is the level the conversation stores (haiku, sonnet, opus), whatever the engine."""
    default, conns = _views()
    claude_ok = effective.claude_connected()
    flags = {"is_installed": effective.installed, "claude_ok": claude_ok}
    groups: list[dict[str, Any]] = []

    def group(conn: dict[str, Any] | None) -> dict[str, Any]:
        if conn is None or conn["engine"] == "claude_code":
            label = "Claude" if conn is None else conn["label"]
            models = [{"id": m, "tier": tier, "name": name} for m, tier, name in _CLAUDE_MODELS]
        else:
            label = conn["label"]
            models = [{"id": m, "tier": tier, "name": catalog.tier_model(conn, tier) or name} for m, tier, name in _CLAUDE_MODELS]
        return {"connection_id": None if conn is None else conn["id"], "engine": "claude_code" if conn is None else conn["engine"],
                "label": label, "models": models}

    has_claude_row = any(c["engine"] == "claude_code" and c.get("auth_kind") == "subscription" for c in conns)
    if claude_ok and not has_claude_row:
        groups.append(group(None))
    for conn in conns:
        if effective.verified(conn, **flags):
            groups.append(group(conn))
    shown = effective.resolve(default, conns).conn
    listed = any(g["connection_id"] == (shown or {}).get("id") for g in groups) \
        or (shown is None and any(g["engine"] == "claude_code" for g in groups))     # Claude without a row is the Claude group
    if not listed and effective.can_run(shown, **flags):
        groups.append(group(shown))
    groups.sort(key=lambda g: g["engine"] != "claude_code")        # Claude first, the others as they were saved
    return groups


def ensure_claude_connection() -> dict[str, Any]:
    """The connection row of Claude Code itself, made once when the student picks Claude in the model button, so that
    every engine the button lists is a saved connection (a conversation can be attached to it)."""
    with get_database().write() as u:
        for c in u.engine_connections.list():
            if c.engine == "claude_code" and c.auth_kind == "subscription":
                return public_view(c)
        conn = u.engine_connections.add("claude_code", "Claude", "subscription", auto_default=False)
        u.engine_connections.set_status(conn.id, "connected", None)
        return public_view(u.engine_connections.get(conn.id))


def startup_check() -> None:
    """At start: say in engine.log which engine will answer and why (never changes what the student chose)."""
    try:
        from hokma.engine import errlog

        info = active_engine()
        errlog.log(f"engine at start: {info['label']} ({info['engine']}), available={info['available']}"
                   + (f", replaced the saved {info['saved']}" if info["replaced"] else ""), scope="engines")
    except Exception:                                   # noqa: BLE001  (a log problem never stops the start)
        pass


def remove(connection_id: int) -> None:
    with get_database().write() as u:
        u.engine_connections.remove(connection_id)
    secrets.delete(connection_id)


def set_default(connection_id: int) -> None:
    with get_database().write() as u:
        u.engine_connections.set_default(connection_id)


def rename(connection_id: int, label: str) -> dict[str, Any]:
    with get_database().write() as u:
        return public_view(u.engine_connections.update(connection_id, label=label))


def set_tiers(connection_id: int, tiers: dict[str, str]) -> dict[str, Any]:
    clean = {k: str(v).strip()[:120] for k, v in tiers.items() if k in catalog.TIERS and str(v).strip()}
    with get_database().write() as u:
        return public_view(u.engine_connections.update(connection_id, tier_models=clean))


# --------------------------------------------------------------------- test

async def ping(connection_id: int, owner: Any = None) -> dict[str, Any]:
    """A short real turn at the Fast level. The result is saved in the connection (status and, for a region block,
    status_detail = 'region')."""
    with get_database().read() as u:
        c = u.engine_connections.get(connection_id)
    conn = DbChatStore._conn(c)
    adapter = build_adapter(c.engine, owner or _Owner())
    model = catalog.tier_model(conn, "fast") or ""
    ctx = EngineContext(conversation_id="", model=model, cwd=str(_cwd()), mode="ask", connection=conn, tier="fast",
                        instructions=_chat_instructions(),
                        task_id="ping", extra={"base_flags": ["-p", "--output-format", "stream-json", "--input-format",
                                                              "stream-json", "--verbose", "--permission-prompt-tool",
                                                              "stdio"], "no_session_flags": True})
    code, message, said = "", "", ""
    session = None
    try:
        session = await adapter.open(ctx)

        async def run() -> None:
            nonlocal code, message, said
            async for ev in session.send([{"type": "text", "text": "reply OK"}]):
                if ev["type"] == "text":
                    said += ev["text"]
                elif ev["type"] == "permission_request":
                    await session.answer_permission(ev["request_id"], False, "ping")
                elif ev["type"] == "error":
                    code, message = ev.get("code", "engine_error"), ev.get("message", "")
                    return
                elif ev["type"] == "done":
                    return

        await asyncio.wait_for(run(), PING_SECONDS)
    except EngineError as exc:
        code, message = exc.code, exc.message
    except asyncio.TimeoutError:
        code, message = "network", "no answer in time"
    except Exception as exc:                         # noqa: BLE001  (any failure of the launch is a result, not a 500)
        code, message = "engine_error", f"{type(exc).__name__}: {exc}"
        from hokma.engine import errlog
        errlog.log("connection test failed", exc, scope="engines")
    finally:
        if session is not None:
            await session.close()
    if c.engine == "claude_code" and not code:
        # Refresh the saved sign-in state (`claude auth status`): a signed-out Claude must not stay "connected" (bug 1112)
        from hokma.setup import claude as claude_setup

        state = await asyncio.to_thread(claude_setup.detect)
        with get_database().write() as u:
            if u.profile.get() is not None:
                u.profile.set_claude_status(state.state, state.plan)
        code = "auth" if state.state == "no_session" else code
    if code in ("claude_not_found", "codex_not_found", "gemini_not_found"):
        status, detail = "no_cli", code
    elif code == "auth":
        status, detail = "no_session", "auth"
    elif code:
        status = "error"
        detail = code if code in ("region", "quota", "network") else diagnose.classify_text(c.provider, message)
    else:
        status, detail = "connected", None
    with get_database().write() as u:
        u.engine_connections.set_status(connection_id, status, detail)

    return {"ok": not code, "status": status, "code": detail or "", "latency_ok": bool(said or not code)}


class _Owner:
    """What the Claude and Codex adapters read from their owner when there is no chat engine around."""
    claude_bin = None
    codex_bin = None
    session_token = ""

    @staticmethod
    def launch():
        from hokma.kit.launch import prepare_claude_launch

        return prepare_claude_launch()


def _chat_instructions() -> str:
    """The same instructions a chat hands to the engine, so the test writes the same file and fails where the chat fails."""
    try:
        from hokma.engine.engine import ChatEngine

        return ChatEngine.instructions_for(str(_cwd()), None)
    except Exception:                                # noqa: BLE001  (the test then reports what the launch says)
        return ""


def _cwd() -> Path:
    from hokma.core.config import hokma_home

    return hokma_home()


# --------------------------------------------------------------- live models

def live_models(connection_id: int) -> list[str]:
    with get_database().read() as u:
        c = u.engine_connections.get(connection_id)
    if c.engine != "api":
        return []
    info = catalog.provider(c.provider)
    key = secrets.get(c.id)
    base = (c.base_url or info.get("base_url") or "").rstrip("/")
    headers: dict[str, str] = {}
    if c.wire == "anthropic":
        headers = {"x-api-key": key or "", "anthropic-version": "2023-06-01"}
    elif key:
        headers = {"Authorization": f"Bearer {key}"}
    try:
        data = wire.get_json(base + info.get("models_path", "/models"), headers)
    except wire.WireError:
        return []
    items = data.get("data") or data.get("models") or []
    names = [str(i.get("id") or i.get("name") or "").removeprefix("models/") for i in items if isinstance(i, dict)]
    return sorted(n for n in names if n)[:500]


# ------------------------------------------------------- import from Claude

def _claude_settings() -> dict[str, Any]:
    path = Path.home() / ".claude" / "settings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def claude_import_preview() -> dict[str, Any]:
    """What the student's own Claude has that Hokma can reuse: variable NAMES and the destination host, never a value
    that is a secret."""
    data = _claude_settings()
    env = data.get("env") if isinstance(data.get("env"), dict) else {}
    names = [k for k in IMPORT_ENV_KEYS if env.get(k)]
    host = urlparse(str(env.get("ANTHROPIC_BASE_URL"))).hostname if env.get("ANTHROPIC_BASE_URL") else None
    return {"found": bool(names or data.get("apiKeyHelper")), "variables": names, "host": host,
            "api_key_helper": bool(data.get("apiKeyHelper")),
            "session_file": (Path.home() / ".claude" / ".credentials.json").is_file()}


def claude_import_apply() -> dict[str, Any]:
    """Create a Claude Code connection from the student's env block and apiKeyHelper. The values (secret or not) are
    stored as one JSON entry in the credential store; nothing is written to a file in the data folder."""
    data = _claude_settings()
    env = data.get("env") if isinstance(data.get("env"), dict) else {}
    picked = {k: str(env[k]) for k in IMPORT_ENV_KEYS if env.get(k)}
    helper = data.get("apiKeyHelper")
    if not picked and not helper:
        raise ServiceError(404, "engines.nothing_to_import")
    kind = "gateway" if picked.get("ANTHROPIC_BASE_URL") else "imported"
    payload = {"env": picked, **({"apiKeyHelper": str(helper)} if helper else {})}
    with get_database().write() as u:
        conn = u.engine_connections.add("claude_code", "Claude (imported)", kind,
                                        base_url=picked.get("ANTHROPIC_BASE_URL"), auto_default=False)
    conn = _default_if_needed(conn.id)
    try:
        secrets.put(conn.id, json.dumps(payload))
    except StoreUnavailable:
        with get_database().write() as u:
            u.engine_connections.remove(conn.id)
        raise ServiceError(503, "engines.store_unavailable") from None
    return public_view(conn)


def _key_helper(cid: Any, which: str = "") -> str:
    script = Path(__file__).resolve().parent.parent / "kit" / "engine_key.py"
    return f'"{shutil.which("python3") or "python"}" -I "{script}" --conn {cid}' + (f" --var {which}" if which else "")


def claude_env_for(connection: dict[str, Any]) -> tuple[dict[str, str], str | None]:
    """(environment, apiKeyHelper command) a Claude Code connection adds to the process. Secrets never go to the
    environment the agent's commands inherit: a plain key and the secret variables of an imported setup are handed to
    Claude Code by `apiKeyHelper` (engine_key.py). Only the non-secret variables (URL, model names, region, proxy)
    go to the environment. The one exception is a long-lived OAuth token (`setup-token`), which Claude Code only
    accepts through CLAUDE_CODE_OAUTH_TOKEN."""
    kind, cid = connection.get("auth_kind"), connection.get("id")
    if kind == "subscription" or cid is None:
        return {}, None
    raw = secrets.get(cid)
    if not raw:
        return {}, None
    if kind == "oauth_token":
        return {"CLAUDE_CODE_OAUTH_TOKEN": raw}, None
    if kind == "api_key":
        return {}, _key_helper(cid)
    try:
        payload = json.loads(raw)
    except ValueError:
        return {}, None
    env = {k: v for k, v in (payload.get("env") or {}).items() if k not in SECRET_KEYS}
    helper = payload.get("apiKeyHelper")
    if not helper:
        for name in SECRET_KEYS:
            if (payload.get("env") or {}).get(name):
                helper = _key_helper(cid, name)
                break
    return env, helper


_ = (os, re)
