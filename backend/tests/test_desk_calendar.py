"""The Calendar panel of a desk (SPEC-028, delivery 7, task 3340): the week of earnings from the one data connection, asked a day at a time,
the three filters, the market holidays, the missing connection and the chief's tool (C1 to C6 of the spec, the part the engine decides)."""
import json
from datetime import date

import pytest
from desks_support import desks_db, db, env, make_desk  # noqa: F401  (fixtures)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.desks import earnings, service
from hokma.desks.api import router as desks_router
from hokma.kit import company_tools, hokma_tools
from hokma.rpc.ws import SESSION_TOKEN

WEEK = ("2026-11-23", "2026-11-27")          # Monday to Friday; the 26th is Thanksgiving


@pytest.fixture
def client(desks_db):
    app = FastAPI()
    app.include_router(desks_router)
    with TestClient(app) as c:
        c.headers.update({"X-Hokma-Token": SESSION_TOKEN})
        yield c


def report(symbol, day, hour="amc", est=1.0, actual=None, revenue=1000.0):
    return {"symbol": symbol, "date": day, "hour": hour, "eps_estimate": est, "eps_actual": actual, "revenue_estimate": revenue, "revenue_actual": None}


class Provider:
    """Stands for earnings_calendar: one list of reports, cut at `cap` per call like the real one, and a record of what was asked."""

    def __init__(self, rows, cap=100):
        self.rows, self.cap, self.calls = rows, cap, []

    def __call__(self, desde=None, hasta=None, symbol=None, desk_id=None):
        self.calls.append((desde, hasta, symbol))
        found = [r for r in self.rows if desde <= r["date"] <= hasta and (symbol is None or r["symbol"] == symbol)]
        if not found:
            raise company_tools.NoRows("nothing")
        return {"source": "Finnhub", "fetched_at": "x", "count": min(len(found), self.cap), "truncated": len(found) > self.cap, "earnings": found[: self.cap]}


@pytest.fixture(autouse=True)
def no_broker(monkeypatch):
    earnings._hours.clear()
    earnings._failed.clear()
    monkeypatch.setattr(earnings, "_holiday", lambda day: None)


def week(client, desk, filter_="all"):
    return client.get(f"/api/desks/{desk['id']}/earnings-calendar", params={"from": WEEK[0], "to": WEEK[1], "filter": filter_})


def test_c1_the_week_is_asked_a_day_at_a_time_and_each_day_has_its_reports(client, desks_db, monkeypatch):
    provider = Provider([report("AAPL", "2026-11-23", "bmo"), report("MSFT", "2026-11-25"), report("NVDA", "2026-11-25", "amc")])
    monkeypatch.setattr(company_tools, "earnings_calendar", provider)
    body = week(client, make_desk(desks_db)).json()
    assert [c[:2] for c in provider.calls] == [(d, d) for d in ("2026-11-23", "2026-11-24", "2026-11-25", "2026-11-26", "2026-11-27")]
    assert [d["date"] for d in body["days"]] == ["2026-11-23", "2026-11-24", "2026-11-25", "2026-11-26", "2026-11-27"]
    assert [(r["symbol"], r["hour"]) for r in body["days"][2]["rows"]] == [("MSFT", "amc"), ("NVDA", "amc")]
    assert body["days"][1]["rows"] == [] and body["days"][1]["error"] is None          # a day with no report is empty, not an error
    assert body["connected"] is True and body["source"] == "Finnhub"


def test_a_cut_day_says_so_and_the_other_days_are_whole(client, desks_db, monkeypatch):
    rows = [report(f"T{n}", "2026-11-24") for n in range(130)] + [report("AAPL", "2026-11-25")]
    monkeypatch.setattr(company_tools, "earnings_calendar", Provider(rows))
    body = week(client, make_desk(desks_db)).json()
    assert body["days"][1]["truncated"] is True and len(body["days"][1]["rows"]) == 100
    assert body["days"][2]["truncated"] is False


