"""Guards between untrusted text and the places it can do harm (SPEC-017: TA-57, TA-58).

- `safe_ticker_component`: a symbol is interpolated into paths and provider URLs. It comes from the student or from a tool
  call, and a tool call can be steered by text inside a fetched page. Only a plain symbol passes.
- `scrub_text` and friends: a provider error or response can repeat the key that was sent (in the URL, in a header, in the
  body). Whatever goes to the model or to a log passes through here first.
- `call_scrubbed`: runs a call and re-raises its failure with the secret removed, with no request, no response and no
  exception chain attached (those carry the URL).
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Mapping, TypeVar

T = TypeVar("T")

TICKER_RE = re.compile(r"^[A-Za-z0-9._\-\^=+]+$")
MAX_TICKER = 32
MASK = "***"

# Query or form parameters that carry a secret, and header names that do.
SECRET_PARAMS = ("api_key", "apikey", "api-key", "key", "token", "access_token", "refresh_token", "id_token", "secret",
                 "client_secret", "password", "passwd", "auth", "authorization", "signature", "sig", "session",
                 "account", "account_id", "accountid", "accountnumber", "account_number", "hashvalue", "account_hash")
SECRET_HEADERS = ("authorization", "proxy-authorization", "x-api-key", "api-key", "x-auth-token", "x-access-token",
                  "cookie", "set-cookie", "x-goog-api-key", "x-account-id")

_PARAM_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<name>(?:" + "|".join(re.escape(p) for p in sorted(SECRET_PARAMS, key=len, reverse=True)) + r"))"
    r"(?P<sep>[\"']?\s*[=:]\s*)(?P<quote>[\"']?)(?P<value>[^\s&\"',;}\]]+)", re.IGNORECASE)
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic|token)\s+([A-Za-z0-9._~+/=\-]{8,})")
_HEADER_RE = re.compile(r"(?im)^(?P<name>" + "|".join(re.escape(h) for h in SECRET_HEADERS) + r")(?P<sep>\s*:\s*)(?P<value>.*)$")
_URL_USERINFO_RE = re.compile(r"(?P<scheme>://)[^/\s:@]+:[^/\s@]+@")
_LONG_TOKEN_RE = re.compile(r"\b(?:sk|pk|rk|xox[abp]|ghp|gho|ghs|AKIA|AIza)[A-Za-z0-9_\-]{12,}\b")


def safe_ticker_component(value: str, *, max_len: int = MAX_TICKER) -> str:
    """`value` unchanged when it is safe to use as a path component and inside a URL; ValueError otherwise.

    Letters, digits and `. _ - ^ = +` pass. A slash, a backslash, a semicolon, a space, a colon, `..` and a name made only of
    dots do not."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"ticker must be a non-empty string, got {value!r}")
    if len(value) > max_len:
        raise ValueError(f"ticker exceeds {max_len} chars: {value[:40]!r}")
    if not TICKER_RE.fullmatch(value):
        raise ValueError(f"ticker contains characters not allowed in a path: {value!r}")
    if set(value) == {"."}:
        raise ValueError(f"ticker cannot consist solely of dots: {value!r}")
    return value


def is_safe_ticker(value: Any) -> bool:
    try:
        safe_ticker_component(value)  # type: ignore[arg-type]
    except ValueError:
        return False
    return True


def scrub_text(text: str, secrets: Iterable[str] = ()) -> str:
    """`text` with every known secret, every secret-looking parameter, header and bearer token replaced by `***`."""
    out = str(text)
    for secret in sorted({s for s in secrets if isinstance(s, str) and len(s) >= 4}, key=len, reverse=True):
        out = out.replace(secret, MASK)
    out = _URL_USERINFO_RE.sub(lambda m: m.group("scheme") + MASK + "@", out)
    out = _HEADER_RE.sub(lambda m: f"{m.group('name')}{m.group('sep')}{MASK}", out)
    out = _BEARER_RE.sub(lambda m: f"{m.group(1)} {MASK}", out)
    out = _PARAM_RE.sub(lambda m: f"{m.group('name')}{m.group('sep')}{m.group('quote')}{MASK}", out)
    out = _LONG_TOKEN_RE.sub(MASK, out)
    return out


def scrub_value(value: Any, secrets: Iterable[str] = ()) -> Any:
    """`scrub_text` applied to every string inside a dict, list or tuple. Keys that name a secret lose their value."""
    secrets = tuple(secrets)
    if isinstance(value, str):
        return scrub_text(value, secrets)
    if isinstance(value, Mapping):
        out = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in SECRET_PARAMS + SECRET_HEADERS:
                out[key] = MASK
            else:
                out[key] = scrub_value(item, secrets)
        return out
    if isinstance(value, (list, tuple)):
        return type(value)(scrub_value(v, secrets) for v in value)
    return value


class ScrubbedError(Exception):
    """A call failed. `kind` is the class name of the original failure; the text has the secrets removed."""

    def __init__(self, message: str, kind: str = "Exception") -> None:
        super().__init__(message)
        self.kind = kind


def call_scrubbed(fn: Callable[..., T], *args: Any, secrets: Iterable[str] = (), passthrough: Iterable[type] = (),
                  **kwargs: Any) -> T:
    """Run `fn`; when it raises, raise ScrubbedError with the secrets removed from the message.

    The new error carries no `__cause__`, no `__context__` and no traceback of the original, because the original usually
    holds the request URL. The re-raise happens outside the `except` block so Python does not chain it.
    Exception classes in `passthrough` propagate untouched."""
    secrets = tuple(secrets)
    passthrough = tuple(passthrough)
    try:
        return fn(*args, **kwargs)
    except passthrough:
        raise
    except Exception as exc:                                    # noqa: BLE001  (any provider failure is cleaned)
        error = ScrubbedError(scrub_text(str(exc), secrets), type(exc).__name__)
    raise error from None
