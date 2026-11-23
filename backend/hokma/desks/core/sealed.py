"""Credentials sealed at rest, and never in text (SPEC-017: OA-38).

A credential a desk needs (a broker key, a data key) is stored as an AES-256-GCM envelope. The key of the envelopes lives in the
system credential store, never in a file next to them, so a copy of the data folder (a backup, a shared folder) does not carry
what is needed to read the credentials. The envelope is bound to the credential name: moving it under another name fails.

An agent never sees the value. It sees a placeholder, `{{sealed:name}}`, which `resolve` turns into the value only at the moment
a tool runs; what comes back from the tool, what goes into the prompt and what goes into a log passes through `redact`, which
replaces the value (and its URL-encoded and base64 forms) with the placeholder again.

What this does not do: malware running as the same user can read the key store like Hokma does. It protects the data folder
leaving the machine, and the model, the logs and the tool results from ever holding the value.
"""
from __future__ import annotations

import base64
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, TypeVar
from urllib.parse import quote

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from hokma.desks.core.paths import sealed_file

T = TypeVar("T")
ALG = "aes-256-gcm"
KEY_BYTES = 32
NONCE_BYTES = 12
MIN_SECRET = 6
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.\-]{0,59}$")
PLACEHOLDER_RE = re.compile(r"\{\{sealed:([a-z0-9][a-z0-9_.\-]{0,59})\}\}")
KEY_SERVICE = "hokma-sealing"
KEY_USER = "master"


class SealError(Exception):
    """Base of the errors of this module. The message never holds a credential."""


class UnsealError(SealError):
    """The envelope does not open: the key is missing or different, the file was changed, or the name does not match."""


class UnknownCredential(SealError):
    """There is no credential with that name."""


class SecretLeak(SealError):
    """A text holds a sealed value."""


class KeyProvider(Protocol):
    def get_or_create(self) -> bytes: ...


class MemoryKeyProvider:
    """A key kept in memory: for tests and for processes that must not touch the system store."""

    def __init__(self, key: bytes | None = None) -> None:
        self._key = key or os.urandom(KEY_BYTES)

    def get_or_create(self) -> bytes:
        return self._key


class KeyringKeyProvider:
    """The key in the system credential store (service `hokma-sealing`). With no usable store nothing is sealed."""

    def get_or_create(self) -> bytes:
        import keyring
        from keyring.errors import KeyringError

        from hokma.connections.store import StoreUnavailable, _usable

        try:
            if not _usable(keyring.get_keyring()):
                raise StoreUnavailable()
            raw = keyring.get_password(KEY_SERVICE, KEY_USER)
            if raw:
                key = base64.b64decode(raw)
                if len(key) != KEY_BYTES:
                    raise UnsealError("the sealing key is malformed")
                return key
            key = os.urandom(KEY_BYTES)
            keyring.set_password(KEY_SERVICE, KEY_USER, base64.b64encode(key).decode("ascii"))
            return key
        except KeyringError as exc:
            raise StoreUnavailable() from exc


def seal(value: str, name: str, key: bytes) -> dict[str, Any]:
    """An envelope of `value`, bound to `name`."""
    if len(key) != KEY_BYTES:
        raise SealError("the key must have 32 bytes")
    nonce = os.urandom(NONCE_BYTES)
    data = AESGCM(key).encrypt(nonce, value.encode("utf-8"), name.encode("utf-8"))
    return {"sealed": 1, "alg": ALG, "nonce": base64.b64encode(nonce).decode("ascii"), "data": base64.b64encode(data).decode("ascii")}


def unseal(envelope: dict[str, Any], name: str, key: bytes) -> str:
    if not isinstance(envelope, dict) or envelope.get("sealed") != 1 or envelope.get("alg") != ALG:
        raise UnsealError("the envelope has an unknown format")
    try:
        nonce = base64.b64decode(envelope["nonce"])
        data = base64.b64decode(envelope["data"])
        return AESGCM(key).decrypt(nonce, data, name.encode("utf-8")).decode("utf-8")
    except (InvalidTag, KeyError, ValueError):
        raise UnsealError("the envelope does not open with this key and name") from None


def placeholder(name: str) -> str:
    return "{{sealed:" + name + "}}"


@dataclass(frozen=True)
class CredentialInfo:
    name: str
    placeholder: str


