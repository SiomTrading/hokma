"""Records of cycles, receipts, the state log and the timing of the analysts (SPEC-018: AH-24, AH-64, TA-84, TA-22)."""
import json
import pathlib
from decimal import Decimal as D

import pytest
from backoffice_support import db, desks_db, env, ready  # noqa: F401

from hokma.backoffice import runs, timing
from hokma.desks.core import paths
from hokma.desks.core.state import Message, ToolCall

KEY, ACCOUNT, TOKEN = "SENTINEL-KEY-4455667788", "99887766554", "SENTINEL-TOKEN-ABCDEF123456"


def test_a_cycle_leaves_a_record_with_inputs_outputs_decisions_and_order_ids_AH_24(ready):
    rid = runs.record(ready.desk_id, "cycle", signature="sig-1", entries={"symbol": "NQ", "as_of": "2026-03-12"},
                      outputs={"action": "buy", "size": D("1")}, decisions=[7, {"id": 8, "verdict": "approved"}], order_ids=["S123", 456], db=ready.db)
    row = runs.get(rid, db=ready.db)
    assert row["status"] == "done" and row["desk_id"] == ready.desk_id and row["signature"] == "sig-1" and row["ended_at"]
    body = row["record"]
    assert body["entries"] == {"symbol": "NQ", "as_of": "2026-03-12"} and body["outputs"]["action"] == "buy"
    assert runs.decode(body["outputs"])["size"] == D("1")                       # a Decimal comes back as a Decimal
    assert body["decisions"] == [7, {"id": 8, "verdict": "approved"}] and body["order_ids"] == ["S123", 456] and body["timings"] is None
    assert [r["id"] for r in runs.list_runs(ready.desk_id, kind="cycle", db=ready.db)] == [rid]


def test_a_record_is_never_edited_or_deleted_AH_24(ready):
    rid = runs.record(ready.desk_id, "cycle", signature="s", outputs={"x": 1}, db=ready.db)
    before = runs.get(rid, db=ready.db)
    with pytest.raises(runs.RunClosed):
        runs.finish(rid, "failed", outputs={"x": 2}, db=ready.db)
    with pytest.raises(runs.RunClosed):
        runs.interrupt(rid, db=ready.db)
    with pytest.raises(runs.RunClosed):
        runs.resume(rid, db=ready.db)
    assert runs.get(rid, db=ready.db) == before                                  # the row is byte for byte what it was
    assert not [n for n in dir(runs) if n.startswith(("delete", "remove", "update", "edit", "set_", "overwrite"))]   # there is no route
    source = pathlib.Path(runs.__file__).read_text(encoding="utf-8").upper() + pathlib.Path(runs.__file__).with_name("checkpoint.py").read_text(encoding="utf-8").upper()
    assert "DELETE FROM RUN_" not in source and "DROP TABLE" not in source
    assert source.count("UPDATE RUN_RECORDS") == 1 and "STATUS IN (" in source        # the one guarded step of the life of a run


def test_a_long_run_goes_running_interrupted_running_and_finishes_once_AH_24(ready):
    rid = runs.begin(ready.desk_id, "cycle", signature="s", db=ready.db)
    assert runs.get(rid, db=ready.db)["status"] == "running"
    runs.interrupt(rid, db=ready.db)
    assert runs.get(rid, db=ready.db)["status"] == "interrupted"
    runs.resume(rid, db=ready.db)
    runs.finish(rid, "done", outputs={"ok": True}, order_ids=["S1"], db=ready.db)
    done = runs.get(rid, db=ready.db)
    assert done["status"] == "done" and done["record"]["order_ids"] == ["S1"]
    with pytest.raises(runs.RunClosed):
        runs.finish(rid, "done", db=ready.db)


