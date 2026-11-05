"""Which database the app uses: `<data>/database.json`, and the rules for a Postgres connection URL.

The URL carries the password, so the file is written owner-only (0600) in the protected data folder, atomically.
`redact()` is the only way a URL or an error text may leave this module toward a response or a log.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit

from hokma.db.errors import ValidationError
from hokma.db.paths import database_config_path

MAX_URL_LENGTH = 1024
SCHEMES = ("postgresql", "postgres")
# libpq options that read local files or run commands on connect are refused; these are harmless.
ALLOWED_QUERY = ("sslmode", "connect_timeout", "application_name", "channel_binding", "target_session_attrs")
SSL_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")


@dataclass(frozen=True)
class DatabaseConfig:
    engine: str = "sqlite"                     # "sqlite" | "postgres"
    url: str | None = None                     # kept even when engine is sqlite, so switching back needs no retyping


def parse_url(raw: object) -> str:
    """Validate and normalise a Postgres URL. Raises ValidationError with a catalog code in args[0]."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError("url_empty")
    text = raw.strip()
    if len(text) > MAX_URL_LENGTH or any(ord(c) < 32 or ord(c) == 127 or c.isspace() for c in text):
        raise ValidationError("url_invalid")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise ValidationError("url_invalid") from None
    if parts.scheme.lower() not in SCHEMES:
        raise ValidationError("url_scheme")
    if not parts.hostname:
        raise ValidationError("url_host")
    if port is not None and not 0 < port < 65536:
        raise ValidationError("url_invalid")
    if not parts.path.strip("/"):
        raise ValidationError("url_database")
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key not in ALLOWED_QUERY:
            raise ValidationError("url_option")
        if key == "sslmode" and value not in SSL_MODES:
            raise ValidationError("url_option")
        if key == "connect_timeout" and not value.isdigit():
            raise ValidationError("url_option")
    return urlunsplit(("postgresql", parts.netloc, parts.path, parts.query, ""))


def password_of(url: str) -> str | None:
    try:
        value = urlsplit(url).password
    except ValueError:
        return None
    return unquote(value) if value else None


def redact(text: str, url: str | None = None) -> str:
    """`text` with the password (raw and percent-encoded) and any user:password@ pair replaced by ***."""
    out = re.sub(r"(postgres(?:ql)?://[^:/@\s]*:)[^@\s]*@", r"\1***@", text)
    if url:
        secret = password_of(url)
        if secret:
            for variant in {secret, quote(secret, safe="")}:
                out = out.replace(variant, "***")
    return out


def display_url(url: str) -> str:
    """The URL with the password hidden, for the panel."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    user = f"{unquote(parts.username)}@" if parts.username else ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{user}{host}{port}{parts.path}"


def describe(url: str) -> dict[str, str | int | None]:
    parts = urlsplit(url)
    return {"host": parts.hostname, "port": parts.port or 5432, "database": parts.path.lstrip("/"),
            "user": unquote(parts.username) if parts.username else None, "display": display_url(url)}


def load_config() -> DatabaseConfig:
    try:
        data = json.loads(database_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DatabaseConfig()
    if not isinstance(data, dict):
        return DatabaseConfig()
    url = data.get("url")
    try:
        url = parse_url(url) if url else None
    except ValidationError:
        url = None
    engine = data.get("engine")
    if engine == "postgres" and url:
        return DatabaseConfig("postgres", url)
    return DatabaseConfig("sqlite", url)


def save_config(config: DatabaseConfig) -> None:
    path = database_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    payload = json.dumps({"engine": config.engine, "url": config.url}, indent=1, sort_keys=True) + "\n"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
