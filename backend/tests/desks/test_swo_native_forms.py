"""The native forms against every SWO structure (SPEC-019, B13): `hokma.exec.native` reads only a SINGLE, an exit OCO and an entry TRIGGER with exit
children. Each structure the desk can propose is turned into the Schwab order that `exec.submit.to_native` builds for it, with and without a stop and a
target, and the filter must read it (or the test names the structure that does not fit). The same proposals also travel as plain `OrderProposal`s through
`hokma.exec.submit`, which is the normal path of the desk, so a structure that does not fit the native forms still has its way and is never sent unchecked."""
from decimal import Decimal as D

import pytest
from trader_support import C200, C210, C220, P170, P180, P190, STOCK, sheets as trader_sheets, sheet

from hokma.desks.swo.premium_switch import ALL_STRUCTURES, PURCHASE_STRUCTURES, SELLING_STRUCTURES
from hokma.desks.swo.schema import STRUCTURES, SWOProposal, UNBOUNDED_LOSS
from hokma.exec import native
from hokma.exec.native import Unconvertible
from hokma.exec.proposal import Leg
from hokma.exec.submit import Refusal, exits_apart, to_native

P200 = "OPT:AAPL:2026-12-18:P:200"
LEGS = {
    "long_call": [(C200, "buy")], "long_put": [(P190, "buy")],
    "debit_call_spread": [(C200, "buy"), (C210, "sell")], "debit_put_spread": [(P190, "buy"), (P180, "sell")],
    "short_put": [(P190, "sell")], "cash_secured_put": [(P190, "sell")], "short_call": [(C200, "sell")],
    "credit_call_spread": [(C200, "sell"), (C210, "buy")], "credit_put_spread": [(P190, "sell"), (P180, "buy")],
    "iron_condor": [(P170, "buy"), (P180, "sell"), (C200, "sell"), (C210, "buy")],
    "short_straddle": [(C200, "sell"), (P200, "sell")], "short_strangle": [(P190, "sell"), (C200, "sell")],
    "covered_call": [(STOCK, "buy"), (C200, "sell")],
    "iron_butterfly": [(P190, "buy"), (P200, "sell"), (C200, "sell"), (C210, "buy")],
    "protective_put": [(STOCK, "buy"), (P190, "buy")],
}
SHARES_AND_OPTIONS = ("covered_call", "protective_put")        # shares plus an option: the door refuses a stop or a target on them, their exits are orders apart (decision 1264)
SHEETS = {**trader_sheets(), P200: sheet(P200), C220: sheet(C220)}


def proposal(structure: str, *, exits: bool) -> SWOProposal:
    legs = tuple(Leg(cid, side, "open", D(100) if cid == STOCK else D(1)) for cid, side in LEGS[structure])
    debit = structure in PURCHASE_STRUCTURES
    extra = {"stop_loss": D("1.00") if debit else D("6.00"), "take_profit": D("5.00") if debit else D("0.50")} if exits else {}
    return SWOProposal(7, "SWO", "desk", legs, "limit", limit_price=D("2.50"), thesis="A test structure.", created_by="isaac-7",
                       structure=structure, expiration=__import__("datetime").date(2026, 12, 18), net_premium=D("2.50") if debit else D("-2.50"),
                       max_loss_usd=None if structure in UNBOUNDED_LOSS else D("1000"), underlying="AAPL", **extra)


def test_every_structure_has_legs_in_this_test_and_the_premium_split_matches_the_switch():
    assert set(LEGS) == set(STRUCTURES) == set(ALL_STRUCTURES)
    assert set(PURCHASE_STRUCTURES) == {"long_call", "long_put", "debit_call_spread", "debit_put_spread", "protective_put"}
    assert "iron_butterfly" in SELLING_STRUCTURES                                    # it is a credit: premium selling, closed with the switch
    assert set(SELLING_STRUCTURES) == set(STRUCTURES) - set(PURCHASE_STRUCTURES)


@pytest.mark.parametrize("structure", sorted(STRUCTURES))
def test_the_entry_of_every_structure_fits_the_single_form_of_the_native_filter(structure):
    order = to_native(proposal(structure, exits=False).to_order(), SHEETS, "schwab")
    assert order["orderStrategyType"] == "SINGLE" and len(order["orderLegCollection"]) == len(LEGS[structure])
    walked = native.to_proposals(7, order, created_by="isaac-7", line="SWO")
    assert len(walked) == 1 and [(g.contract_id, g.side) for g in walked[0].legs] == LEGS[structure]


