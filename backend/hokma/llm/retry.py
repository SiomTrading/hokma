"""Retries and token limits of a model call (SPEC-017: TA-87).

`coerce_max_retries` and `coerce_max_tokens` read a setting that may arrive as text (an environment variable) and fail
loudly on a wrong one. `RetryPolicy` and `call_with_retry` run a call again, with exponential backoff and a hard cap, only
for failures that can pass (network, rate limit, overload) and never for the ones that cannot (sign-in, region, model).
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

T = TypeVar("T")

MAX_RETRIES_LIMIT = 10
RETRYABLE_CODES = frozenset({"network", "timeout", "quota", "rate_limit", "overloaded", "unavailable", "engine_error"})
FATAL_CODES = frozenset({"auth", "region", "model", "model_choice", "config", "claude_not_found", "codex_not_found",
                         "gemini_not_found", "unsupported_engine", "no_engine", "bad_request", "device_auth"})


def coerce_max_retries(value: Any) -> int:
    """A non-negative integer (at most 10). Booleans, negatives and non-numbers raise ValueError."""
    if isinstance(value, bool):
        raise ValueError(f"max retries must be an integer, not a boolean: {value!r}")
    try:
        number = int(str(value).strip()) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        raise ValueError(f"max retries must be an integer, got {value!r}") from None
    if isinstance(value, float) and not float(value).is_integer():
        raise ValueError(f"max retries must be an integer, got {value!r}")
    if number < 0:
        raise ValueError(f"max retries must be >= 0, got {number}")
    if number > MAX_RETRIES_LIMIT:
        raise ValueError(f"max retries must be <= {MAX_RETRIES_LIMIT}, got {number}")
    return number


def coerce_max_tokens(value: Any, *, limit: int = 1_000_000) -> int:
    """A positive integer of tokens. Booleans, zero, negatives and non-numbers raise ValueError."""
    if isinstance(value, bool):
        raise ValueError(f"max tokens must be an integer, not a boolean: {value!r}")
    try:
        number = int(str(value).strip()) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        raise ValueError(f"max tokens must be an integer, got {value!r}") from None
    if isinstance(value, float) and not float(value).is_integer():
        raise ValueError(f"max tokens must be an integer, got {value!r}")
    if number <= 0:
        raise ValueError(f"max tokens must be > 0, got {number}")
    if number > limit:
        raise ValueError(f"max tokens must be <= {limit}, got {number}")
    return number


def is_retryable(code: str | None) -> bool:
    """True for a failure code that can pass by itself. An unknown code is not retried."""
    return bool(code) and code in RETRYABLE_CODES and code not in FATAL_CODES


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 2          # retries after the first try
    base_delay: float = 1.0       # seconds before the first retry
    factor: float = 2.0
    max_delay: float = 30.0       # a single wait never exceeds this
    jitter: float = 0.0           # fraction of the delay added at random; 0 keeps it deterministic

    def __post_init__(self) -> None:
        coerce_max_retries(self.max_retries)
        if self.base_delay < 0 or self.max_delay < 0 or self.factor < 1 or not 0 <= self.jitter <= 1:
            raise ValueError("bad retry policy")

    def delay(self, retry_number: int, rng: Callable[[], float] = random.random) -> float:
        """Seconds to wait before retry number `retry_number` (1 for the first retry)."""
        raw = min(self.base_delay * self.factor ** (retry_number - 1), self.max_delay)
        return min(raw * (1 + self.jitter * rng()), self.max_delay)


def call_with_retry(fn: Callable[[], T], *, policy: RetryPolicy | None = None, code_of: Callable[[BaseException], str | None],
                    sleep: Callable[[float], None] = time.sleep, on_retry: Callable[[int, BaseException, float], None] | None = None
                    ) -> T:
    """Run `fn`. When it raises and `code_of(error)` is retryable, wait and try again, at most `policy.max_retries` more times.

    The last error propagates unchanged. `on_retry(retry_number, error, delay)` is told before each wait."""
    policy = policy or RetryPolicy()
    retry = 0
    while True:
        try:
            return fn()
        except Exception as exc:                                   # noqa: BLE001  (the code decides, not the class)
            if retry >= policy.max_retries or not is_retryable(code_of(exc)):
                raise
            retry += 1
            wait = policy.delay(retry)
            if on_retry is not None:
                on_retry(retry, exc, wait)
            sleep(wait)
