"""The proposal of a SWO desk (SPEC-019 TA-36): a subclass of the order proposal with its structure, premium and expiration."""
import dataclasses
from datetime import date
from decimal import Decimal as D

import pytest

from hokma.desks.swo import schema
from hokma.desks.swo.schema import SWOProposal, derive_structure
from hokma.exec.proposal import Leg, OrderProposal, ProposalError
from trader_support import C200, C210, C220, EXP, P170, P180, P190, STOCK


def L(cid, side, qty=1, effect="open"):
    return Leg(cid, side, effect, D(qty))


def make(structure, legs, net, loss, **kw):
    base = dict(desk_id=3, line="SWO", origin="desk", legs=tuple(legs), order_type="limit", limit_price=abs(D(net)), thesis="why",
                structure=structure, expiration=EXP, net_premium=D(net), max_loss_usd=None if loss is None else D(loss), underlying="AAPL")
    base.update(kw)
    return SWOProposal(**base)


CASES = {
    "long_call": ([L(C200, "buy")], "5.2", "520"),
    "long_put": ([L(P190, "buy")], "2.5", "250"),
    "debit_call_spread": ([L(C200, "buy"), L(C210, "sell")], "3.4", "340"),
    "debit_put_spread": ([L(P190, "buy"), L(P180, "sell")], "1.3", "130"),
    "short_put": ([L(P190, "sell")], "-2.4", "18760"),
    "cash_secured_put": ([L(P190, "sell")], "-2.4", "18760"),
    "short_call": ([L(C200, "sell")], "-5.1", None),
    "credit_call_spread": ([L(C200, "sell"), L(C210, "buy")], "-3.3", "670"),
    "credit_put_spread": ([L(P190, "sell"), L(P180, "buy")], "-1.3", "870"),
    "iron_condor": ([L(P170, "buy"), L(P180, "sell"), L(C220, "sell"), L(C210, "buy")], "-1.0", "900"),
    "short_straddle": ([L("OPT:AAPL:2026-12-18:P:200", "sell"), L(C200, "sell")], "-9.0", None),
    "short_strangle": ([L(P190, "sell"), L(C200, "sell")], "-7.5", None),
    "covered_call": ([L(STOCK, "buy", 100), L(C210, "sell")], "-1.8", "19820"),
    # the two structures of the Strategies panel (SPEC-028, 3339-B): the butterfly shares its body, the protective put buys the shares and the put
    "iron_butterfly": ([L(P190, "buy"), L("OPT:AAPL:2026-12-18:P:200", "sell"), L(C200, "sell"), L(C210, "buy")], "-3.7", "630"),
    "protective_put": ([L(STOCK, "buy", 100), L(P190, "buy")], "2.5", "1250"),
}


def test_TA_36_all_fifteen_structures_are_derived_from_their_legs():
    assert set(CASES) == set(schema.STRUCTURES) and len(CASES) == 15
    for name, (legs, net, loss) in CASES.items():
        if name == "iron_condor":
            legs = [L(P170, "buy"), L(P180, "sell"), L(C210, "sell"), L(C220, "buy")]
        p = make(name, legs, net, loss)
        assert isinstance(p, OrderProposal) and isinstance(p, SWOProposal) and p.structure == name


def test_3339B_the_butterfly_and_the_condor_are_told_apart_by_their_body_and_the_protective_put_needs_the_shares_and_a_bought_put():
    body = "OPT:AAPL:2026-12-18:P:200"
    butterfly = [L(P190, "buy"), L(body, "sell"), L(C200, "sell"), L(C210, "buy")]
    condor = [L(P170, "buy"), L(P180, "sell"), L(C200, "sell"), L(C210, "buy")]
    assert derive_structure(butterfly) == "iron_butterfly" and derive_structure(condor) == "iron_condor"
    assert derive_structure([L(P190, "buy"), L(body, "sell"), L(C220, "sell"), L(C210, "buy")]) is None         # the puts' body above the calls' body: neither
    assert derive_structure([L(STOCK, "buy", 100), L(P190, "buy")]) == "protective_put"
    assert derive_structure([L(STOCK, "buy", 50), L(P190, "buy")]) is None                                        # not the shares of one contract
    assert derive_structure([L(STOCK, "buy", 100), L(P190, "sell")]) is None
    assert derive_structure([L(STOCK, "sell", 100), L(P190, "buy")]) is None
    with pytest.raises(ProposalError, match="they make a protective_put"):
        make("covered_call", [L(STOCK, "buy", 100), L(P190, "buy")], "-1.0", "100")
    with pytest.raises(ProposalError, match="above zero"):
        make("protective_put", [L(STOCK, "buy", 100), L(P190, "buy")], "-2.5", "1250")                      # the put is paid: a debit


def test_TA_36_it_is_a_subclass_of_the_order_proposal_and_keeps_its_fingerprint():
    p = make("debit_call_spread", *CASES["debit_call_spread"][:1], "3.4", "340")
    order = p.to_order()
    assert type(order) is OrderProposal and order.fingerprint() == p.fingerprint()
    assert order.thesis.startswith("[debit_call_spread on AAPL, expires 2026-12-18, net debit 3.4 per share, most it can lose: 340 USD.]")
    assert order.thesis.endswith("why") and "structure" not in p.canonical()


