"""The Today card: the student's choice of what it shows, stored as one validated JSON row, and the data it shows.

No row means the defaults. Everything that comes from outside (calendar, news, the briefing text) is returned as quoted
data with a `notice`, never as an instruction. A source that fails says why in its own section; the rest stays."""
from __future__ import annotations

import json
from typing import Any, Callable

from hokma.db import Database, NotFound, get_database
from hokma.db.timeutil import utcnow
from hokma.i18n import t
from hokma.kit import company_tools as ct
from hokma.kit import today_data
from hokma.market.yahoo import valid_symbol

SECTIONS = ("watchlist", "calendar", "news", "briefing")
MAX_EXTRA, MAX_TOPICS, MAX_NEWS = 30, 10, ct.MAX_NEWS
DEFAULTS: dict[str, Any] = {
    "sections": {name: True for name in SECTIONS}, "extra_symbols": [],
    "calendar": {"countries": ["US"], "min_importance": "medium", "events": []},
    "news": {"limit": 8, "topics": []}, "briefing_job_id": None}


# One literal t(...) per message, so the catalog check can see that every key is used.
_MESSAGES: dict[str, Callable[..., str]] = {
    "shape": lambda **v: t("today.err.shape", **v),
    "unknown_key": lambda **v: t("today.err.unknown_key", **v),
    "sections": lambda **v: t("today.err.sections", **v),
    "symbols": lambda **v: t("today.err.symbols", **v),
    "countries": lambda **v: t("today.err.countries", **v),
    "importance": lambda **v: t("today.err.importance", **v),
    "news_limit": lambda **v: t("today.err.news_limit", **v),
    "topics": lambda **v: t("today.err.topics", **v),
    "job": lambda **v: t("today.err.job", **v),
    "events": lambda **v: t("today.err.events", **v),
    "nothing": lambda **v: t("today.err.nothing", **v)}


class TodayError(ValueError):
    """A request the student can fix; `code` is stable, the message is in their language."""

    def __init__(self, code: str, **values: Any) -> None:
        super().__init__(_MESSAGES[code](**values))
        self.code = code


def _shape(value: Any, allowed: dict, where: str) -> dict:
    if not isinstance(value, dict):
        raise TodayError("shape", where=where)
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise TodayError("unknown_key", keys=", ".join(map(str, unknown)), allowed=", ".join(allowed))
    return {**allowed, **value}


def validate(raw: Any) -> dict:
    """The full config from a partial one (missing keys take the defaults); anything unknown or out of range is refused."""
    cfg = _shape(raw, DEFAULTS, "config")
    sections = _shape(cfg["sections"], DEFAULTS["sections"], "sections")
    if not all(isinstance(v, bool) for v in sections.values()):
        raise TodayError("sections")
    extra = cfg["extra_symbols"]
    if not isinstance(extra, list) or not all(isinstance(s, str) for s in extra):
        raise TodayError("symbols", max=MAX_EXTRA)
    symbols = list(dict.fromkeys(s.strip().upper() for s in extra))
    if len(symbols) > MAX_EXTRA or not all(valid_symbol(s) for s in symbols):
        raise TodayError("symbols", max=MAX_EXTRA)
    calendar = _shape(cfg["calendar"], DEFAULTS["calendar"], "calendar")
    try:
        countries = today_data._countries(calendar["countries"])
    except ValueError:
        raise TodayError("countries") from None
    if calendar["min_importance"] not in ct.IMPACT:
        raise TodayError("importance", options=", ".join(ct.IMPACT))
    try:
        events = today_data._events(calendar["events"])
    except ValueError:
        raise TodayError("events", max=today_data.MAX_EVENTS) from None
    news = _shape(cfg["news"], DEFAULTS["news"], "news")
    limit, topics = news["limit"], news["topics"]
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_NEWS:
        raise TodayError("news_limit", max=MAX_NEWS)
    if not isinstance(topics, list) or len(topics) > MAX_TOPICS or not all(
            isinstance(x, str) and 1 <= len(x.strip()) <= 40 for x in topics):
        raise TodayError("topics", max=MAX_TOPICS)
    job = cfg["briefing_job_id"]
    if job is not None and (isinstance(job, bool) or not isinstance(job, int)):
        raise TodayError("job")
    return {"sections": sections, "extra_symbols": symbols,
            "calendar": {"countries": list(countries), "min_importance": calendar["min_importance"],
                         "events": list(events)},
            "news": {"limit": limit, "topics": [x.strip() for x in topics]}, "briefing_job_id": job}


