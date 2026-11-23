"""Test support: a transport that replays recorded provider responses. No network, no keys, no account data."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hokma.data.sec.client import SecClient
from hokma.data.sec.transport import TransportError

FIXTURES = Path(__file__).parent / "fixtures"
IDENTITY = "Hokma Tests tests@example.invalid"


class RecordedTransport:
    """Answers from files in `fixtures/`. A missing file is a not_found error, like the SEC would answer."""

    def __init__(self, root: Path = FIXTURES) -> None:
        self.root = root
        self.calls: list[tuple[str, Any]] = []

    def _json(self, name: str) -> dict[str, Any]:
        path = self.root / name
        if not path.exists():
            raise TransportError("not_found", name)
        return json.loads(path.read_text(encoding="utf-8"))

    def submissions(self, cik: int) -> dict[str, Any]:
        self.calls.append(("submissions", cik))
        return self._json(f"submissions_{int(cik)}.json")

    def submissions_file(self, name: str) -> dict[str, Any]:
        self.calls.append(("submissions_file", name))
        return self._json(name)

    def company_tickers(self) -> dict[str, Any]:
        self.calls.append(("company_tickers", None))
        return self._json("company_tickers.json")

    def company_facts(self, cik: int) -> dict[str, Any]:
        self.calls.append(("company_facts", cik))
        return self._json(f"facts_{int(cik)}.json")

    def submission_text(self, cik: int, accession: str) -> str:
        self.calls.append(("submission_text", accession))
        path = self.root / "sgml" / f"{accession}.txt"
        if not path.exists():
            raise TransportError("not_found", accession)
        return path.read_text(encoding="utf-8")

    def current_feed(self, form: str, count: int) -> bytes:
        self.calls.append(("current_feed", form))
        path = self.root / "current_feed.xml"
        if not path.exists():
            raise TransportError("not_found", "current_feed.xml")
        return path.read_bytes()

    def full_text_search(self, query: str, *, forms: tuple[str, ...], cik: int | None, start: str | None, end: str | None) -> dict[str, Any]:
        self.calls.append(("full_text_search", query, forms, cik, start, end))
        return self._json("efts.json")


class DownTransport:
    """A provider that does not answer."""

    def __getattr__(self, name: str) -> Any:
        def fail(*args: Any, **kwargs: Any) -> Any:
            raise TransportError("network", "connection refused")
        return fail


def make_client(transport: Any = None, *, identity: str | None = IDENTITY) -> SecClient:
    return SecClient(transport or RecordedTransport(), identity_reader=lambda: identity)