class SealedVault:
    """The sealed credentials of one Hokma data folder."""

    def __init__(self, path: Path | str | None = None, key_provider: KeyProvider | None = None) -> None:
        self._path = Path(path) if path is not None else sealed_file()
        self._keys = key_provider or KeyringKeyProvider()

    # -- the file --------------------------------------------------------------------------------------------------
    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise UnsealError(f"the sealed file cannot be read: {type(exc).__name__}") from None
        items = data.get("items") if isinstance(data, dict) else None
        return items if isinstance(items, dict) else {}

    def _write(self, items: dict[str, dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(dir=self._path.parent, prefix=".sealed.", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as tmp:
                json.dump({"version": 1, "items": items}, tmp, indent=2, sort_keys=True)
            try:
                os.chmod(tmp_name, 0o600)
            except OSError:
                pass
            os.replace(tmp_name, self._path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    @staticmethod
    def _check_name(name: str) -> str:
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise SealError("a credential name has 1 to 60 characters: lowercase letters, digits, dot, dash and underscore")
        return name

    # -- the credentials -------------------------------------------------------------------------------------------
    def put(self, name: str, secret: str) -> CredentialInfo:
        """Seal and store a credential (replacing one with the same name). Returns the placeholder to use in text."""
        self._check_name(name)
        if not isinstance(secret, str) or len(secret) < MIN_SECRET:
            raise SealError(f"a credential has at least {MIN_SECRET} characters")
        items = self._read()
        items[name] = seal(secret, name, self._keys.get_or_create())
        self._write(items)
        return CredentialInfo(name, placeholder(name))

    def has(self, name: str) -> bool:
        return name in self._read()

    def names(self) -> list[str]:
        return sorted(self._read())

    def forget(self, name: str) -> bool:
        items = self._read()
        if name not in items:
            return False
        del items[name]
        self._write(items)
        return True

    def _secret(self, name: str, items: dict[str, dict[str, Any]], key: bytes) -> str:
        try:
            envelope = items[name]
        except KeyError:
            raise UnknownCredential(f"no credential named {name!r}") from None
        return unseal(envelope, name, key)

    def use(self, name: str, fn: Callable[[str], T]) -> T:
        """Run `fn(secret)` for a tool. The secret is passed in, never returned."""
        items = self._read()
        return fn(self._secret(name, items, self._keys.get_or_create()))

    def resolve(self, text: str) -> str:
        """`text` with each placeholder replaced by its value. Only a tool that runs the call may use the result."""
        names = set(PLACEHOLDER_RE.findall(text))
        if not names:
            return text
        items, key = self._read(), self._keys.get_or_create()
        values = {n: self._secret(n, items, key) for n in names}
        return PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], text)

    # -- keeping values out of text --------------------------------------------------------------------------------
    def _forms(self) -> list[tuple[str, str]]:
        """(form, name) of every stored value as written raw, URL-encoded and in base64, longest first."""
        items = self._read()
        if not items:
            return []
        key = self._keys.get_or_create()
        forms: list[tuple[str, str]] = []
        for name in items:
            secret = self._secret(name, items, key)
            for form in {secret, quote(secret, safe=""), base64.b64encode(secret.encode("utf-8")).decode("ascii"),
                         base64.urlsafe_b64encode(secret.encode("utf-8")).decode("ascii")}:
                if len(form) >= MIN_SECRET:
                    forms.append((form, name))
        return sorted(forms, key=lambda f: len(f[0]), reverse=True)

    def redact(self, text: str) -> str:
        """`text` with every sealed value replaced by its placeholder. Safe for a prompt, a log or a tool result."""
        out = str(text)
        for form, name in self._forms():
            out = out.replace(form, placeholder(name))
        return out

    def redact_value(self, value: Any) -> Any:
        """`redact` applied to every string inside dicts, lists and tuples (the result of a tool)."""
        forms = self._forms()

        def walk(node: Any) -> Any:
            if isinstance(node, str):
                for form, name in forms:
                    node = node.replace(form, placeholder(name))
                return node
            if isinstance(node, dict):
                return {walk(k) if isinstance(k, str) else k: walk(v) for k, v in node.items()}
            if isinstance(node, (list, tuple)):
                return type(node)(walk(v) for v in node)
            return node

        return walk(value)

    def leaks(self, text: str) -> list[str]:
        """Names of the credentials whose value appears in `text`."""
        return sorted({name for form, name in self._forms() if form in text})

    def assert_clean(self, text: str) -> str:
        """`text` itself, or SecretLeak naming the credentials that appear in it (never their values)."""
        found = self.leaks(text)
        if found:
            raise SecretLeak(f"text holds the value of: {', '.join(found)}")
        return text