def test_c2_a_published_report_has_its_surprise_and_an_unpublished_one_has_none(client, desks_db, monkeypatch):
    monkeypatch.setattr(company_tools, "earnings_calendar", Provider([
        report("AAPL", "2026-11-23", est=2.0, actual=2.2), report("MSFT", "2026-11-23", est=1.0, actual=0.8), report("NVDA", "2026-11-23", est=0.0, actual=0.1),
        report("AMD", "2026-11-23", est=1.0)]))
    rows = {r["symbol"]: r for r in week(client, make_desk(desks_db)).json()["days"][0]["rows"]}
    assert rows["AAPL"]["surprise_pct"] == 10.0 and rows["MSFT"]["surprise_pct"] == -20.0
    assert rows["NVDA"]["surprise_pct"] is None and rows["AMD"]["surprise_pct"] is None and rows["AMD"]["eps_actual"] is None
    assert earnings.surprise_pct(-2.0, -1.0) == 50.0                        # against the size of the estimate, so a loss that shrinks is a beat


def test_c3_my_list_keeps_only_the_assets_of_the_desk_and_fills_a_cut_day(client, desks_db, monkeypatch):
    desk = make_desk(desks_db)
    service.set_assets(desk["id"], ["AAPL", "KO"], db=desks_db)
    rows = [report(f"T{n}", "2026-11-24") for n in range(130)] + [report("AAPL", "2026-11-24"), report("KO", "2026-11-24"), report("MSFT", "2026-11-25")]
    provider = Provider(rows)
    monkeypatch.setattr(company_tools, "earnings_calendar", provider)
    body = week(client, desk, "list").json()
    assert sorted(r["symbol"] for r in body["days"][1]["rows"]) == ["AAPL", "KO"]       # both were past the cut and came by their own call
    assert body["days"][2]["rows"] == [] and body["days"][1]["truncated"] is False
    assert ("2026-11-24", "2026-11-24", "KO") in provider.calls


def test_c4_without_a_connection_the_answer_says_so(client, desks_db, monkeypatch):
    def missing(*a, **k):
        raise company_tools.NotConnected("connect it")
    monkeypatch.setattr(company_tools, "earnings_calendar", missing)
    body = week(client, make_desk(desks_db)).json()
    assert body["connected"] is False and body["days"] == []


def test_a_refusal_of_the_provider_shows_on_the_day_and_is_not_an_empty_day(client, desks_db, monkeypatch):
    def refuse(*a, **k):
        raise LookupError("the plan does not have this")
    monkeypatch.setattr(company_tools, "earnings_calendar", refuse)
    body = week(client, make_desk(desks_db)).json()
    assert body["connected"] is True and body["days"][0]["error"] == "the plan does not have this" and body["days"][0]["rows"] == []


def test_the_liquid_filter_reads_the_biggest_first_and_counts_what_it_could_not_read(client, desks_db, monkeypatch):
    monkeypatch.setattr(company_tools, "earnings_calendar", Provider([
        report("AAPL", "2026-11-23", revenue=9e9), report("MSFT", "2026-11-23", revenue=8e9), report("TINY", "2026-11-23", revenue=1e3), report("ODD", "2026-11-23", revenue=5e9)]))
    monkeypatch.setattr(earnings, "_liquid", lambda symbol, desk_id: {"AAPL": True, "MSFT": False, "ODD": None, "TINY": True}[symbol])
    body = week(client, make_desk(desks_db), "liquid").json()
    assert sorted(r["symbol"] for r in body["days"][0]["rows"]) == ["AAPL", "TINY"]
    assert body["liquid"] == {"checked": 3, "unchecked": 1}                    # ODD had no readable options: neither liquid nor illiquid
    monkeypatch.setattr(earnings, "LIQUID_CHECKED_MAX", 2)
    body = week(client, make_desk(desks_db), "liquid").json()
    assert sorted(r["symbol"] for r in body["days"][0]["rows"]) == ["AAPL"] and body["liquid"] == {"checked": 2, "unchecked": 2}


