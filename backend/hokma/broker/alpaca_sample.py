"""The anonymous sample of the Alpaca account (SPEC-026 v2, piece 3390e1b): the same tool as the one of Schwab, so the reader of the P&L is tested with REAL
answers and not with the ones of the documentation. It is read only: it asks `GET /v2/account` and `GET /v2/positions` (the two answers the reader uses) and keeps
them as Alpaca sent them.

Nothing personal reaches the file: the account number and the account id are replaced by "XXXX" (and any text that holds one); the amounts are NOT touched.
Before writing, the text is checked against the real values and the file is not written if one is in it. The file goes to the downloads folder of the owner
(or to the data folder of Hokma when there is none) and the owner sends it himself. Paper accounts work the same way."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from hokma.db import Database, get_database
from hokma.schwab.sample import MASK, anonymize, downloads_dir, leaks

ID_FIELDS = ("id", "account_number")                                   # of the account answer; the `asset_id` of a position is the id of the asset, not of the owner


def build(account: dict, positions: list, secrets: list[str], now: dt.datetime | None = None) -> str:
    """The text of the file from the raw answers. Raises RuntimeError when anything personal would be in it."""
    now = now or dt.datetime.now(dt.timezone.utc)
    shown = {k: (MASK if k in ID_FIELDS and v not in (None, "") else v) for k, v in account.items()}
    document = {
        "kind": "alpaca-account-sample", "provisional": False, "generated_at": now.isoformat(),
        "note": "Anonymous: the account number and the account id are XXXX. The amounts are as Alpaca sent them.",
        "account": anonymize(shown, secrets), "positions": anonymize(positions, secrets),
    }
    text = json.dumps(document, indent=1, ensure_ascii=False)
    found = leaks(text, secrets) or ("the account id" if _account_leak(text) else None)
    if found:
        raise RuntimeError(f"the sample would have saved {found}: nothing was written")
    return text


def _account_leak(text: str) -> bool:
    """An `id` or `account_number` of the ACCOUNT that is not XXXX. The `id` of a position or an order is not looked at: only the `account` object is."""
    account = json.loads(text).get("account") or {}
    return any(account.get(k) not in (None, "", MASK) for k in ID_FIELDS)


def capture(db: Database | None = None, folder: Path | None = None, now: dt.datetime | None = None) -> dict[str, Any]:
    """Blocking (a thread): ask Alpaca once, anonymize and save. Returns {ok, file, folder_is_downloads, accounts, positions, activity}."""
    from hokma.broker import alpaca as alpaca_broker
    from hokma.broker import pnl
    from hokma.broker.service import BrokerError
    from hokma.i18n import t

    groups = pnl.alpaca_links_of(db or get_database())
    if not groups:
        raise BrokerError("alpaca_not_connected", 409, text=t("pnl.err.alpaca_sample_none"))
    row_id, (row, _mine) = next(iter(groups.items()))
    if not pnl.alpaca_budget(row_id).admit(0, 1, False):
        raise BrokerError("alpaca_budget", 429, text=t("pnl.err.alpaca_sample_wait"))
    account, positions = alpaca_broker.read_wire(row)
    secrets = [str(account[k]) for k in ID_FIELDS if len(str(account.get(k) or "")) >= 4]
    now = now or dt.datetime.now(dt.timezone.utc)
    text = build(account, positions, secrets, now)
    target, is_downloads = (folder, True) if folder is not None else downloads_dir()
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"alpaca-account-sample-{now:%Y%m%dT%H%M%SZ}.json"
    path.write_text(text, encoding="utf-8")
    return {"ok": True, "file": str(path), "folder_is_downloads": is_downloads, "accounts": 1, "positions": len(positions), "activity": False}
