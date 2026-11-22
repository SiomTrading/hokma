"""The notice to the owner, one place for every broker (SPEC-016 3.8 point 3): Schwab's session notice and the loss of a broker connection use it.

The owner's linked Telegram chat gets it in the Avisos topic through the outbox (the sender delivers it, retrying every minute). With no Telegram
link the mail notice is tried, and it goes only if the owner turned the mail fallback on in Settings. With neither there is nobody to tell and the
notice counts as given. The text never carries a token, an account or an id: broker, reason and time.
"""


def notify_owner(text: str) -> None:
    """Blocking: call it from a thread."""
    from hokma.db import get_database
    from hokma.mail import notify as mail_notify
    from hokma.telegram.glue import enqueue

    with get_database().write() as unit:
        if unit.session.one("SELECT 1 AS x FROM telegram_link WHERE id = 1") is not None:
            enqueue(unit, text)
            return
    mail_notify.fallback(text)
