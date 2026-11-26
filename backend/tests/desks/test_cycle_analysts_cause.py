"""The analysts step of the SWO cycle says why it failed (bug: `error:analysts:CycleError` with no cause).

The step failed because `app_runtime.load` raised `CycleError("no_data")` (a data source was not connected) and the guard of the graph kept only the
class name. Now: the cause is chained and readable, each analyst has its status and cause, an analyst whose data or run fails is absent and the cycle
goes on while one report is there, and the message says what to set up and where."""
from dataclasses import replace
from datetime import datetime, timezone

import json

import pandas as pd
import pytest
from b12_support import AS_OF, Engines
from exec_support import db, desks_db, env, ready  # noqa: F401  (fixtures)
from b12_support import runtime
from trader_support import C200, C210, sheet

from hokma.backoffice import runs
from hokma.broker import pnl, service
from hokma.contracts import sheet as sheet_store
from hokma.desks.core.graph import ModelReply  # noqa: F401
from hokma.desks.plantel import SEATS
from hokma.desks.swo.create import create_swo_desk, desk_agent_slug
from hokma.desks.swo.graph import run_cycle
from hokma.exec import killswitch

from hokma.data import sentinels
from hokma.desks.swo import app_runtime
from hokma.desks.swo.graph import CycleError, explain
from hokma.kit import desk_tools
from hokma.i18n import t as msg
from account_links_support import link_principal

@pytest.fixture
def swo(ready, monkeypatch):
    """A real SWO desk on its own account of the simulated broker, with the two option sheets and the quotes of the chain (as in test_b12_cycle)."""
    desk = create_swo_desk("Options swing", 1_000_000, db=ready.db)
    ready.swo_id = desk["id"]
    ready.agent = desk_agent_slug(SEATS["A11"], ready.swo_id)
    with ready.db.write() as u:
        account = u.broker.create_account(ready.broker_id, "Sim B", 1_000_000)
        link_principal(u, ready.swo_id, account.id)
    service.set_limits(ready.swo_id, {"max_premium_usd": 5000, "max_drawdown_pct": 10}, db=ready.db)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")                           # the broker answers: the drawdown rule of the desk can be checked
    monkeypatch.setattr(pnl, "latest_pnl", lambda account_id: pnl.BrokerPnl(account_id, 1_000_000.0, 1_000_000.0, 0.0, (), stamp, "sim"))
    killswitch.beat(ready.swo_id, db=ready.db)
    for cid in (C200, C210):
        sheet_store.save(sheet(cid), db=ready.db)
    ready.board.feed(C200, bid="5.10", ask="5.20", last="5.15")
    ready.board.feed(C210, bid="1.80", ask="1.90", last="1.85")
    ready.board.feed("EQ:AAPL", bid="199.90", ask="200.00", last="199.95")
    return ready


def rows(world, sql, **p):
    with world.db.read() as u:
        return u.session.query(sql, p)


def cycle(r, engines=None, **kw):
    engines = engines or Engines()
    return run_cycle(r.swo_id, symbol="AAPL", as_of=AS_OF, runtime=runtime(r, engines, agent=r.agent), db=r.db, **kw), engines


def record(r, result):
    return runs.get(result.run_id, db=r.db)["record"]["outputs"]


NOW = datetime(2026, 11, 23, 14, 0, tzinfo=timezone.utc)