def get_config(db: Database | None = None) -> dict:
    with (db or get_database()).read() as u:
        row = u.session.one("SELECT config FROM today_config WHERE id = 1")
    return validate(json.loads(row["config"])) if row else validate({})


def save_config(raw: Any, db: Database | None = None) -> dict:
    config = validate(raw)
    with (db or get_database()).write() as u:
        row = u.session.one("SELECT config FROM today_config WHERE id = 1")
        before = json.loads(row["config"]).get("briefing_job_id") if row else None
        if config["briefing_job_id"] is not None and config["briefing_job_id"] != before:
            u.scheduled_jobs.get(config["briefing_job_id"])         # NotFound if the routine does not exist
        values = {"config": json.dumps(config, ensure_ascii=False), "updated_at": utcnow()}
        if u.session.update("today_config", {"id": 1}, values) == 0:
            u.session.insert_row("today_config", {"id": 1, **values})
    return config


def change_config(changes: Any, db: Database | None = None) -> dict:
    """Merge `changes` over the saved config, one level down (a section keeps the keys not mentioned), and save."""
    if not isinstance(changes, dict) or not changes:
        raise TodayError("nothing")
    merged = get_config(db)
    for key, value in changes.items():
        merged[key] = {**merged[key], **value} if isinstance(merged.get(key), dict) and isinstance(value, dict) else value
    return save_config(merged, db)


def last_briefing(config: dict, db: Database | None = None) -> dict | None:
    """The text the briefing routine left in its last task, as quoted data; None while it has not run."""
    job_id = config["briefing_job_id"]
    if job_id is None:
        return None
    with (db or get_database()).read() as u:
        try:
            job = u.scheduled_jobs.get(job_id)
        except NotFound:                               # the routine was deleted: the card still works
            return {"error": t("today.err.job")}
        task = u.tasks.get(job.last_task_id) if job.last_task_id else None
    if task is None or not task.result_summary:
        return None
    return {"text": task.result_summary, "task_id": task.id, "as_of": task.completed_at or task.updated_at, "notice": today_data.NOTICE}


def _section(fn) -> dict:
    try:
        return fn()
    except (LookupError, ValueError) as exc:
        return {"error": str(exc)}


def _watchlist(config: dict) -> dict:
    out = today_data.watchlist_all()
    from hokma.kit import market_tools

    have = {a["symbol"] for a in out["assets"]}
    extra = [s for s in config["extra_symbols"] if s not in have]
    rows = [{**p, "desks": []} for p in market_tools.desk_prices(extra)]
    return {**out, "count": out["count"] + len(rows), "assets": out["assets"] + rows}


def _news(config: dict) -> dict:
    news = config["news"]
    topics = [x.casefold() for x in news["topics"]]
    out = today_data.market_news(limite=MAX_NEWS if topics else news["limit"])
    if topics:
        items = [n for n in out["news"] if any(x in f"{n['headline']} {n['summary']}".casefold() for x in topics)]
        out = {**out, "news": items[:news["limit"]], "count": len(items[:news["limit"]])}
    return out


def today(db: Database | None = None) -> dict:
    """The card: the config and, for each section that is on, its data or the reason it has none."""
    config = get_config(db)
    on = config["sections"]
    calendar, news = config["calendar"], config["news"]
    return {"config": config,
            "watchlist": _section(lambda: _watchlist(config)) if on["watchlist"] else None,
            "calendar": _section(lambda: today_data.economic_calendar(
                paises=calendar["countries"], importancia=calendar["min_importance"],
                eventos=calendar["events"])) if on["calendar"] else None,
            "news": _section(lambda: _news(config)) if on["news"] else None,
            "briefing": last_briefing(config, db) if on["briefing"] else None}
