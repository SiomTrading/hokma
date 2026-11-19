"""Gmail connection of the student: an app password over IMAP (imap.gmail.com:993) and SMTP (smtp.gmail.com:465), with
the standard library only, so it works with any engine. Nothing is saved until both servers accepted the login. The
address and the password live only in the system credential store (`KeyStore`, entry `gmail`, one JSON value)."""
import imaplib
import json
import re
import smtplib
import socket

from hokma.connections.store import KeyStore
from hokma.core.tls import outbound_context

KEY = "gmail"
IMAP_HOST, IMAP_PORT = "imap.gmail.com", 993
SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 465
APP_PASSWORDS_URL = "https://myaccount.google.com/apppasswords"
TIMEOUT = 15
_ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class MailError(Exception):
    """`code` is a catalog key (`mail.*`)."""

    def __init__(self, code: str, status: int = 502):
        super().__init__(code)
        self.code, self.status = code, status


def _imap(email: str, password: str) -> None:
    box = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=TIMEOUT, ssl_context=outbound_context())
    try:
        box.login(email, password)
    finally:
        try:
            box.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def _smtp(email: str, password: str) -> None:
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=TIMEOUT, context=outbound_context()) as server:
        server.login(email, password)


def check(email: str, password: str) -> None:
    """Logs in to both servers. Raises MailError: bad_address, bad_credentials (rejected) or unreachable."""
    try:
        _imap(email, password)
        _smtp(email, password)
    except (imaplib.IMAP4.error, smtplib.SMTPAuthenticationError):
        raise MailError("mail.bad_credentials", 400) from None
    except (smtplib.SMTPException, OSError, socket.timeout):
        raise MailError("mail.unreachable") from None


def connect(email: str, password: str) -> str:
    """Tests, then saves. The password is shown by Google in groups of four: the spaces are dropped."""
    email, password = (email or "").strip(), re.sub(r"\s+", "", password or "")
    if not _ADDRESS.match(email):
        raise MailError("mail.bad_address", 400)
    if not password:
        raise MailError("mail.bad_credentials", 400)
    store = KeyStore()
    if not store.available():
        raise MailError("mail.store_unavailable", 503)
    check(email, password)
    store.set(KEY, json.dumps({"email": email, "password": password}))
    return email


def credentials() -> tuple[str, str] | None:
    """(address, app password) for the mail tools of the next piece, or None when Gmail is not connected."""
    try:
        saved = json.loads(KeyStore().get(KEY) or "")
        return saved["email"], saved["password"]
    except (ValueError, KeyError, TypeError):
        return None


def account() -> str | None:
    saved = credentials()
    return saved[0] if saved else None


def disconnect() -> None:
    KeyStore().delete(KEY)
