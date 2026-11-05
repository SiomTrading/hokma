"""Test helpers: expected texts come from the message catalogs, never from handwritten strings."""
from pathlib import Path

from hokma.i18n import t

INSTALLER_MESSAGES = Path(__file__).resolve().parents[2] / "installer" / "messages"
MARK = "\0"


def installer_message(key: str, *params: object, lang: str = "es") -> str:
    """Text of an installer message (installer/messages/<lang>.properties) with {1}, {2}... filled in."""
    for line in (INSTALLER_MESSAGES / f"{lang}.properties").read_text(encoding="utf-8").splitlines():
        name, _, text = line.partition("=")
        if name == key:
            for n, value in enumerate(params, 1):
                text = text.replace("{" + str(n) + "}", str(value))
            return text
    raise KeyError(key)


def doctor_line(tag: str, name_key: str) -> str:
    """`[ok]   <check name>` exactly as `hokma doctor` prints it. tag is ok, warn or fail."""
    return f"{t('cli.tag.' + tag)} {t(name_key)}"


def before(key: str, *fields: str) -> str:
    """The fixed text of a message that comes before its first placeholder (for messages with variable parts)."""
    return t(key, **{field: MARK for field in fields}).split(MARK)[0]


def installer_before(key: str, count: int = 1) -> str:
    return installer_message(key, *([MARK] * count)).split(MARK)[0]
