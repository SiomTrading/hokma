"""Timestamps: strings `YYYY-MM-DD HH:MM:SS` in UTC, the shape SQL CURRENT_TIMESTAMP writes."""
from datetime import UTC, datetime, timedelta

FORMAT = "%Y-%m-%d %H:%M:%S"


def utcnow() -> str:
    return datetime.now(UTC).strftime(FORMAT)


def utc_in(seconds: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).strftime(FORMAT)


def from_db_time(value: str) -> datetime:
    """Aware UTC datetime from a stored value or any ISO 8601 text."""
    text = value.strip().replace("Z", "+00:00")
    if "T" not in text and " " in text:
        text = text.replace(" ", "T", 1)
    parsed = datetime.fromisoformat(text)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def to_db_time(value: "datetime | str | None") -> str | None:
    """Normalise a datetime or ISO text to the stored shape; None stays None."""
    if value is None:
        return None
    if isinstance(value, str):
        value = from_db_time(value)
    elif value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime(FORMAT)
