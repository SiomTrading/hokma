"""Commission tables per broker (SPEC-020: L-16, L-17): every value is worked out by hand in its comment.

The tables are read by name from backend/hokma/broker/fees/ by the backtest (hokma.backtest.costs) and by the simulated broker of H2 (the rule of a table
is the commission configuration that hokma.broker.sim_engine charges); the tests prove that both give the same amount from the same file."""
import json
import re
from decimal import Decimal as D
from pathlib import Path

import pytest

from hokma.backtest import costs
from hokma.backtest.spec import CostModel, SpecError
from hokma.broker import costs as broker_costs
from hokma.broker import fees, sim_engine
from hokma.db.errors import ValidationError

NEW = ("alpaca", "coinbase", "ibkr_cfd", "ibkr_futures", "ibkr_micro_futures", "ibkr_options", "ibkr_stocks", "kraken", "tastytrade", "tradestation")
NQ = (D("25000"), D(20))                                     # a price of NQ and its multiplier: the per contract rules do not read them


def raw(name: str) -> dict:
    return json.loads((fees.FEES_DIR / f"{name}.json").read_text(encoding="utf-8"))


def charge(name: str, asset: str, qty: str, price: str = "100", multiplier: str = "1") -> D:
    return fees.fee(fees.load(name), asset, qty=D(qty), price=D(price), multiplier=D(multiplier))


# ---------------------------------------------------------------------------------------------------------------- the headers
@pytest.mark.parametrize("name", NEW)
def test_every_table_has_its_source_its_date_and_says_what_it_leaves_out(name):
    table = raw(name)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", table["checked_on"]) and table["source"] and table["currency"] == "USD"
    assert table["verified_against_broker"] is False, "a value is marked as verified only after it was checked against a real statement"
    assert isinstance(table["omitted"], list) and table["omitted"] and all(isinstance(x, str) and x for x in table["omitted"])
    assert table["checked_on"] in table["reference"] and "not yet checked against a real statement" in table["reference"]
    assert table["broker"] == name and set(table["rules"]) <= {"equity", "etf", "option", "future", "cfd", "crypto", "crypto_maker"}


def test_the_tables_are_listed_and_load_by_name_and_with_the_extension():
    assert set(NEW) | {"schwab", "zero"} <= set(fees.tables())
    for name in NEW:
        assert fees.load(name) is fees.load(f"{name}.json") and fees.load(name).broker == name


def test_a_rule_that_is_not_in_a_table_is_refused_and_a_value_is_never_invented():
    with pytest.raises(ValidationError, match="no rule"):
        charge("ibkr_futures", "equity", "1")
    for name in NEW:                     # no table carries a swap rate: nothing in the sources gives one
        assert fees.load(name).swap is None


# ---------------------------------------------------------------------------------------------------------------- L-16: futures
def test_one_nq_pays_2_15_per_side_the_sum_of_the_three_parts_of_the_price_list():
    table = raw("ibkr_futures")
    assert sum(D(v) for v in table["components"].values()) == D("0.85") + D("1.28") + D("0.02") == D("2.15")
    assert fees.load("ibkr_futures").rules["future"].value == D("2.15")
    assert charge("ibkr_futures", "future", "1", *map(str, NQ)) == D("2.15")                  # 1 NQ
    assert charge("ibkr_futures", "future", "3", "6800", "50") == D("6.45")                    # 3 ES: 3 x 2.15, whatever the multiplier is
    assert charge("ibkr_futures", "future", "0", *map(str, NQ)) == D(0)                       # nothing traded, nothing paid


def test_one_micro_contract_pays_0_57_per_side_the_sum_of_its_three_parts():
    table = raw("ibkr_micro_futures")
    assert sum(D(v) for v in table["components"].values()) == D("0.25") + D("0.30") + D("0.02") == D("0.57")
    assert charge("ibkr_micro_futures", "future", "1", "25000", "2") == D("0.57")             # 1 MNQ
    assert charge("ibkr_micro_futures", "future", "10", "6800", "5") == D("5.70")             # 10 MES


def test_the_backtest_and_the_simulated_broker_read_the_same_file_for_one_nq():
    spec = costs.fee_model(CostModel("ibkr_futures"), "future")
    assert spec.table is fees.load("ibkr_futures")                                            # the same cached object, read from the same file
    assert costs.commission_usd(spec, qty=D(1), price=NQ[0], multiplier=NQ[1]) == D("2.15")
    # the simulated broker charges the commission configuration of an account: the rule of the table is that configuration (per contract = per unit)
    rule = spec.rule
    account = broker_costs.validate_commission({"type": "per_share", "value": float(rule.value), "min": float(rule.minimum)})
    assert sim_engine.leg_commission(NQ[0], D(1), NQ[1], account) == D("2.15")
    assert sim_engine.leg_commission(NQ[0], D(3), NQ[1], account) == D("6.45")
    assert D(str(broker_costs.commission(float(NQ[0] * NQ[1]), 3.0, account))) == charge("ibkr_futures", "future", "3", *map(str, NQ))


@pytest.mark.parametrize("name, asset, qty, price, mult", [("ibkr_micro_futures", "future", "7", "25000", "2"), ("ibkr_options", "option", "12", "1.5", "100"),
                                                           ("tastytrade", "future", "5", "25000", "20"), ("tradestation", "future", "4", "6800", "50"),
                                                           ("ibkr_stocks", "equity", "350", "40", "1")])