def test_a_record_is_checked_for_shape_and_size_AH_24(ready):
    for kwargs in ({"kind": "Bad Kind"}, {"kind": "cycle", "signature": ""}, {"kind": "cycle", "status": "running"},
                   {"kind": "cycle", "order_ids": [{"a": 1}]}, {"kind": "cycle", "order_ids": ["x" * 65]}, {"kind": "cycle", "order_ids": ["a"] * 1001},
                   {"kind": "cycle", "outputs": {"big": "word " * (runs.MAX_RECORD_CHARS // 5 + 1)}}, {"kind": "cycle", "outputs": {"f": float("nan")}},
                   {"kind": "cycle", "outputs": {"o": object()}}):
        with pytest.raises(runs.RunError):
            runs.record(ready.desk_id, **{"signature": "s", "db": ready.db, **kwargs})
    assert runs.list_runs(ready.desk_id, db=ready.db) == []


def test_a_string_with_no_spaces_cannot_make_a_record_slow_AH_24(ready):
    import time

    started = time.monotonic()
    rid = runs.record(ready.desk_id, "cycle", signature="s", outputs={"blob": "y" * 150_000, "after": "fine"}, db=ready.db)
    assert time.monotonic() - started < 10
    out = runs.get(rid, db=ready.db)["record"]["outputs"]
    assert out["blob"] == "[150000 unbroken characters omitted]" and out["after"] == "fine"


def test_a_record_holds_no_key_token_or_account_id_AH_24(ready):
    rid = runs.record(ready.desk_id, "cycle", signature="s", entries={"api_key": KEY, "note": f"call with token={TOKEN} please", "account_number": ACCOUNT},
                      outputs={"hashValue": "SENTINEL-HASH-1", "ok": "fine"}, db=ready.db)
    with ready.db.read() as u:
        raw = u.session.one("SELECT record_json FROM run_records WHERE id = :i", {"i": rid})["record_json"]
    assert KEY not in raw and TOKEN not in raw and ACCOUNT not in raw and "SENTINEL-HASH-1" not in raw and "fine" in raw


SECRETS = {"api_key": KEY, "nested": {"token": TOKEN, "account_number": ACCOUNT, "ok": "visible"}, "list": [{"Authorization": "Bearer " + TOKEN}],
           "free_text": f"the account was {ACCOUNT} and the key {KEY}", "account_hash": "HASH-SENTINEL-99", "password": "SENTINEL-PASSWORD"}


def test_a_receipt_is_a_json_file_in_the_desk_folder_without_secrets_AH_64(ready):
    path = runs.write_receipt(ready.desk_id, SECRETS, run_id=12, secrets=[KEY, ACCOUNT, TOKEN])
    assert path.parent == paths.desk_dir(ready.desk_id) / "receipts" and path.suffix == ".json"
    raw = path.read_text(encoding="utf-8")
    for sentinel in (KEY, TOKEN, ACCOUNT, "HASH-SENTINEL-99", "SENTINEL-PASSWORD"):
        assert sentinel not in raw
    body = json.loads(raw)
    assert body["desk_id"] == ready.desk_id and body["run_id"] == 12 and body["receipt"]["nested"]["ok"] == "visible"
    assert body["receipt"]["api_key"] == "***" and body["receipt"]["nested"]["token"] == "***"


def test_two_runs_never_write_the_same_receipt_file_AH_64(ready):
    from datetime import datetime, timezone

    same_second = datetime(2026, 3, 12, 15, 0, 0, tzinfo=timezone.utc)
    names = {runs.write_receipt(ready.desk_id, {"n": n}, now=same_second) for n in range(20)}
    assert len(names) == 20 and all(p.is_file() for p in names)
    assert sorted(json.loads(p.read_text(encoding="utf-8"))["receipt"]["n"] for p in names) == list(range(20))


def test_a_receipt_is_checked_and_stays_inside_the_desk_folder_AH_64(ready):
    with pytest.raises(paths.PathEscape):
        runs.write_receipt(0, {"a": 1})
    with pytest.raises(runs.RunError):
        runs.write_receipt(ready.desk_id, {"big": "word " * (runs.MAX_RECORD_CHARS // 5 + 1)})


def _state():
    return {"desk_id": 1, "symbol": "NQ", "as_of": "2026-03-12T14:00:00+00:00", "horizon": "intraday",
            "messages": [Message("user", "NQ", "m0"), Message("assistant", "calling", "m1", (ToolCall("c1", "quote", {"symbol": "NQ"}),))],
            "reports": {"market": {"source": "market", "status": "ok", "text": "up"}, "news": {"source": "news", "status": "absent", "text": "(No news)"}},
            "debate": {"count": 1, "summary": "bull wins", "last_speaker": "bull", "last_response": "x", "verdict": "buy"},
            "tool_rounds": {"market": 2}, "trader_plan": "buy 1 at 21000.25", "final_decision": "approved", "price": D("21000.25"), "legs": ("a", "b")}


def test_the_state_log_is_one_json_per_date_and_reads_back_to_the_same_state_TA_84(ready):
    state = _state()
    path = runs.state_log(ready.desk_id, "NQ", "2026-03-12", state)
    assert path == paths.desk_dir(ready.desk_id) / "state" / "2026-03-12.json"
    assert runs.read_state_log(ready.desk_id, "NQ", "2026-03-12") == state          # the same state, tuples, decimals and messages included
    other = runs.state_log(ready.desk_id, "ES", "2026-03-12", {**state, "symbol": "ES"})
    assert other == path and json.loads(path.read_text(encoding="utf-8"))["states"].keys() == {"NQ", "ES"}      # one file per date
    assert runs.state_log(ready.desk_id, "NQ", "2026-03-13", state) != path
    assert sorted(p.name for p in path.parent.glob("*.json")) == ["2026-03-12.json", "2026-03-13.json"]
    assert runs.read_state_log(ready.desk_id, "NQ", "2026-03-14") is None and runs.read_state_log(ready.desk_id, "TSLA", "2026-03-12") is None


def test_the_state_log_has_no_keys_or_account_ids_TA_84(ready):
    state = {**_state(), **SECRETS}
    path = runs.state_log(ready.desk_id, "NQ", "2026-03-12", state, secrets=[KEY, ACCOUNT, TOKEN])
    raw = path.read_text(encoding="utf-8")
    for sentinel in (KEY, TOKEN, ACCOUNT, "HASH-SENTINEL-99", "SENTINEL-PASSWORD"):
        assert sentinel not in raw
    back = runs.read_state_log(ready.desk_id, "NQ", "2026-03-12")
    assert back["messages"] == state["messages"] and back["nested"]["ok"] == "visible" and back["api_key"] == "***"


def test_the_state_log_refuses_unsafe_names_and_unknown_types_TA_84(ready):
    for symbol in ("../x", "a/b", ".."):
        with pytest.raises(ValueError):
            runs.state_log(ready.desk_id, symbol, "2026-03-12", {})
    for day in ("2026-3-12", "yesterday", "2026-13-01"):
        with pytest.raises(ValueError):
            runs.state_log(ready.desk_id, "NQ", day, {})
    with pytest.raises(runs.RunError):
        runs.state_log(ready.desk_id, "NQ", "2026-03-12", {"o": object()})


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_the_seconds_of_each_analyst_are_in_the_record_and_the_one_over_budget_is_flagged_TA_22(ready):
    clock, timer = Clock(), None
    timer = timing.AnalystTimer(clock)
    with timer.measure("market"):
        clock.now += 12.5
    timer.start("news")
    clock.now += 75.25                                   # the intraday line has 60 seconds per analyst
    timer.stop("news")
    seconds = timer.seconds()
    assert seconds == {"market": D("12.5"), "news": D("75.25")}
    rid = runs.record(ready.desk_id, "cycle", signature="s", analyst_seconds=seconds, line="DT", db=ready.db)
    block = runs.get(rid, db=ready.db)["record"]["timings"]
    assert block == {"line": "DT", "seconds": {"market": "12.5", "news": "75.25"}, "total_seconds": "87.75", "budget_seconds": "60", "over_budget": ["news"]}
    longer = runs.record(ready.desk_id, "cycle", signature="s", analyst_seconds=seconds, line="SWC", db=ready.db)
    assert runs.get(longer, db=ready.db)["record"]["timings"]["over_budget"] == []          # ten minutes for the swing line


def test_a_line_without_a_budget_flags_nothing_and_says_so_TA_22(ready):
    block = timing.flag({"market": 9999}, line="XX")
    assert block["budget_seconds"] is None and block["over_budget"] == []
    assert timing.flag({"market": 9999}, line="XX", budget="30")["over_budget"] == ["market"]
    with ready.db.write() as u:
        pass
    assert timing.line_of_desk(ready.desk_id, db=ready.db) == "DT" and timing.budget_of("DT") == D(60)


def test_the_timer_refuses_misuse_TA_22():
    timer = timing.AnalystTimer(Clock())
    with pytest.raises(timing.TimingError):
        timer.stop("never-started")
    for name in ("", "a b", "x" * 61, "../x"):
        with pytest.raises(timing.TimingError):
            timer.start(name)
    with pytest.raises(timing.TimingError):
        timing.flag({"a": -1})
