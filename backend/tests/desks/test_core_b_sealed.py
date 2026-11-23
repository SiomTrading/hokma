"""OA-38: a stored credential never appears in the prompt, the log or the output of a tool."""
import base64
import json
from urllib.parse import quote

import pytest

from hokma.desks.core import sealed

SENTINEL = "SENTINEL-sk-live-4f9a2c77e1/b+c="
OTHER = "SENTINEL-OTHER-0badc0ffee"


@pytest.fixture
def vault(tmp_path):
    return sealed.SealedVault(tmp_path / "data" / "sealed.json", sealed.MemoryKeyProvider())


def test_a_credential_is_not_on_disk_in_the_clear_OA_38(vault):
    vault.put("broker.key", SENTINEL)
    raw = vault._path.read_text(encoding="utf-8")
    assert SENTINEL not in raw and base64.b64encode(SENTINEL.encode()).decode() not in raw and "broker.key" in raw
    assert json.loads(raw)["items"]["broker.key"]["alg"] == "aes-256-gcm"


def test_the_value_comes_back_only_through_use_and_resolve_OA_38(vault):
    info = vault.put("broker.key", SENTINEL)
    assert info.placeholder == "{{sealed:broker.key}}" and vault.has("broker.key") and vault.names() == ["broker.key"]
    assert vault.use("broker.key", lambda secret: secret == SENTINEL) is True
    assert vault.resolve("Authorization: Bearer {{sealed:broker.key}}") == f"Authorization: Bearer {SENTINEL}"
    assert vault.resolve("no placeholder") == "no placeholder"


def test_the_value_never_reaches_a_prompt_a_log_or_a_tool_result_OA_38(vault):
    vault.put("broker.key", SENTINEL)
    vault.put("data.token", OTHER)
    forms = [SENTINEL, quote(SENTINEL, safe=""), base64.b64encode(SENTINEL.encode()).decode(),
             base64.urlsafe_b64encode(SENTINEL.encode()).decode()]
    for form in forms:
        out = vault.redact(f"echo of the request: key={form} end")
        assert SENTINEL not in out and form not in out and "{{sealed:broker.key}}" in out
    tool_result = {"url": f"https://x/?k={SENTINEL}", "rows": [{"note": OTHER}, 5, None], "ok": True}
    cleaned = vault.redact_value(tool_result)
    flat = json.dumps(cleaned)
    assert SENTINEL not in flat and OTHER not in flat and cleaned["rows"][1] == 5 and cleaned["ok"] is True
    assert "{{sealed:data.token}}" in flat


def test_assert_clean_and_leaks_name_the_credential_not_its_value_OA_38(vault):
    vault.put("broker.key", SENTINEL)
    assert vault.leaks("nothing here") == [] and vault.assert_clean("nothing here") == "nothing here"
    with pytest.raises(sealed.SecretLeak) as info:
        vault.assert_clean(f"prompt with {SENTINEL}")
    assert "broker.key" in str(info.value) and SENTINEL not in str(info.value)


def test_a_failed_use_does_not_leak_the_value_in_the_error_OA_38(vault):
    vault.put("broker.key", SENTINEL)
    with pytest.raises(sealed.UnknownCredential) as info:
        vault.use("missing.key", lambda s: s)
    assert SENTINEL not in str(info.value)


def test_another_key_or_a_changed_name_cannot_open_the_envelope_OA_38(tmp_path):
    vault = sealed.SealedVault(tmp_path / "s.json", sealed.MemoryKeyProvider())
    vault.put("broker.key", SENTINEL)
    stranger = sealed.SealedVault(tmp_path / "s.json", sealed.MemoryKeyProvider())
    with pytest.raises(sealed.UnsealError):
        stranger.use("broker.key", lambda s: s)
    data = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
    data["items"]["renamed"] = data["items"].pop("broker.key")
    (tmp_path / "s.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(sealed.UnsealError):
        vault.use("renamed", lambda s: s)


def test_the_envelope_is_authenticated_OA_38():
    key = b"k" * 32
    envelope = sealed.seal(SENTINEL, "a", key)
    assert sealed.unseal(envelope, "a", key) == SENTINEL
    tampered = dict(envelope, data=base64.b64encode(b"x" * 40).decode())
    with pytest.raises(sealed.UnsealError):
        sealed.unseal(tampered, "a", key)
    with pytest.raises(sealed.UnsealError):
        sealed.unseal({"sealed": 2}, "a", key)
    assert sealed.seal(SENTINEL, "a", key)["nonce"] != envelope["nonce"]


@pytest.mark.parametrize("name,secret", [("Bad Name", SENTINEL), ("", SENTINEL), ("../x", SENTINEL), ("ok", "short"), ("ok", 5)])
def test_bad_names_and_short_secrets_are_refused_OA_38(vault, name, secret):
    with pytest.raises(sealed.SealError):
        vault.put(name, secret)


def test_forget_removes_the_credential_and_replace_keeps_one_OA_38(vault):
    vault.put("broker.key", SENTINEL)
    vault.put("broker.key", OTHER)
    assert vault.names() == ["broker.key"] and vault.use("broker.key", lambda s: s) == OTHER
    assert vault.forget("broker.key") is True and vault.forget("broker.key") is False and vault.names() == []
    assert vault.redact(f"{OTHER}") == OTHER                            # nothing sealed any more, nothing to hide


def test_the_file_is_private_on_posix_OA_38(vault):
    import os
    import sys

    vault.put("broker.key", SENTINEL)
    if sys.platform != "win32":
        assert (os.stat(vault._path).st_mode & 0o777) == 0o600


def test_the_default_key_lives_in_the_system_credential_store_OA_38(tmp_path):
    import keyring
    from keyring.backend import KeyringBackend

    class Store(KeyringBackend):
        priority = 5

        def __init__(self):
            self.items = {}

        def get_password(self, service, username):
            return self.items.get((service, username))

        def set_password(self, service, username, password):
            self.items[(service, username)] = password

        def delete_password(self, service, username):
            self.items.pop((service, username), None)

    previous, store = keyring.get_keyring(), Store()
    keyring.set_keyring(store)
    try:
        vault = sealed.SealedVault(tmp_path / "s.json")
        vault.put("broker.key", SENTINEL)
        assert list(store.items) == [(sealed.KEY_SERVICE, sealed.KEY_USER)]
        assert sealed.SealedVault(tmp_path / "s.json").use("broker.key", lambda s: s) == SENTINEL      # same key on the next start
        assert store.items[(sealed.KEY_SERVICE, sealed.KEY_USER)] not in (tmp_path / "s.json").read_text(encoding="utf-8")
    finally:
        keyring.set_keyring(previous)


def test_without_a_credential_store_nothing_is_sealed_OA_38(tmp_path):
    import keyring
    from keyring.backends.fail import Keyring as FailKeyring

    from hokma.connections.store import StoreUnavailable

    previous = keyring.get_keyring()
    keyring.set_keyring(FailKeyring())
    try:
        with pytest.raises(StoreUnavailable):
            sealed.SealedVault(tmp_path / "s.json").put("broker.key", SENTINEL)
        assert not (tmp_path / "s.json").exists()
    finally:
        keyring.set_keyring(previous)


def test_the_key_is_not_next_to_the_envelopes_OA_38(tmp_path):
    vault = sealed.SealedVault(tmp_path / "s.json", sealed.MemoryKeyProvider())
    vault.put("broker.key", SENTINEL)
    assert [p.name for p in tmp_path.iterdir()] == ["s.json"]
