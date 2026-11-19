"""Notices by email (C7-2c): the owner's own Gmail, over the connection of C7-2a. Hokma sends them, not the agent, and
only to the connected address, so there is no permission to ask each time: the owner chose it once, per routine
(`jobs`) and for the Telegram fallback (`fallback`), in Settings > Email. Choices live in `mail_notify.json`."""
import json
import logging
import smtplib
from email.message import EmailMessage

from hokma.core.config import hokma_data_dir
from hokma.core.tls import outbound_context
from hokma.i18n import t
from hokma.mail import gmail

log = logging.getLogger("hokma.mail")


def _file():
    return hokma_data_dir() / "mail_notify.json"


def load() -> dict:
    try:
        saved = json.loads(_file().read_text(encoding="utf-8"))
        jobs = sorted({int(x) for x in saved.get("jobs", [])})
        return {"fallback": saved.get("fallback") is True, "jobs": jobs}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"fallback": False, "jobs": []}


def save(fallback: bool, jobs: list[int]) -> dict:
    choice = {"fallback": bool(fallback), "jobs": sorted({int(x) for x in jobs})}
    _file().write_text(json.dumps(choice), encoding="utf-8")
    return choice


def send_to_self(subject: str, body: str) -> None:
    """One mail from the connected address to itself. Raises gmail.MailError (not_connected, unreachable)."""
    saved = gmail.credentials()
    if saved is None:
        raise gmail.MailError("mail.not_connected", 409)
    email, password = saved
    message = EmailMessage()
    message["From"] = message["To"] = email
    message["Subject"] = " ".join(subject.split())[:150] or "Hokma"
    message.set_content(body)
    try:
        with smtplib.SMTP_SSL(gmail.SMTP_HOST, gmail.SMTP_PORT, timeout=gmail.TIMEOUT, context=outbound_context()) as server:
            server.login(email, password)
            server.send_message(message)
    except (smtplib.SMTPException, OSError):
        raise gmail.MailError("mail.unreachable") from None


def _try(subject: str, body: str) -> bool:
    """A notice that cannot go out is logged and dropped: it never breaks the routine or the Telegram queue."""
    try:
        send_to_self(subject, body)
        return True
    except gmail.MailError as error:
        log.warning("mail notice not sent: %s", error.code)
        return False


def for_job(job_id: int, text: str) -> bool:
    """The routine's notice (the same text Telegram gets), when its owner turned the mail on for that routine."""
    if job_id not in load()["jobs"]:
        return False
    return _try(text.strip().splitlines()[0] if text.strip() else "Hokma", text)


def fallback(text: str) -> bool:
    """Telegram could not take this message: it goes to the mail when the owner turned that on."""
    if not load()["fallback"]:
        return False
    return _try(t("mail.notify.fallback_subject"), text)
