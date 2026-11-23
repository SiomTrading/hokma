"""Filing index of a company and the point in time rule: a filing is visible at `as_of` only if it was filed by then."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable


@dataclass(frozen=True)
class FilingRef:
    cik: int
    accession: str
    form: str
    filing_date: date
    report_date: date | None = None
    accepted: datetime | None = None
    primary_document: str = ""
    items: tuple[str, ...] = ()
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"cik": self.cik, "accession": self.accession, "form": self.form, "filing_date": self.filing_date.isoformat(),
                "report_date": self.report_date.isoformat() if self.report_date else None,
                "accepted": self.accepted.isoformat() if self.accepted else None,
                "primary_document": self.primary_document, "items": list(self.items), "description": self.description}


def parse_date(text: Any) -> date | None:
    if not text:
        return None
    try:
        return date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


def parse_accepted(text: Any) -> datetime | None:
    if not text:
        return None
    raw = str(text).replace("Z", "+00:00")
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _columns(block: dict[str, Any]) -> list[dict[str, Any]]:
    keys = list(block.keys())
    count = len(block.get("accessionNumber", []))
    return [{k: (block[k][i] if i < len(block[k]) else None) for k in keys} for i in range(count)]


def parse_submissions(payload: dict[str, Any], *, extra_pages: Iterable[dict[str, Any]] = ()) -> list[FilingRef]:
    """Filing refs of a submissions payload (the recent block plus any older pages), newest first."""
    cik = int(payload.get("cik") or 0)
    blocks = [payload.get("filings", {}).get("recent", {})] + list(extra_pages)
    refs: list[FilingRef] = []
    seen: set[str] = set()
    for block in blocks:
        for row in _columns(block):
            accession = row.get("accessionNumber")
            filed = parse_date(row.get("filingDate"))
            if not accession or filed is None or accession in seen:
                continue
            seen.add(accession)
            items = tuple(i.strip() for i in str(row.get("items") or "").split(",") if i.strip())
            refs.append(FilingRef(cik=cik, accession=accession, form=str(row.get("form") or ""), filing_date=filed,
                                  report_date=parse_date(row.get("reportDate")), accepted=parse_accepted(row.get("acceptanceDateTime")),
                                  primary_document=str(row.get("primaryDocument") or ""), items=items,
                                  description=str(row.get("primaryDocDescription") or "")))
    refs.sort(key=lambda r: (r.filing_date, r.accession), reverse=True)
    return refs


def cutoff(as_of: datetime | date) -> tuple[date, datetime | None]:
    """The cut as a date and, when the caller gave a time, as an aware UTC datetime."""
    if isinstance(as_of, datetime):
        aware = as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
        return aware.astimezone(timezone.utc).date(), aware.astimezone(timezone.utc)
    return as_of, None


def is_visible(ref: FilingRef, as_of: datetime | date) -> bool:
    """True when the SEC had accepted the filing at `as_of`. Uses the acceptance time when both sides have a time."""
    day, moment = cutoff(as_of)
    if moment is not None and ref.accepted is not None:
        return ref.accepted <= moment
    return ref.filing_date <= day


def visible(refs: Iterable[FilingRef], as_of: datetime | date) -> list[FilingRef]:
    return [r for r in refs if is_visible(r, as_of)]


def base_form(form: str) -> str:
    """`10-K/A` to `10-K`."""
    return form.upper().split("/")[0].strip()


def matches_form(ref: FilingRef, form: str | Iterable[str] | None, *, amendments: bool = True) -> bool:
    if form is None:
        return True
    wanted = {f.upper() for f in ([form] if isinstance(form, str) else form)}
    value = ref.form.upper()
    if value in wanted:
        return True
    return amendments and base_form(value) in wanted
