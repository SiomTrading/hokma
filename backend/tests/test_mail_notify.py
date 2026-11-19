"""C7-2c: notices by email. Only to the connected address, only what the owner chose (per routine and the Telegram
fallback), a failing mail never breaks the routine or the Telegram queue. Gmail and Telegram are fakes."""
import json
import smtplib
from types import SimpleNamespace

import keyring
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.mail import gmail, notify
from hokma.mail.api import router
from hokma.rpc.ws import SESSION_TOKEN
from live_support import MemoryKeyring
from test_telegram_glue import aio, env, linked  # noqa: F401  (fixtures and helpers)
from test_telegram_outbox import break_telegram

AUTH = {"X-Hokma-Token": SESSION_TOKEN}
ME = "student@gmail.com"


class Smtp:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, host, port, **kw):
        outer = self

        class Server:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def login(self, user, password):
                if outer.fail:
                    raise smtplib.SMTPException("down")

            def send_message(self, message):
                outer.sent.append(message)

        return Server()


@pytest.fixture
def smtp(env, monkeypatch):
    keyring.set_keyring(MemoryKeyring())
    fake = Smtp()
    monkeypatch.setattr(smtplib, "SMTP_SSL", fake)
    gmail.KeyStore().set(gmail.KEY, json.dumps({"email": ME, "password": "x"}))
    return fake


def job(env):
    with env.db.write() as u:
        return u.scheduled_jobs.create("Briefing", "hokma", "p", "daily", "07:00", "UTC")


def done(text="the result"):
    return SimpleNamespace(status="done", detail=text, denied=0)


@aio
async def test_a_routine_marked_for_mail_also_leaves_a_mail_to_the_own_account(env, smtp):
    await linked(env)
    j = job(env)
    notify.save(False, [j.id])
    env.glue.notify_job(j, done())
    await env.glue.flush()
    for task in list(env.glue._tasks):
        await task
    assert len(smtp.sent) == 1 and smtp.sent[0]["To"] == ME == smtp.sent[0]["From"]
    assert "Briefing" in smtp.sent[0]["Subject"] and "the result" in smtp.sent[0].get_content()
    assert [m["text"] for m in env.app.bot.sent][0].startswith("Routine finished: Briefing")   # Telegram still gets it


@aio
async def test_a_routine_not_marked_sends_no_mail(env, smtp):
    await linked(env)
    j = job(env)
    notify.save(True, [])
    env.glue.notify_job(j, done())
    for task in list(env.glue._tasks):
        await task
    assert smtp.sent == []


@aio
async def test_without_telegram_the_marked_routine_still_mails(env, smtp):
    j = job(env)
    notify.save(False, [j.id])
    env.glue.notify_job(j, done())
    for task in list(env.glue._tasks):
        await task
    assert len(smtp.sent) == 1


@aio
async def test_a_failing_mail_does_not_break_the_routine_notice(env, smtp):
    await linked(env)
    j = job(env)
    notify.save(False, [j.id])
    smtp.fail = True
    env.glue.notify_job(j, done())
    for task in list(env.glue._tasks):
        await task
    await env.glue.flush()
    assert smtp.sent == [] and len(env.app.bot.sent) >= 1


@aio
async def test_telegram_failure_goes_to_mail_once_when_the_fallback_is_on(env, smtp):
    await linked(env)
    notify.save(True, [])
    real = break_telegram(env.app.bot)
    await env.glue.deliver("important")
    await env.glue.flush()                                            # a retry that fails again
    assert len(smtp.sent) == 1 and "important" in smtp.sent[0].get_content() and smtp.sent[0]["To"] == ME
    env.app.bot.send_message = real
    assert await env.glue.flush() == 1


@aio
async def test_telegram_failure_sends_no_mail_when_the_fallback_is_off(env, smtp):
    await linked(env)
    break_telegram(env.app.bot)
    await env.glue.deliver("important")
    assert smtp.sent == []


def test_without_gmail_nothing_is_sent_and_the_test_says_so(env, monkeypatch):
    keyring.set_keyring(MemoryKeyring())
    assert notify.fallback("x") is False
    with pytest.raises(gmail.MailError) as error:
        notify.send_to_self("s", "b")
    assert error.value.code == "mail.not_connected"


@pytest.fixture
def client(env):
    app = FastAPI()
    app.include_router(router)
    app.state.db = env.db
    return TestClient(app)


def test_routes_need_the_token(client):
    assert client.get("/api/mail/notify").status_code == 401
    assert client.post("/api/mail/notify/test").status_code == 401


def test_settings_roundtrip_and_the_test_mail_goes_to_the_own_address(client, env, smtp):
    j = job(env)
    first = client.get("/api/mail/notify", headers=AUTH).json()
    assert first == {"connected": True, "email": ME, "fallback": False, "jobs": [{"id": j.id, "name": "Briefing", "mail": False}]}
    saved = client.put("/api/mail/notify", json={"fallback": True, "jobs": [j.id]}, headers=AUTH).json()
    assert saved["fallback"] is True and saved["jobs"][0]["mail"] is True
    assert client.post("/api/mail/notify/test", headers=AUTH).json() == {"sent": True}
    assert smtp.sent[0]["To"] == ME


def test_not_connected_state_and_save_refused(client, env):
    keyring.set_keyring(MemoryKeyring())
    assert client.get("/api/mail/notify", headers=AUTH).json()["connected"] is False
    assert client.put("/api/mail/notify", json={"fallback": True, "jobs": []}, headers=AUTH).status_code == 409
    assert client.post("/api/mail/notify/test", headers=AUTH).status_code == 409


def test_a_failed_test_mail_is_a_translated_error(client, env, smtp):
    smtp.fail = True
    response = client.post("/api/mail/notify/test", headers={**AUTH, "Accept-Language": "es"})
    assert response.status_code == 502 and response.json()["detail"]["code"] == "mail.unreachable"