@pytest.mark.parametrize("structure", sorted(STRUCTURES))
def test_the_entry_with_a_stop_and_a_target_fits_the_trigger_form_with_an_exit_oco(structure):
    if structure in SHARES_AND_OPTIONS:                                              # decision 1264: the door refuses shares plus a call with exits; they are orders apart
        with pytest.raises(Refusal) as refused:
            exits_apart(proposal(structure, exits=True).to_order(), SHEETS)
        assert refused.value.code == "exits_apart_required"
        return
    order = to_native(proposal(structure, exits=True).to_order(), SHEETS, "schwab")
    assert order["orderStrategyType"] == "TRIGGER" and order["childOrderStrategies"][0]["orderStrategyType"] == "OCO"
    walked = native.to_proposals(7, order, created_by="isaac-7", line="SWO")
    assert len(walked) == 3                                                         # the entry and the two exits, nothing skipped
    entry, *exits = walked
    assert [(g.contract_id, g.side) for g in entry.legs] == LEGS[structure]
    for leg_exit in exits:
        assert {g.contract_id for g in leg_exit.legs} == {cid for cid, _ in LEGS[structure]}
        assert all(g.side != dict(LEGS[structure])[g.contract_id] for g in leg_exit.legs)
        for g in leg_exit.legs:
            # FINDING (B13): Schwab has no close instruction for a share, so the exit of the share leg of a covered call is a plain SELL that
            # the native filter reads as an opening sell of the share. Every option leg reads as a close.
            assert g.effect == ("open" if g.contract_id == STOCK else "close")


@pytest.mark.parametrize("structure", sorted(set(STRUCTURES) - set(SHARES_AND_OPTIONS)))
def test_a_single_exit_child_also_fits_and_an_exit_that_is_not_the_root_contract_does_not(structure):
    only_stop = proposal(structure, exits=True)
    order = to_native(only_stop.to_order(), SHEETS, "schwab")
    order["childOrderStrategies"] = [order["childOrderStrategies"][0]["childOrderStrategies"][1]]          # one single exit under the trigger
    assert len(native.to_proposals(7, order, created_by="isaac-7", line="SWO")) == 2
    stray = {"orderStrategyType": "SINGLE", "session": "NORMAL", "duration": "GOOD_TILL_CANCEL", "orderType": "LIMIT", "price": 1.0,
             "orderLegCollection": [{"instruction": "SELL_TO_CLOSE", "quantity": 1, "instrument": {"symbol": "AAPL  261218C00220000", "assetType": "OPTION"}}]}
    order["childOrderStrategies"] = [stray]
    with pytest.raises(Unconvertible):
        native._walked(7, order, created_by="isaac-7", line="SWO")


def test_a_shape_the_native_filter_does_not_read_is_cannot_check_and_the_desk_never_builds_it():
    nested = {"orderStrategyType": "TRIGGER", "orderType": "LIMIT", "price": 1.0, "session": "NORMAL", "duration": "DAY",
              "orderLegCollection": to_native(proposal("long_call", exits=False).to_order(), SHEETS, "schwab")["orderLegCollection"],
              "childOrderStrategies": [{"orderStrategyType": "TRIGGER", "orderType": "LIMIT", "price": 1.0, "orderLegCollection": [],
                                        "childOrderStrategies": []}]}
    with pytest.raises(Unconvertible):
        native._walked(7, nested, created_by="isaac-7", line="SWO")
    # the exits of the desk are an OCO of two singles or one single: no structure the desk can propose builds anything else
    for structure in set(STRUCTURES) - set(SHARES_AND_OPTIONS):
        order = to_native(proposal(structure, exits=True).to_order(), SHEETS, "schwab")
        assert order["childOrderStrategies"][0]["orderStrategyType"] == "OCO"
        assert all(branch["orderStrategyType"] == "SINGLE" and "childOrderStrategies" not in branch for branch in order["childOrderStrategies"][0]["childOrderStrategies"])


def test_the_premium_selling_structures_fit_the_forms_and_the_switch_stops_them_before_the_broker():
    """Fitting a form means the filter can READ the order; whether it may go is the guard of the premium switch, which reads every node."""
    for structure in sorted(SELLING_STRUCTURES):
        order = to_native(proposal(structure, exits=True).to_order(), SHEETS, "schwab")
        assert native.to_proposals(7, order, created_by="isaac-7", line="SWO")
