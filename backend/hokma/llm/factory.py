"""`make_llm`: the client an agent uses (SPEC-017: AH-52).

Resolves the engine Hokma already uses for the student, with the rule the chat uses (`hokma.engines.effective`): the saved
connection when it can run, else the verified one chosen last, else Claude Code. The model is the tier the desk gave the
agent. Nothing is chosen from this module: no engine name, no model name, no URL is written here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from hokma.engines import catalog
from hokma.engines.base import EngineAdapter, EngineContext
from hokma.llm.cache import CachedClient, PromptCache
from hokma.llm.clients import ProviderClient, client_class
from hokma.llm.protocol import LLMClient
from hokma.llm.retry import RetryPolicy


@dataclass(frozen=True)
class Resolved:
    """What `make_llm` needs to build a client: the connection (None is Claude Code without a saved row), the model and
    the folder the engine works in."""
    connection: dict[str, Any] | None
    model: str
    tier: str
    cwd: str


Resolver = Callable[[str, "int | None"], Resolved]
AdapterFactory = Callable[[str], EngineAdapter]


def _default_resolver(agent_slug: str, desk_id: int | None) -> Resolved:
    from hokma.core.config import hokma_home
    from hokma.db import get_database
    from hokma.db.errors import NotFound
    from hokma.desks import service as desks_service
    from hokma.engine.dbstore import DbChatStore
    from hokma.engines import effective
    from hokma.engines.registry import tier_of

    store = DbChatStore()
    with get_database().read() as u:
        default = u.engine_connections.default()
        project_id = None
        if desk_id is not None:
            try:
                project_id = u.desks.get(desk_id).project_id
            except NotFound:
                project_id = None
        saved = desks_service.model_in(u, project_id, agent_slug)
    chosen = effective.resolve(store._conn(default) if default else None, store.connections())
    tier = tier_of(saved)
    model = catalog.tier_model(chosen.conn, tier) or ""
    return Resolved(chosen.conn, model, tier, str(hokma_home()))


def _default_adapter(kind: str) -> EngineAdapter:
    from hokma.engines.registry import build_adapter
    from hokma.engines.service import _Owner

    return build_adapter(kind, _Owner())


def make_llm(agent_slug: str, *, desk_id: int | None = None, config: Mapping[str, Any] | None = None,
             resolver: Resolver | None = None, adapter_factory: AdapterFactory | None = None,
             cache: PromptCache | None = None, policy: RetryPolicy | None = None,
             sleep: Callable[[float], None] | None = None) -> LLMClient:
    """The model client of `agent_slug` on a desk.

    `resolver` and `adapter_factory` are the two seams for tests (a fake engine); by default they read the database and build the
    real adapter. With `cache` the client answers an identical question from disk. An adapter that cannot be built
    (an engine that is not installed) raises EngineError from the factory."""
    resolved = (resolver or _default_resolver)(agent_slug, desk_id)
    conn = resolved.connection
    kind = "claude_code" if conn is None else str(conn.get("engine") or "claude_code")
    adapter = (adapter_factory or _default_adapter)(kind)
    context = EngineContext(conversation_id="", model=resolved.model, cwd=resolved.cwd, agent=agent_slug, mode="ask",
                            connection=conn, tier=resolved.tier, task_id=f"llm-{agent_slug}",
                            extra={"no_session_flags": True, "desk_id": desk_id})
    provider = (conn or {}).get("provider") or ("anthropic" if kind == "claude_code" else kind)
    client: ProviderClient = client_class(provider)(adapter, context, config=config, policy=policy, sleep=sleep)
    return CachedClient(client, cache, agent=agent_slug) if cache is not None else client

