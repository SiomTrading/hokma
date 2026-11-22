"""The hours of a market as the PROVIDER says them (SPEC-016 3.4). Hokma writes no schedule of its own: it keeps the pieces of the day that
the provider sent, with the names it sent, and answers "open" from them. A provider that says nothing gives `None`: the chart is not shaded
and `market_open` is "unknown" instead of a guess.

For Schwab the source is `GET /marketdata/v1/markets/{market}` (`sessionHours`: `preMarket`, `regularMarket`, `postMarket`, and whatever new
piece it adds, for example a night one, is kept with its name).
"""
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Segment:
    name: str                 # as the provider named it
    start: int                # epoch seconds
    end: int


@dataclass(frozen=True)
class ProviderHours:
    market: str
    product: str | None
    date: str | None
    is_open: bool | None      # the flag the provider sends; a holiday says false
    segments: tuple[Segment, ...]

    def segment_at(self, ts: float) -> Segment | None:
        return next((s for s in self.segments if s.start <= ts < s.end), None)

    def open_at(self, ts: float) -> bool | None:
        """Open when a piece of the provider's day holds the instant; with no pieces, the provider's own flag (None when it has none)."""
        if self.segments:
            return self.segment_at(ts) is not None
        return self.is_open

    def last_end(self) -> int | None:
        return max((s.end for s in self.segments), default=None)

    def to_wire(self) -> list[dict]:
        return [{"name": s.name, "start": s.start, "end": s.end} for s in self.segments]


def _stamp(value) -> int | None:
    try:
        return int(datetime.fromisoformat(str(value)).timestamp())
    except (TypeError, ValueError):
        return None


def parse_schwab_hours(payload: Mapping, market: str, product: str | None = None) -> ProviderHours | None:
    """The hours of one market from the dump of `endpoints.market_hours`. `product` None takes the first one the market lists."""
    section = payload.get(market) if isinstance(payload, Mapping) else None
    if not isinstance(section, Mapping) or not section:
        return None
    key = product if product in section else next(iter(section))
    entry = section.get(key)
    if not isinstance(entry, Mapping):
        return None
    hours = entry.get("sessionHours")
    segments: list[Segment] = []
    for name, spans in (hours.items() if isinstance(hours, Mapping) else ()):
        for span in spans if isinstance(spans, list) else ():
            start, end = _stamp(span.get("start") if isinstance(span, Mapping) else None), _stamp(span.get("end") if isinstance(span, Mapping) else None)
            if start is not None and end is not None and end > start:
                segments.append(Segment(str(name), start, end))
    flag = entry.get("isOpen")
    return ProviderHours(market, key, entry.get("date") if isinstance(entry.get("date"), str) else None,
                         flag if isinstance(flag, bool) else None, tuple(sorted(segments, key=lambda s: s.start)))
