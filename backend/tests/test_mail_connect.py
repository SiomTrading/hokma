"""Gmail connection (C7-2a): app password over IMAP and SMTP, saved in the credential store only after both logins pass.
No real network: the imaplib / smtplib classes are replaced by scripted fakes."""
import imaplib
import json
import smtplib

import keyring
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.i18n import catalog
from hokma.mail import gmail
from hokma.mail.api import router
from hokma.rpc.ws import SESSION_TOKEN
from live_support import MemoryKeyring

AUTH = {"X-Hokma-Token": SESSION_TOKEN}
EMAIL, SHOWN, PASSWORD = "student@gmail.com", "abcd efgh ijkl mnop", "abcdefghijklmnop"


class Servers:
    """Records what each server was asked; `imap_error` / `smtp_error` is raised at login."""

    def __init__(self):
        self.log, self.imap_error, self.smtp_error = [], None, None

    def imap(self, host, port, **kw):
        outer = self
        outer.log.append(("imap", host, port))

        class Box:
            def login(self, user, password):
                outer.log.append(("imap-login", user, password))
                if outer.imap_error:
                    raise outer.imap_error

            def logout(self):
                outer.log.append(("imap-logout",))

        return Box()

    def smtp(self, host, port, **kw):
        outer = self
        outer.log.append(("smtp", host, port))

        class Server:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def login(self, user, password):
                outer.log.append(("smtp-login", user, password))
                if outer.smtp_error:
                    raise outer.smtp_error

        return Server()


@pytest.fixture(autouse=True)
def store():
    previous = keyring.get_keyring()
    memory = MemoryKeyring()
    keyring.set_keyring(memory)
    yield memory
    keyring.set_keyring(previous)


@pytest.fixture
def servers(monkeypatch):
    fake = Servers()
    monkeypatch.setattr(imaplib, "IMAP4_SSL", fake.imap)
    monkeypatch.setattr(smtplib, "SMTP_SSL", fake.smtp)
    return fake


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def put(client, email=EMAIL, password=SHOWN, lang="en"):
    return client.put("/api/mail", json={"email": email, "password": password},
                      headers={**AUTH, "Accept-Language": lang})


def test_routes_need_the_session_token(client):
    assert client.get("/api/mail").status_code == 401
    assert client.put("/api/mail", json={"email": EMAIL, "password": PASSWORD}).status_code == 401
    assert client.delete("/api/mail").status_code == 401


def test_empty_state_has_the_google_link(client):
    body = client.get("/api/mail", headers=AUTH).json()
    assert body == {"connected": False, "email": None, "store_available": True,
                    "apppasswords_url": "https://myaccount.google.com/apppasswords"}


def test_connect_tests_both_servers_then_saves_and_never_echoes_the_password(client, servers, store):
    response = put(client)
    assert response.json() == {"connected": True, "email": EMAIL}
    assert ("imap", "imap.gmail.com", 993) in servers.log and ("smtp", "smtp.gmail.com", 465) in servers.log
    assert ("imap-login", EMAIL, PASSWORD) in servers.log and ("smtp-login", EMAIL, PASSWORD) in servers.log   # spaces dropped
    assert json.loads(store.items[("hokma-market", "gmail")]) == {"email": EMAIL, "password": PASSWORD}
    state = client.get("/api/mail", headers=AUTH)
    assert state.json()["connected"] is True and state.json()["email"] == EMAIL
    assert PASSWORD not in state.text and PASSWORD not in response.text


def test_rejected_password_saves_nothing(client, servers, store):
    servers.imap_error = imaplib.IMAP4.error("AUTHENTICATIONFAILED")
    response = put(client)
    assert response.status_code == 400 and response.json()["detail"]["code"] == "mail.bad_credentials"
    assert store.items == {} and PASSWORD not in response.text


def test_imap_ok_but_smtp_rejected_saves_nothing(client, servers, store):
    servers.smtp_error = smtplib.SMTPAuthenticationError(535, b"bad")
    assert put(client).json()["detail"]["code"] == "mail.bad_credentials"
    assert store.items == {}


def test_no_network_is_unreachable_not_a_bad_password(client, servers, store):
    servers.imap_error = OSError("down")
    response = put(client)
    assert response.status_code == 502 and response.json()["detail"]["code"] == "mail.unreachable"
    assert store.items == {}


@pytest.mark.parametrize("email,password", [("student", PASSWORD), ("a@b", PASSWORD), (EMAIL, "  ")])
def test_bad_input_never_reaches_google(client, servers, email, password):
    assert put(client, email, password).status_code == 400
    assert servers.log == []


def test_without_a_credential_store_nothing_is_tried(client, servers, store):
    keyring.set_keyring(keyring.backends.fail.Keyring())
    response = put(client)
    assert response.status_code == 503 and response.json()["detail"]["code"] == "mail.store_unavailable"
    assert servers.log == []


def test_disconnect_deletes_the_entry(client, servers, store):
    put(client)
    assert client.delete("/api/mail", headers=AUTH).json() == {"connected": False}
    assert store.items == {}
    assert client.get("/api/mail", headers=AUTH).json()["connected"] is False


def test_messages_follow_the_panel_language(client, servers):
    servers.imap_error = imaplib.IMAP4.error("x")
    es = put(client, lang="es").json()["detail"]["message"]
    en = put(client, lang="en").json()["detail"]["message"]
    assert es == catalog("es")["mail.bad_credentials"] != en == catalog("en")["mail.bad_credentials"]
