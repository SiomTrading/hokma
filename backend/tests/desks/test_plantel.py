"""The plantel registry (SPEC-019 4.2): codes, names, lines, tools, roles, models and the SWO variants."""
import dataclasses

import pytest

from hokma.db.repos.desks import MODELS, ROLES
from hokma.desks.core.signal import LINES, Signal
from hokma.desks.plantel import KIT_AGENTS_DIR, SEATS, Seat, seats_for

EXPECTED = {
    "A01": ("Adam", "market-analyst"), "A02": ("Sam", "sentiment-analyst"), "A04": ("Tim", "news-macro-analyst"),
    "A06": ("Ezra", "fundamentals-analyst"), "A07": ("Caleb", "options-analyst"), "A08": ("Jonah", "bull-advocate"),
    "A09": ("Simon", "bear-advocate"), "A10": ("Amos", "desk-judge"), "A11": ("Isaac", "trader"),
    "A12": ("Jake", "risk-bold"), "A13": ("Glen", "risk-careful"), "A14": ("Reed", "risk-arbiter"),
    "A15": ("Grant", "desk-manager"), "A16": ("Owen", "quality-owner"), "A17": ("Nate", "inversion-veto"),
    "A18": ("Gabe", "safety-margin"), "A19": ("Paul", "fair-growth"), "A20": ("Ray", "inflection-hunter"),
    "A21": ("Noah", "contrarian-catalyst"), "A22": ("Ruth", "disruption-builder"), "A23": ("Drew", "earnings-drift"),
}


def test_the_plantel_has_the_21_seats_of_the_spec_without_levi_and_seth():
    assert list(SEATS) == list(EXPECTED)
    assert "A03" not in SEATS and "A05" not in SEATS
    for code, (name, slug) in EXPECTED.items():
        seat = SEATS[code]
        assert (seat.code, seat.name, seat.slug) == (code, name, slug)
        assert seat.kit_file == f"{KIT_AGENTS_DIR}/{slug}.md" and seat.kit_file.endswith(".md")
        assert seat.code_module.startswith("backend/hokma/desks/agents/") and seat.code_module.endswith(".py")


def test_every_seat_uses_a_valid_role_model_and_lines():
    for seat in SEATS.values():
        assert seat.desk_role in ROLES and seat.default_model in MODELS
        assert seat.lines and set(seat.lines) <= set(LINES)
        assert isinstance(seat.tools, tuple) and len(set(seat.tools)) == len(seat.tools)
        assert isinstance(seat.output, type)


def test_roles_follow_the_nearest_role_of_the_check():
    roles = {code: seat.desk_role for code, seat in SEATS.items()}
    assert roles["A15"] == "chief" and sum(1 for r in roles.values() if r == "chief") == 1
    assert (roles["A08"], roles["A09"], roles["A10"], roles["A11"]) == ("bull", "bear", "judge", "execution")
    assert {roles[c] for c in ("A12", "A13", "A14")} == {"risk"}
    assert {roles[c] for c in ("A16", "A17", "A18", "A19", "A20", "A21", "A22", "A23")} == {"signals"}
    assert {roles[c] for c in ("A01", "A02", "A04", "A06", "A07")} == {"research"}


def test_tools_per_seat_match_the_table():
    assert SEATS["A08"].tools == () and SEATS["A09"].tools == ()
    assert SEATS["A11"].tools == ("exec_propose", "exec_status", "contract_get")
    assert "order_place" not in {tool for seat in SEATS.values() for tool in seat.tools}
    assert SEATS["A01"].tools == ("data_bars", "data_snapshot", "quant_call") and SEATS["A01"].quant_modules == ("indicators", "stats")
    assert SEATS["A15"].tools == ("book_pnl", "lessons_propose", "schedule_run")
    assert "desk_configure" in SEATS["A14"].tools and SEATS["A14"].quant_modules == ("risk", "options")
    assert SEATS["A10"].quant_modules == ("validation",)
    for code in ("A16", "A17", "A18", "A19", "A21", "A22"):
        assert SEATS[code].tools == ("quant_call",) and SEATS[code].quant_modules == ("fundamentals",)
    assert SEATS["A20"].quant_modules == ("fundamentals", "stats")
    assert SEATS["A23"].tools == ("quant_call", "data_filings") and SEATS["A23"].quant_modules == ("indicators", "stats")


def test_investors_answer_with_signals_and_nobody_else_does():
    for code, seat in SEATS.items():
        assert (seat.output is Signal) == (code in {f"A{n}" for n in range(16, 24)})


def test_swo_traditional_has_20_seats_without_drew_and_pead_has_all_21():
    traditional = seats_for("SWO", "traditional")
    assert len(traditional) == 20 and "A23" not in {s.code for s in traditional}
    assert [s.code for s in seats_for("SWO", None)] == [s.code for s in traditional]
    pead = seats_for("SWO", "pead")
    assert len(pead) == 21 and [s.code for s in pead] == list(EXPECTED)
    assert [s.code for s in traditional] == sorted(s.code for s in traditional)


def test_other_lines_take_their_own_seats_and_no_variant():
    dt = {s.code for s in seats_for("DT", None)}
    assert "A06" not in dt and "A07" not in dt and "A23" not in dt and "A15" in dt
    assert {s.code for s in seats_for("RF", None)} == {"A04", "A10", "A11", "A14", "A15"}
    assert "A23" not in {s.code for s in seats_for("LP", None)}
    with pytest.raises(ValueError):
        seats_for("DT", "pead")


@pytest.mark.parametrize("line,variant", [("XX", None), ("SWO", "nope"), ("SWO", ""), (None, None)])
def test_out_of_domain_arguments_raise_a_value_error(line, variant):
    with pytest.raises(ValueError):
        seats_for(line, variant)


def test_a_seat_with_a_bad_role_model_lines_or_quant_scope_is_refused():
    base = SEATS["A01"]
    for change in ({"desk_role": "boss"}, {"default_model": "gpt"}, {"lines": frozenset()}, {"lines": frozenset({"XX"})},
                   {"tools": ("data_bars",)}):
        with pytest.raises(ValueError):
            dataclasses.replace(base, **change)
    with pytest.raises(dataclasses.FrozenInstanceError):
        base.name = "Other"       # type: ignore[misc]
    assert isinstance(base, Seat)


def test_B13_the_two_advocate_seats_declare_the_real_tribunal_outputs_from_a_neutral_module():
    from hokma.desks.agents import debate, debate_output

    assert SEATS["A08"].output is debate_output.BullOutput and SEATS["A09"].output is debate_output.BearOutput
    assert debate.BullOutput is debate_output.BullOutput and debate.BearOutput is debate_output.BearOutput    # the debate module re-exports them
    assert debate.OutputError is debate_output.OutputError and debate.MISSING is debate_output.MISSING
    source = open(debate_output.__file__, encoding="utf-8").read()
    assert "hokma.desks.plantel" not in source                                                              # the neutral module never imports the plantel