def test_TA_36_a_name_that_is_not_what_the_legs_make_is_refused():
    legs = CASES["debit_call_spread"][0]
    for wrong in ("credit_call_spread", "long_call", "iron_condor"):
        with pytest.raises(ProposalError):
            make(wrong, legs, "3.4" if wrong != "credit_call_spread" else "-3.4", "340")
    with pytest.raises(ProposalError, match="they make a debit_call_spread"):
        make("credit_call_spread", legs, "-3.4", "340")
    # shapes that are none of the thirteen
    assert derive_structure([L(C200, "buy"), L(C210, "buy")]) is None
    assert derive_structure([L(C200, "buy", 1), L(C210, "sell", 2)]) is None
    assert derive_structure([L(C200, "buy"), L("OPT:AAPL:2027-01-15:C:210", "sell")]) is None
    assert derive_structure([L(STOCK, "buy", 50), L(C210, "sell")]) is None
    assert derive_structure([L(C200, "buy"), L(C210, "sell"), L(P190, "sell")]) is None


def test_TA_36_the_sign_of_the_premium_and_the_bound_of_the_loss_are_checked():
    legs = CASES["debit_call_spread"][0]
    with pytest.raises(ProposalError, match="above zero"):
        make("debit_call_spread", legs, "-3.4", "340")
    with pytest.raises(ProposalError, match="below zero"):
        make("short_put", CASES["short_put"][0], "2.4", "100")
    with pytest.raises(ProposalError):
        make("short_call", CASES["short_call"][0], "-5.1", "100")          # an unbounded loss is never given a bound
    with pytest.raises(ProposalError):
        make("debit_call_spread", legs, "3.4", None)                       # a bounded loss must be given
    with pytest.raises(ProposalError):
        make("debit_call_spread", legs, "0", "340")


def test_TA_36_every_leg_is_on_the_underlying_and_the_expiration_is_the_nearest():
    legs = CASES["debit_call_spread"][0]
    with pytest.raises(ProposalError, match="underlying"):
        make("debit_call_spread", legs, "3.4", "340", underlying="MSFT")
    with pytest.raises(ProposalError, match="nearest expiration"):
        make("debit_call_spread", legs, "3.4", "340", expiration=date(2026, 12, 19))
    with pytest.raises(ProposalError, match="opens risk"):
        make("long_call", [L(C200, "buy", effect="close")], "5.2", "520")
    with pytest.raises(ProposalError):
        make("long_call", [L("FUT:NQ:2026-12", "buy")], "5.2", "520")
    with pytest.raises(ProposalError):
        make("long_call", legs[:1], "5.2", "520", line="DT")


def test_TA_36_unknown_keys_are_refused_and_the_dict_round_trips():
    body = {"line": "SWO", "order_type": "limit", "limit_price": "3.4", "thesis": "why", "structure": "debit_call_spread", "expiration": "2026-12-18",
            "net_premium": "3.4", "max_loss_usd": "340", "underlying": "aapl",
            "legs": [{"contract_id": C200, "side": "buy", "effect": "open", "qty": 1}, {"contract_id": C210, "side": "sell", "effect": "open", "qty": 1}]}
    p = SWOProposal.from_dict(body, desk_id=3, origin="desk", created_by="trader")
    assert p.underlying == "AAPL" and p.max_loss_usd == D(340) and p.created_by == "trader"
    again = SWOProposal.from_dict({**p.to_tool_body(), "line": "SWO"}, desk_id=3, origin="desk", created_by="trader")
    assert again == dataclasses.replace(p, thesis=again.thesis) and again.fingerprint() == p.fingerprint()
    for bad in ({**body, "color": "red"}, {**body, "structure": None}, {**body, "expiration": "soon"}, {**body, "net_premium": "x"},
                {k: v for k, v in body.items() if k != "underlying"}, "not a dict"):
        with pytest.raises(ProposalError):
            SWOProposal.from_dict(bad, desk_id=3, origin="desk")
    with pytest.raises(ProposalError):
        SWOProposal.from_dict({**body, "legs": [{**body["legs"][0], "color": "red"}]}, desk_id=3, origin="desk")


def test_TA_36_every_field_has_a_description_that_instructs_the_model():
    descriptions = SWOProposal.field_descriptions()
    assert set(descriptions) == {f.name for f in dataclasses.fields(SWOProposal)}
    for name, text in descriptions.items():
        assert len(text) >= 40, name
    for name in ("stop_loss", "take_profit", "risk_usd", "desk_id", "line", "origin", "created_by"):
        assert "Never write it" in descriptions[name], name
    for name in schema.SWO_KEYS:
        assert dataclasses.fields(SWOProposal)[[f.name for f in dataclasses.fields(SWOProposal)].index(name)].metadata["description"]


def test_TA_36_the_structure_names_are_the_ones_of_the_options_module():
    from hokma.quant.options.structures import CATALOG as module_structures

    assert set(schema.STRUCTURES) == set(module_structures)
    for name, spec in module_structures.items():
        assert (name in schema.DEBIT_STRUCTURES) == (spec["class"] == "debit")
    assert schema.UNBOUNDED_LOSS == {"short_call", "short_straddle", "short_strangle"}


def test_TA_36_the_tool_body_has_no_field_the_tool_sets_itself():
    p = make("long_call", CASES["long_call"][0], "5.2", "520")
    body = p.to_tool_body()
    assert not {"desk_id", "origin", "created_by"} & set(body) and body["structure"] == "long_call" and body["net_premium"] == "5.2"
    assert body["legs"][0] == {"contract_id": C200, "effect": "open", "qty": "1", "side": "buy"}