def _bars():
    index = pd.date_range("2025-10-01", periods=130, freq="B", tz="UTC")
    return pd.DataFrame({"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "volume": 1000}, index=index)


def _ok(value):
    return sentinels.ok(value, source="hokma_market", as_of=NOW, received_at=NOW)


def _down(source, reason):
    return sentinels.unavailable(source, reason, as_of=NOW, received_at=NOW)


# ---- the exception chain ----------------------------------------------------------------------------------------------------------

def test_explain_keeps_the_class_the_message_and_every_cause():
    try:
        try:
            raise ValueError("no key")
        except ValueError as inner:
            raise CycleError("no_data", "no market data") from inner
    except CycleError as outer:
        text = explain(outer)
    assert text == "CycleError: no market data <- ValueError: no key"
    assert explain(RuntimeError()) == "RuntimeError"


# ---- the app loader: what the owner reads ----------------------------------------------------------------------------------------------

def test_load_without_a_market_source_says_what_to_connect_and_where(monkeypatch):
    monkeypatch.setattr("hokma.data.bars", lambda *a, **k: _down("hokma_market", "provider_key_missing"))
    with pytest.raises(CycleError) as exc:
        app_runtime.load(4, "NVDA", NOW)
    text = str(exc.value)
    assert exc.value.code == "no_data" and exc.value.source == "market"
    assert msg("desks.cycle.what.market", symbol="NVDA") in text and msg("desks.cycle.reason.provider_key_missing") in text and msg("desks.cycle.fix_market") in text


def test_load_turns_a_source_that_raises_into_a_cycle_error_with_its_cause_chained(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("socket closed")

    monkeypatch.setattr("hokma.data.bars", boom)
    with pytest.raises(CycleError) as exc:
        app_runtime.load(4, "NVDA", NOW)
    assert "ConnectionError: socket closed" in str(exc.value) and isinstance(exc.value.__cause__, ConnectionError)


def test_load_without_fundamentals_keeps_the_market_data_and_marks_that_analyst_absent(monkeypatch):
    import hokma.data.fundamentals_src as fsrc
    import hokma.data.snapshot as snapshots

    class Snap:
        def to_dict(self):
            return {"sections": {"market": {"status": "ok", "value": {"close": 10.5}}, "fundamentals": {"status": "DATA_UNAVAILABLE"}}}

    monkeypatch.setattr("hokma.data.bars", lambda *a, **k: _ok(_bars()))
    monkeypatch.setattr(fsrc, "fundamentals", lambda *a, **k: _down("financial_datasets", "provider_key_missing"))
    monkeypatch.setattr(snapshots, "snapshot", lambda *a, **k: Snap())
    data = app_runtime.load(4, "NVDA", NOW)
    assert set(data.analysts) == {"market"} and set(data.problems) == {"fundamentals"}
    assert msg("desks.cycle.fix_fundamentals") in data.problems["fundamentals"]
    assert data.company.statements.empty and len(data.company.candles) == 130


# ---- the cycle: causes by analyst, partial and total failures ---------------------------------------------------------------------------------

def test_a_loader_that_cannot_give_data_fails_the_cycle_with_the_cause_of_every_analyst(swo):
    from b12_support import runtime

    engines = Engines()
    rt = runtime(swo, engines, agent=swo.agent)
    message = f"{msg('desks.cycle.what.market', symbol='AAPL')}: {msg('desks.cycle.reason.provider_key_missing')}. {msg('desks.cycle.fix_market')}"

    def load(desk_id, symbol, as_of):
        raise CycleError("no_data", message, source="market")

    rt.load = load
    from hokma.desks.swo.graph import run_cycle

    result = run_cycle(swo.swo_id, symbol="AAPL", as_of=AS_OF, runtime=rt, db=swo.db)
    assert result.proposal_id is None and result.stopped_at == "analysts" and result.reason == "error:analysts:CycleError"
    assert result.detail == message
    assert set(result.analyst_status) == {"market", "sentiment", "news", "fundamentals", "options"} and set(result.analyst_status.values()) == {"error:CycleError"}
    assert all(cause == message for cause in result.analyst_causes.values())
    assert set(result.absent) == {"market", "sentiment", "news", "fundamentals", "options"}
    out = record(swo, result)
    assert out["detail"] == message and out["error"].startswith("CycleError: " + msg("desks.cycle.what.market", symbol="AAPL")) and out["analyst_status"] == result.analyst_status
    assert rows(swo, "SELECT status FROM run_records WHERE id = :i", i=result.run_id)[0]["status"] == "failed"
    shown = desk_tools._cycle_row(runs.get(result.run_id, db=swo.db), full=False)
    assert shown["detail"] == message and shown["analyst_status"]["market"] == "error:CycleError" and shown["analyst_causes"]["market"] == message


def test_an_analyst_without_data_is_absent_with_its_cause_and_the_cycle_goes_on(swo):
    from b12_support import runtime

    engines = Engines()
    rt = runtime(swo, engines, agent=swo.agent)
    original = rt.load
    cause = f"{msg('desks.cycle.what.fundamentals', symbol='AAPL')}: {msg('desks.cycle.reason.provider_key_missing')}. {msg('desks.cycle.fix_fundamentals')}"
    rt.load = lambda desk_id, symbol, as_of: replace(original(desk_id, symbol, as_of), problems={"fundamentals": cause})
    from hokma.desks.swo.graph import run_cycle

    result = run_cycle(swo.swo_id, symbol="AAPL", as_of=AS_OF, runtime=rt, db=swo.db)
    assert result.proposal_id and result.stopped_at is None and result.detail is None      # the quorum held: Sam reported
    assert result.analyst_status["fundamentals"] == "no_data" and result.analyst_causes["fundamentals"] == cause
    assert result.analyst_status["sentiment"] == "ok" and "sentiment" not in result.analyst_causes
    assert "fundamentals" in result.absent
    assert all(result.analyst_causes[k] for k, v in result.analyst_status.items() if v != "ok")          # no absent analyst is left without a cause
    assert record(swo, result)["analyst_causes"]["fundamentals"] == cause


def test_an_analyst_that_raises_does_not_tumble_the_cycle_and_its_cause_is_readable(swo):
    engines = Engines()

    class Raises:
        def chat(self, *a, **k):
            raise RuntimeError("model endpoint refused")

    engines.models["A01"] = Raises()
    result, _ = cycle(swo, engines)
    assert result.proposal_id and result.stopped_at is None
    assert result.analyst_status["market"] == "error:RuntimeError" and "RuntimeError: model endpoint refused" in result.analyst_causes["market"]


def test_without_quorum_the_cycle_stops_and_says_why_analyst_by_analyst(swo):
    from b12_support import runtime
    from hokma.desks.swo.graph import run_cycle

    engines = Engines(sam=False)
    rt = runtime(swo, engines, agent=swo.agent)
    original = rt.load
    rt.load = lambda desk_id, symbol, as_of: replace(original(desk_id, symbol, as_of), problems={"market": "no market snapshot. Connect Schwab in Connections."})
    result = run_cycle(swo.swo_id, symbol="AAPL", as_of=AS_OF, runtime=rt, db=swo.db)
    assert result.reason == "no_analyst_report" and result.proposal_id is None
    assert msg("desks.cycle.no_report", why="").strip() in result.detail and "market: no market snapshot. Connect Schwab in Connections." in result.detail
    assert result.analyst_status["market"] == "no_data"


def test_the_company_filter_with_no_statements_abstains_and_ends_the_cycle_with_its_own_reason(swo):
    from b12_support import runtime
    from hokma.desks.swo.graph import run_cycle

    engines = Engines()
    rt = runtime(swo, engines, agent=swo.agent)
    original = rt.load

    def load(desk_id, symbol, as_of):
        data = original(desk_id, symbol, as_of)
        empty = replace(data.company, statements=pd.DataFrame({"filed": pd.Series(dtype=str)}))
        return replace(data, company=empty, problems={"fundamentals": "no fundamentals data. Connect Financial Datasets in Connections."})

    rt.load = load
    result = run_cycle(swo.swo_id, symbol="AAPL", as_of=AS_OF, runtime=rt, db=swo.db)
    assert result.proposal_id is None and result.stopped_at == "company_filter" and not result.reason.startswith("error:"), result.reason
    assert result.analyst_status["fundamentals"] == "no_data"


# ---- secrets never reach the owner, the record or the log ----------------------------------------------------------------------------------------

APIKEY = "ABCD1234SECRETKEY99"
BEARER = "sk-ant-api03-ZZZZSECRETTOKEN1234567890"


def _leaky():
    return ConnectionError(f"GET https://api.example.com/v1/bars?symbol=AAPL&apikey={APIKEY} failed; headers: Authorization: Bearer {BEARER}")


def test_explain_scrubs_the_secrets_of_every_message_in_the_chain():
    try:
        try:
            raise _leaky()
        except ConnectionError as inner:
            raise RuntimeError("outer") from inner
    except RuntimeError as outer:
        text = explain(outer)
    assert APIKEY not in text and BEARER not in text and "ConnectionError" in text and "outer" in text


def test_a_source_that_raises_with_a_key_in_its_url_leaves_no_secret_in_the_cycle_error(monkeypatch):
    def boom(*a, **k):
        raise _leaky()

    monkeypatch.setattr("hokma.data.bars", boom)
    with pytest.raises(CycleError) as exc:
        app_runtime.load(4, "NVDA", NOW)
    assert APIKEY not in str(exc.value) and BEARER not in str(exc.value) and APIKEY not in explain(exc.value) and BEARER not in explain(exc.value)


def test_the_secret_does_not_reach_detail_causes_the_record_the_tool_answer_or_the_log(swo, caplog):
    import logging

    engines = Engines()

    class Leaks:
        def chat(self, *a, **k):
            raise _leaky()

    engines.models["A01"] = Leaks()
    with caplog.at_level(logging.DEBUG):
        result, _ = cycle(swo, engines)
        rt = runtime(swo, Engines(), agent=swo.agent)
        rt.load = lambda *a: (_ for _ in ()).throw(_leaky())
        failed = run_cycle(swo.swo_id, symbol="AAPL", as_of=AS_OF, runtime=rt, db=swo.db)
    assert "ConnectionError" in result.analyst_causes["market"]
    shown = desk_tools._cycle_row(runs.get(failed.run_id, db=swo.db), full=True)
    everything = " ".join([str(result.analyst_causes), str(failed.detail), str(failed.analyst_causes), str(record(swo, failed)), str(shown), caplog.text,
                           json.dumps({"detail": failed.detail, "analyst_causes": dict(failed.analyst_causes)})])
    assert APIKEY not in everything and BEARER not in everything and "ConnectionError" in failed.detail


def test_the_setup_step_is_only_offered_when_the_cause_is_a_missing_key_or_connection(monkeypatch):
    monkeypatch.setattr("hokma.data.bars", lambda *a, **k: _down("hokma_market", "provider_unreachable"))
    with pytest.raises(CycleError) as exc:
        app_runtime.load(4, "NVDA", NOW)
    assert msg("desks.cycle.reason.provider_unreachable") in str(exc.value) and msg("desks.cycle.fix_market") not in str(exc.value)


def test_a_cycle_that_ends_at_the_company_filter_for_lack_of_statements_says_so(swo):
    engines = Engines()
    rt = runtime(swo, engines, agent=swo.agent)
    original = rt.load

    def load(desk_id, symbol, as_of):
        data = original(desk_id, symbol, as_of)
        return replace(data, company=replace(data.company, statements=pd.DataFrame({"filed": pd.Series(dtype=str)})), problems={"fundamentals": "no fundamentals data for AAPL."})

    rt.load = load
    result = run_cycle(swo.swo_id, symbol="AAPL", as_of=AS_OF, runtime=rt, db=swo.db)
    assert result.stopped_at == "company_filter" and msg("desks.cycle.company_filter_no_statements", symbol="AAPL", why="").strip() in result.detail and "no fundamentals data for AAPL." in result.detail