def test_the_account_configuration_of_a_per_unit_rule_charges_what_the_table_charges(name, asset, qty, price, mult):
    rule = fees.load(name).rules[asset]
    account = broker_costs.validate_commission({"type": "per_share", "value": float(rule.value), "min": float(rule.minimum)})
    assert sim_engine.leg_commission(D(price), D(qty), D(mult), account) == fees.fee(fees.load(name), asset, qty=D(qty), price=D(price), multiplier=D(mult))


def test_a_basis_point_rule_is_the_account_configuration_in_basis_points():
    rule = fees.load("coinbase").rules["crypto"]
    account = broker_costs.validate_commission({"type": "bps", "value": float(rule.value), "min": float(rule.minimum)})
    assert sim_engine.leg_commission(D("50000"), D(1), D(1), account) == fees.fee(fees.load("coinbase"), "crypto", qty=D(1), price=D("50000"))


# ---------------------------------------------------------------------------------------------------------------- L-17: the rest of the brokers
def test_stocks_pay_half_a_cent_a_share_with_a_minimum_of_one_dollar():
    assert charge("ibkr_stocks", "equity", "100", "50") == D("1.00")          # 100 x 0.005 = 0.50, the minimum is 1.00
    assert charge("ibkr_stocks", "etf", "1000", "50") == D("5.00")            # 1000 x 0.005
    assert charge("ibkr_stocks", "equity", "10", "50") == D("1.00")           # 0.05 against the minimum
    assert fees.load("ibkr_stocks").rules["equity"].cap_bps is None          # no cap is written: the loader order differs from the price list below 200 dollars


def test_options_pay_65_cents_a_contract_with_a_minimum_of_one_dollar():
    assert charge("ibkr_options", "option", "1", "2.50", "100") == D("1.00")          # 0.65 against the minimum
    assert charge("ibkr_options", "option", "3", "2.50", "100") == D("1.95")          # 3 x 0.65
    assert charge("ibkr_options", "option", "10", "2.50", "100") == D("6.50")         # 10 x 0.65
    assert charge("tradestation", "option", "5", "2.50", "100") == D("3.00")          # 5 x 0.60
    assert charge("tastytrade", "option", "4", "2.50", "100") == D("4.00")            # 4 x 1.00 to open


def test_futures_of_the_other_brokers_pay_per_contract():
    assert charge("tastytrade", "future", "2", *map(str, NQ)) == D("2.50")            # 2 x 1.25
    assert charge("tradestation", "future", "2", *map(str, NQ)) == D("3.00")          # 2 x 1.50
    assert charge("tradestation", "equity", "500", "40") == D(0) == charge("alpaca", "equity", "500", "40") == charge("alpaca", "option", "5", "2", "100")


def test_cfds_pay_two_tenths_of_a_basis_point_with_a_minimum_of_one_dollar():
    assert charge("ibkr_cfd", "cfd", "1", "10000") == D("1.00")                       # 10000 x 0.2 / 10000 = 0.20, the minimum is 1.00
    assert charge("ibkr_cfd", "cfd", "50", "10000") == D("10.00")                     # 500000 x 0.2 / 10000 = 10.00


def test_crypto_pays_a_share_of_the_value_and_a_maker_order_pays_less():
    assert charge("alpaca", "crypto", "0.5", "60000") == D("75.00")                   # 30000 x 0.25 percent
    assert charge("alpaca", "crypto_maker", "0.5", "60000") == D("45.00")             # 30000 x 0.15 percent
    assert charge("coinbase", "crypto", "1", "50000") == D("400.00")                  # 50000 x 0.80 percent
    assert charge("coinbase", "crypto_maker", "1", "50000") == D("300.00")            # 50000 x 0.60 percent
    assert charge("kraken", "crypto", "1", "50000") == D("130.00")                    # 50000 x 0.26 percent
    assert charge("kraken", "crypto_maker", "1", "50000") == D("80.00")               # 50000 x 0.16 percent
    for name in ("alpaca", "coinbase", "kraken"):
        assert fees.load(name).rules["crypto_maker"].value < fees.load(name).rules["crypto"].value


# ---------------------------------------------------------------------------------------------------------------- the backtest reads them
def test_the_backtest_picks_a_table_by_name_and_refuses_an_asset_the_table_has_no_rule_for():
    assert costs.commission_usd(costs.fee_model(CostModel("ibkr_stocks"), "etf"), qty=D(200), price=D(30)) == D("1.00")
    assert costs.commission_usd(costs.fee_model(CostModel("coinbase"), "crypto"), qty=D(2), price=D("40000")) == D("640.00")      # 80000 x 0.80 percent
    with pytest.raises(SpecError):
        costs.fee_model(CostModel("alpaca"), "future")
    with pytest.raises(SpecError):
        costs.fee_model(CostModel("tradestation"), "crypto")


def test_the_json_files_are_the_whole_list_and_nothing_else_is_read(tmp_path):
    assert {p.stem for p in Path(fees.FEES_DIR).glob("*.json")} == set(fees.tables())
    for bad in ("../ibkr_futures", "ibkr futures", "IBKR_FUTURES", "ibkr_futures/../zero"):
        with pytest.raises(ValidationError):
            fees.load(bad)
