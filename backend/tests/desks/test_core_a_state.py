"""Typed state (TA-23, TA-24, TA-29)."""
import json
from datetime import datetime, timezone

import pytest

from hokma.desks.core import state as st
from hokma.desks.core.graph import AnalystSpec, FlowSpec, ModelReply, build_desk_graph

AS_OF = datetime(2026, 1, 2, 15, 0, tzinfo=timezone.utc)


def test_initial_state_has_desk_symbol_line_and_as_of_TA_23():
    s = st.initial_state(desk_id=4, symbol=" msft ", line="LP", as_of=AS_OF, horizon="years", analysts=("market", "news"))
    assert (s["desk_id"], s["symbol"], s["line"], s["horizon"]) == (4, "MSFT", "LP", "years")
    assert s["as_of"] == "2026-01-02T15:00:00+00:00"
    assert s["debate"]["count"] == 0 and s["risk_debate"]["count"] == 0 and s["debate"]["summary"] == ""


def test_absent_report_is_marked_and_does_not_break_TA_23_TA_29():
    s = st.initial_state(desk_id=1, symbol="X", line="DT", as_of=AS_OF, horizon="intraday", analysts=("market",))
    report = s["reports"]["market"]
    assert report["status"] == "absent" and st.is_absent(report)
    assert "(No market report in this run" in report["text"]
    assert st.is_absent(None) and not st.is_absent(st.make_report("m", "text"))


@pytest.mark.parametrize("value", [None, "", "   \n"])
def test_report_or_absent_declares_missing_text_TA_29(value):
    assert st.report_or_absent(value, "news") == "(No news report in this run: it is not available, not an empty finding.)"
    assert st.report_or_absent("  Real report ", "news") == "Real report"


def test_opponent_or_opening_never_leaves_a_blank_TA_29():
    assert "has not spoken yet" in st.opponent_or_opening("", "bear")
    assert st.opponent_or_opening("their point", "bear") == "their point"


def test_initial_state_validates_inputs_TA_23():
    ok = dict(desk_id=1, symbol="X", line="DT", as_of=AS_OF, horizon="intraday")
    for bad in ({"line": "XX"}, {"horizon": "decades"}, {"as_of": datetime(2026, 1, 1)}, {"symbol": " "}):
        with pytest.raises(ValueError):
            st.initial_state(**{**ok, **bad})


def test_round_stores_a_bounded_summary_not_the_history_TA_24():
    debate = st.new_debate()
    sizes = []
    for i in range(40):
        debate = st.record_round(debate, "bull" if i % 2 == 0 else "bear", f"round {i} " + "argument " * 400)
        sizes.append(len(json.dumps(debate)))
    assert debate["count"] == 40
    assert len(debate["summary"]) <= st.MAX_SUMMARY_CHARS and len(debate["last_response"]) <= st.MAX_RESPONSE_CHARS
    assert max(sizes[5:]) <= max(sizes[:5]) + st.MAX_SUMMARY_CHARS     # stops growing after the cap
    assert sizes[-1] == sizes[-2] or abs(sizes[-1] - sizes[-2]) < 20


def test_record_round_uses_the_summary_the_node_wrote_TA_24():
    debate = st.record_round(st.new_debate(), "bull", "long answer " * 100, summary="Short rolling summary.")
    assert debate["summary"] == "Short rolling summary."


class Echo:
    def chat(self, system, messages, tools=()):
        return ModelReply("long answer " * 200)


def test_graph_state_size_does_not_grow_with_rounds_TA_24():
    def run(rounds):
        s = st.initial_state(desk_id=1, symbol="X", line="LP", as_of=AS_OF, horizon="years", analysts=("m",))
        graph = build_desk_graph(FlowSpec((AnalystSpec("m"),), debate_rounds=rounds), Echo(), lambda r, sp, s_: f"{r}/{sp}")
        out = graph.invoke(s).state
        return len(json.dumps({"debate": out["debate"], "reports": out["reports"]}))

    assert run(10) <= run(3) + st.MAX_SUMMARY_CHARS


def test_clip_cuts_on_a_word_boundary():
    out = st.clip("alpha beta gamma delta epsilon", 18)
    assert len(out) <= 18 and out.endswith("\u2026") and " " not in out[-3:]
    assert st.clip("short", 18) == "short"