def test_liquid_reads_the_chain_of_the_nearest_expiration(monkeypatch):
    from hokma.market import options

    def cell(bid, ask, oi):
        return {"symbol": "X", "bid": bid, "ask": ask, "open_interest": oi}
    monkeypatch.setattr(options, "expirations", lambda s, d=None: {"expirations": [{"date": "2026-11-27", "days": 0}, {"date": "2026-12-04", "days": 9}]})
    asked = []
    good = {"expirations": [{"rows": [{"strike": 100, "call": cell(1.0, 1.05, 500), "put": cell(1.0, 1.04, 800)}]}]}
    wide = {"expirations": [{"rows": [{"strike": 100, "call": cell(1.0, 2.0, 500), "put": cell(0.5, 1.5, 5)}]}]}
    monkeypatch.setattr(options, "chain", lambda s, dates, strikes=None, desk_id=None: (asked.append(dates), good if s == "GOOD" else wide)[1])
    assert earnings._liquid("GOOD", 1) is True and earnings._liquid("WIDE", 1) is False
    assert asked[0] == ["2026-12-04"]                                          # today's expiration is not the one that tells


def test_liquid_is_unknown_when_the_options_cannot_be_read(monkeypatch):
    from hokma.market import options

    def down(*a, **k):
        raise options.OptionsError("no_connection", 400, "connect one")
    monkeypatch.setattr(options, "expirations", down)
    assert earnings._liquid("AAPL", 1) is None


def test_the_holidays_are_marked_when_the_broker_knows_them_and_unknown_when_it_does_not(client, desks_db, monkeypatch):
    monkeypatch.setattr(company_tools, "earnings_calendar", Provider([]))
    unknown = week(client, make_desk(desks_db)).json()
    assert unknown["holidays_known"] is False and all(d["holiday"] is None for d in unknown["days"])
    monkeypatch.setattr(earnings, "_holiday", lambda day: day.isoformat() == "2026-11-26")
    known = week(client, make_desk(desks_db)).json()
    assert known["holidays_known"] is True and [d["date"] for d in known["days"] if d["holiday"]] == ["2026-11-26"]


def test_the_holiday_reader_follows_the_market_hours_of_the_broker(monkeypatch):
    from hokma.schwab import endpoints, models

    monkeypatch.undo()
    answers = {"2026-11-26": {"equity": {"EQ": {"isOpen": False, "sessionHours": None}}},
               "2026-11-25": {"equity": {"EQ": {"isOpen": True, "sessionHours": {"regularMarket": [{"start": "x", "end": "y"}]}}}}}
    monkeypatch.setattr(endpoints, "market_hours", lambda market, date=None, past_ok=False: answers[date])
    monkeypatch.setattr(models, "dump", lambda value: value)
    earnings._hours.clear()
    assert earnings._holiday(date(2026, 11, 26)) is True and earnings._holiday(date(2026, 11, 25)) is False

    def down(*a, **k):
        raise RuntimeError("no session")
    monkeypatch.setattr(endpoints, "market_hours", down)
    assert earnings._holiday(date(2026, 11, 24)) is None


def test_a_bad_range_filter_or_desk_is_refused(client, desks_db, monkeypatch):
    monkeypatch.setattr(company_tools, "earnings_calendar", Provider([]))
    desk = make_desk(desks_db)
    path = f"/api/desks/{desk['id']}/earnings-calendar"
    assert client.get(path, params={"from": "nope", "to": WEEK[1]}).status_code == 400
    assert client.get(path, params={"from": WEEK[1], "to": WEEK[0]}).status_code == 400
    assert client.get(path, params={"from": "2026-11-01", "to": "2026-12-31"}).status_code == 400
    assert client.get(path, params={"from": WEEK[0], "to": WEEK[1], "filter": "pead"}).status_code == 400
    assert client.get("/api/desks/9999/earnings-calendar", params={"from": WEEK[0], "to": WEEK[1]}).status_code == 404
    anonymous = TestClient(client.app)
    assert anonymous.get(path, params={"from": WEEK[0], "to": WEEK[1]}).status_code == 401


def test_the_weekend_is_never_asked(client, desks_db, monkeypatch):
    provider = Provider([])
    monkeypatch.setattr(company_tools, "earnings_calendar", provider)
    desk = make_desk(desks_db)
    body = client.get(f"/api/desks/{desk['id']}/earnings-calendar", params={"from": "2026-11-27", "to": "2026-11-30"}).json()
    assert [d["date"] for d in body["days"]] == ["2026-11-27", "2026-11-30"]


