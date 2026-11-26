"""The anonymous sample of the account (SPEC-026 v2, piece 3390e1): a tool for the owner to capture, on his own computer, what Schwab answers
for the account, so the readers of the P&L are tested with REAL answers and not with the documentation's. It is read only: it asks
`GET /trader/v1/accounts?fields=positions` (the answer the P&L reader uses) and keeps the latest message of ACCT_ACTIVITY the Streamer received.

Nothing personal reaches the file: every account number, hash and id (and any text that holds one) is replaced by "XXXX"; the amounts are NOT
touched. Before writing, the text is checked against the real values and the file is not written if one is in it. The file goes to the downloads
folder of the owner (or to the data folder of Hokma when there is none) and the owner sends it himself."""
import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any

from hokma.core.config import hokma_data_dir
from hokma.schwab import accounts, http

MASK = "XXXX"
# Fields that identify an account or a person: exact names or "account/customer/..." followed by Id or Number. NOT accountValue, not an amount.
ID_KEY = re.compile(r"^(hashvalue|displayacctid|(account|acct|customer|correl|client|user|login)\w*(id|number))$", re.IGNORECASE)
LONG_DIGITS = re.compile(r"(?<![\d.])\d{8,}(?![\d.])")           # in the free text of an ACCT_ACTIVITY message: account and order numbers
LEAK = re.compile(r"\"(accountNumber|hashValue|displayAcctId)\"\s*:\s*\"(?!XXXX\")", re.IGNORECASE)


def downloads_dir() -> tuple[Path, bool]:
    """(folder, is_downloads). The downloads folder of the owner (HOKMA_DOWNLOADS_DIR first, for tests), else the diagnostics folder of Hokma."""
    chosen = os.environ.get("HOKMA_DOWNLOADS_DIR")
    for candidate in ([Path(chosen)] if chosen else []) + [Path.home() / "Downloads"]:
        if candidate.is_dir():
            return candidate, True
    return Path(hokma_data_dir()) / "diagnostics", False


def _clean_text(text: str, secrets: list[str], digits: bool) -> str:
    for secret in secrets:
        if secret.isdigit():                                             # an account number is not cut out of a longer number: that one goes with the long numbers
            text = re.sub(rf"(?<!\d){re.escape(secret)}(?!\d)", MASK, text)
        elif secret:
            text = text.replace(secret, MASK)
    return LONG_DIGITS.sub(MASK, text) if digits else text


def anonymize(value: Any, secrets: list[str], *, digits: bool = False, key: str = "") -> Any:
    """The same structure with every identifier replaced by "XXXX". `digits` also replaces the long numbers inside text (for ACCT_ACTIVITY)."""
    if key and ID_KEY.match(key) and not isinstance(value, (dict, list)):
        return MASK
    if isinstance(value, dict):
        return {k: anonymize(v, secrets, digits=digits, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [anonymize(v, secrets, digits=digits, key=key) for v in value]
    if isinstance(value, str):
        return _clean_text(value, secrets, digits)
    return value


def leaks(text: str, secrets: list[str]) -> str | None:
    """What the file must not carry, or None."""
    for secret in secrets:
        if secret and secret in text:
            return "an account number or hash"
    return "an account identifier" if LEAK.search(text) else None


def build(raw_accounts: Any, activity: dict | None, secrets: list[str], now: dt.datetime | None = None) -> str:
    """The text of the file from the raw answer and the latest activity message. Raises RuntimeError when anything personal would be in it."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if activity is not None:
        activity = json.loads(json.dumps(activity, default=str))
        if isinstance(activity.get("entry"), dict) and "1" in activity["entry"]:           # field 1 of ACCT_ACTIVITY is the account
            activity["entry"]["1"] = MASK
    document = {
        "kind": "schwab-account-sample", "provisional": False, "generated_at": now.isoformat(),
        "note": "Anonymous: account numbers, hashes and ids are XXXX. The amounts are as Schwab sent them.",
        "accounts_fields_positions": anonymize(raw_accounts, secrets),
        "account_activity_last": None if activity is None else anonymize(activity, secrets, digits=True),
    }
    text = json.dumps(document, indent=1, ensure_ascii=False)
    found = leaks(text, secrets)
    if found:
        raise RuntimeError(f"the sample would have saved {found}: nothing was written")
    return text


def capture(activity: dict | None = None, folder: Path | None = None, now: dt.datetime | None = None) -> dict[str, Any]:
    """Blocking (a thread): ask Schwab, anonymize and save. Returns {ok, file, folder_is_downloads, accounts, positions, activity}."""
    accounts.refresh()
    raw = http.get(f"{http.TRADER}/accounts", {"fields": "positions"})
    secrets = [v for v in accounts.sensitive_values() if len(v) >= 4]
    now = now or dt.datetime.now(dt.timezone.utc)
    text = build(raw, activity, secrets, now)
    target, is_downloads = (folder, True) if folder is not None else downloads_dir()
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"schwab-account-sample-{now:%Y%m%dT%H%M%SZ}.json"
    path.write_text(text, encoding="utf-8")
    rows = raw if isinstance(raw, list) else []
    positions = sum(len((r.get("securitiesAccount") or {}).get("positions") or []) for r in rows if isinstance(r, dict))
    return {"ok": True, "file": str(path), "folder_is_downloads": is_downloads, "accounts": len(rows), "positions": positions, "activity": activity is not None}
