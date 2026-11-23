"""The clients of each provider (SPEC-017: TA-90).

Seven families of provider are reached through the same engine layer: Anthropic, OpenAI, Google, DeepSeek, Moonshot (Kimi),
OpenRouter and local servers (Ollama, LM Studio). Each client is a `ChatLLM` on a `hokma.engines` adapter that carries the
parameters its provider takes (`params.provider_kwargs`) and the capabilities of its model. None of them opens a socket: the
adapter owns the connection, the key and the permission rules.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from hokma.engines.base import EngineAdapter, EngineContext
from hokma.llm import capabilities
from hokma.llm.params import provider_kwargs
from hokma.llm.protocol import ChatLLM
from hokma.llm.retry import RetryPolicy


class ProviderClient(ChatLLM):
    """A `ChatLLM` of one provider. `provider` is its id in `hokma.llm.registry`."""

    provider_id = ""

    def __init__(self, adapter: EngineAdapter, context: EngineContext, *, config: Mapping[str, Any] | None = None,
                 policy: RetryPolicy | None = None, sleep: Callable[[float], None] | None = None) -> None:
        config = dict(config or {})
        params = capabilities.drop_unsupported(context.model, provider_kwargs(self.provider_id, config))
        if policy is None and config.get("llm_max_retries") is not None:
            policy = RetryPolicy(max_retries=int(config["llm_max_retries"]))
        super().__init__(adapter, context, model=context.model, provider=self.provider_id, policy=policy, sleep=sleep, params=params)

    @property
    def capabilities(self) -> capabilities.ModelCapabilities:
        return capabilities.get_capabilities(self.model)

    @property
    def structured_method(self) -> str:
        return capabilities.structured_method(self.model)


class AnthropicClient(ProviderClient):
    provider_id = "anthropic"


class OpenAIClient(ProviderClient):
    provider_id = "openai"


class GoogleClient(ProviderClient):
    provider_id = "google"


class DeepSeekClient(ProviderClient):
    provider_id = "deepseek"


class MoonshotClient(ProviderClient):
    provider_id = "moonshot"


class OpenRouterClient(ProviderClient):
    provider_id = "openrouter"


class LocalClient(ProviderClient):
    """Ollama, LM Studio or any server on the student's own machine."""

    provider_id = "local"


CLIENTS: dict[str, type[ProviderClient]] = {c.provider_id: c for c in (
    AnthropicClient, OpenAIClient, GoogleClient, DeepSeekClient, MoonshotClient, OpenRouterClient, LocalClient)}
LOCAL_PROVIDERS = frozenset({"ollama", "lmstudio", "local"})


def client_class(provider_id: str | None) -> type[ProviderClient]:
    """The client class of a provider id. Local providers share one; an unknown or custom endpoint speaks the OpenAI wire."""
    if provider_id in LOCAL_PROVIDERS:
        return LocalClient
    return CLIENTS.get(provider_id or "", OpenAIClient)