def test_the_pead_marks_are_not_in_the_answer(client, desks_db, monkeypatch):
    monkeypatch.setattr(company_tools, "earnings_calendar", Provider([report("AAPL", "2026-11-23")]))
    row = week(client, make_desk(desks_db)).json()["days"][0]["rows"][0]
    assert not {"candidate", "discarded", "pead"} & set(row)


def test_c6_the_chief_puts_the_calendar_on_the_screen_and_reads_the_week(client, desks_db, monkeypatch):
    monkeypatch.setattr(company_tools, "earnings_calendar", Provider([report("AAPL", "2026-11-25")]))
    desk = make_desk(desks_db)
    assert hokma_tools.TOOLS["desk_calendar_show"][4] is True
    text, error = hokma_tools.call("desk_calendar_show", {"desk_id": desk["id"], "week": "2026-11-25"}, hokma_tools.Caller({}))
    out = json.loads(text)
    assert not error and "calendar" in out["panels"] and out["week"]["from"] == "2026-11-23"
    assert [r["symbol"] for r in out["week"]["days"][2]["rows"]] == ["AAPL"]
    assert client.get(f"/api/desks/{desk['id']}").json()["panels"] == out["panels"]          # the screen reads the same list
    again, error = hokma_tools.call("desk_calendar_show", {"desk_id": desk["id"]}, hokma_tools.Caller({}))
    assert not error and json.loads(again)["panels"] == out["panels"]                          # a second call does not repeat the panel
    bad, error = hokma_tools.call("desk_calendar_show", {"desk_id": desk["id"], "week": "soon"}, hokma_tools.Caller({}))
    assert error and "YYYY-MM-DD" in bad


def test_the_calendar_panel_can_be_added_and_taken_off_by_hand(client, desks_db):
    desk = make_desk(desks_db)
    assert client.post(f"/api/desks/{desk['id']}/panels", json={"id": "calendar"}).status_code == 201
    assert "calendar" in client.get(f"/api/desks/{desk['id']}").json()["panels"]
    assert client.delete(f"/api/desks/{desk['id']}/panels/calendar").status_code == 200
    assert "calendar" not in client.get(f"/api/desks/{desk['id']}").json()["panels"]


def test_a_missing_connection_is_told_apart_from_an_empty_answer(monkeypatch):
    class Connections:
        def order(self, use):
            return ["finnhub"]

        def is_enabled(self, p):
            return False

        def has_key(self, p):
            return False
    monkeypatch.setattr(company_tools, "_connections", lambda: Connections())
    company_tools.reset()
    with pytest.raises(company_tools.NotConnected):
        company_tools.earnings_calendar("2026-11-23", "2026-11-23")


def test_a_broker_that_is_down_is_not_asked_again_for_a_while_and_is_asked_again_after(monkeypatch):
    from hokma.schwab import endpoints, models

    monkeypatch.undo()
    calls = []

    def down(*a, **k):
        calls.append(1)
        raise RuntimeError("no session")
    monkeypatch.setattr(endpoints, "market_hours", down)
    monkeypatch.setattr(models, "dump", lambda value: value)
    earnings._hours.clear()
    earnings._failed.clear()
    day = date(2026, 11, 24)
    assert earnings._holiday(day) is None and earnings._holiday(day) is None and len(calls) == 1
    earnings._failed[day.isoformat()] = 0.0                                              # the pause ended
    assert earnings._holiday(day) is None and len(calls) == 2
    earnings._failed.clear()


def test_a_week_that_is_not_a_day_adds_no_panel(client, desks_db):
    desk = make_desk(desks_db)
    before = client.get(f"/api/desks/{desk['id']}").json()["panels"]
    text, error = hokma_tools.call("desk_calendar_show", {"desk_id": desk["id"], "week": "soon"}, hokma_tools.Caller({}))
    assert error and "YYYY-MM-DD" in text and client.get(f"/api/desks/{desk['id']}").json()["panels"] == before
