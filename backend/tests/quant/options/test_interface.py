"""Interface of the options module: registry coverage of every row of the block, bad data handling and hard rules."""
import ast
from pathlib import Path

import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.types import QuantResult

from .conftest import call

ROOT = Path(__file__).resolve().parents[4]
OPTIONS_DIR = ROOT / "backend" / "hokma" / "quant" / "options"

# the rows of block B1 (SVC-options) that are functions of the module
ROW_IDS = ["ECT-045", "FM-11", "FM-12", "FT-019", "FT-020", "FT-021", "FT-022", "FT-023", "FT-024", "FT-025", "FT-026", "FT-027", "FT-028", "FT-029",
           "FT-030", "FT-031", "FT-032", "FT-033", "FT-034", "FT-035", "FT-036", "FT-037", "FT-038", "FT-039", "FT-040", "AFS-005", "ECT-031",
           "ECT-098", "ECT-033"]
PATHS = {  # row id -> (file, function) of implementado_en
    "FM-11": ("greeks", "gamma"), "FM-12": ("greeks", "vanna"), "FT-019": ("pricing", "d1_d2"), "FT-020": ("pricing", "black_scholes"),
    "FT-021": ("iv", "implied_volatility"), "FT-022": ("pricing", "put_call_parity"), "FT-023": ("pricing", "garman_kohlhagen"),
    "FT-024": ("exotic", "binary"), "FT-025": ("pricing", "bjerksund_stensland"), "FT-026": ("greeks", "first_order"), "FT-027": ("greeks", "gamma"),
    "FT-028": ("greeks", "dual_gamma"), "FT-029": ("greeks", "vanna"), "FT-030": ("greeks", "second_order"), "FT-031": ("greeks", "second_order_partial"),
    "FT-032": ("greeks", "third_order"), "FT-034": ("pricing", "monte_carlo"), "FT-037": ("exotic", "barrier_asian"), "FT-038": ("surface", "svi"),
    "FT-039": ("surface", "risk_neutral_density"), "FT-040": ("surface", "surface"),
}
FILE_ONLY = {"ECT-045": "trees", "FT-035": "trees", "FT-036": "payoff", "FT-033": "chain", "AFS-005": "vol_analysis", "ECT-031": "position_greeks",
             "ECT-098": "structures", "ECT-033": "liquidity"}

# a call of every function with a NaN where a number belongs: value None and warnings, never an exception
NAN = float("nan")
LEG = {"position": "long", "kind": "call", "strike": 100.0, "premium": 1.0}
BSM = {"spot": NAN, "strike": 100.0, "rate": 0.05, "volatility": 0.2, "time_to_expiration": 1.0}
NAN_CALLS = {
    "options.d1_d2": BSM, "options.black_scholes": BSM, "options.put_call_parity": {"spot": NAN, "strike": 100.0, "rate": 0.05, "time_to_expiration": 1.0, "call_price": 1.0, "put_price": 1.0},
    "options.garman_kohlhagen": {**BSM, "foreign_rate": 0.01}, "options.bjerksund_stensland": BSM, "options.monte_carlo": BSM,
    "options.implied_volatility": {"market_price": NAN, "spot": 100.0, "strike": 100.0, "rate": 0.05, "time_to_expiration": 1.0},
    "options.first_order": BSM, "options.gamma": BSM, "options.dual_gamma": BSM, "options.vanna": BSM, "options.second_order": BSM,
    "options.second_order_partial": BSM, "options.third_order": BSM, "options.binomial_tree": {**BSM, "n_steps": 10},
    "options.tree_convergence": {**BSM, "steps": [10]}, "options.early_assignment_risk": {"spot": NAN, "strike": 95.0, "rate": 0.05, "dividend": 1.0, "years_to_next_payment": 0.25, "call_price": 5.0},
    "options.binary": BSM, "options.barrier_asian": {"exotic": "asian", **BSM},
    "options.payoff": {"legs": [LEG], "multiplier": NAN, "spot": 100.0}, "options.svi": {"log_moneyness": [NAN] * 6, "implied_vol": [0.2] * 6, "time_to_expiration": 0.5},
    "options.svi_calendar": {"slices": {"1.0": {"a": NAN, "b": 0.1, "rho": 0.0, "m": 0.0, "sigma": 0.1}, "2.0": {"a": 0.1, "b": 0.1, "rho": 0.0, "m": 0.0, "sigma": 0.1}}},
    "options.risk_neutral_density": {"spot": NAN, "rate": 0.03, "time_to_expiration": 0.5, "svi_params": {"a": 0.01, "b": 0.1, "rho": 0.0, "m": 0.0, "sigma": 0.1}},
    "options.surface": {"contracts": [], "spot": 100.0, "rate": 0.03, "as_of": "2026-11-20T15:30:00-05:00"},
    "options.position_greeks": {"legs": [{"kind": "call", "strike": 100.0, "volatility": NAN, "quantity": 1, "time_to_expiration": 0.5}], "spot": 100.0, "rate": 0.03, "multiplier": 100},
    "options.rebalance_check": {"delta_shares": NAN, "vega_usd_per_point": 1.0, "delta_band_shares": 1.0, "vega_limit_usd": 1.0},
    "options.gamma_scalp": {"gamma": NAN, "theta_per_day": -0.1, "quantity": 1, "multiplier": 100, "move": 1.0},
    "options.vol_richness": {"contracts": [], "spot": 100.0, "rate": 0.03, "as_of": "2026-11-20T15:30:00-05:00", "expiration": "2026-12-18", "closes": [100.0] * 30},
    "options.liquidity": {"bid": NAN, "ask": 1.0}, "options.chain_liquidity": {"contracts": []},
    "options.structure_matrix": {"direction": "sideways", "vol_view": "flat"}, "options.classify_structure": {"legs": []},
    "options.structure_profile": {"structure": "long_call", "strikes": [NAN], "premiums": [1.0], "quantity": 1, "multiplier": 100},
    "options.management_plan": {"structure": "long_call", "net_premium_usd": NAN, "max_profit_usd": 1.0, "max_loss_usd": 1.0, "days_to_expiration": 5.0, "equity_usd": 1000.0},
}
NETWORK = {"options.expiry_dates", "options.option_chain"}        # covered in test_chain.py with a fake market module


def test_every_row_of_the_block_has_a_registered_function():
    covered = {pid for spec in registry.list("options") for pid in spec.piece_ids}
    assert set(ROW_IDS) | {"FT-035"} <= covered, sorted((set(ROW_IDS) | {"FT-035"}) - covered)


def test_registered_names_are_module_dot_function_with_a_description():
    specs = registry.list("options")
    assert len(specs) == len(NAN_CALLS) + len(NETWORK)
    for spec in specs:
        assert spec.module == "options" and spec.name.startswith("options.")
        assert spec.summary and spec.inputs and spec.piece_ids and not spec.blocked
        assert spec.fn is not None and spec.fn.__module__.startswith("hokma.quant.options.")
    assert {s.name for s in specs} == set(NAN_CALLS) | NETWORK


@pytest.mark.parametrize("name", sorted(NAN_CALLS))
def test_nan_or_empty_input_gives_none_with_warnings(name):
    res = call(name, **NAN_CALLS[name])
    assert isinstance(res, QuantResult) and res.value is None and res.warnings, name
    assert res.piece_ids == registry.describe(name).piece_ids and res.name == name


def test_unknown_and_missing_arguments_are_refused_by_the_registry():
    with pytest.raises(BadInput):
        call("options.black_scholes", spot=100.0, strike=100.0, rate=0.05, volatility=0.2, time_to_expiration=1.0, nonsense=1)
    with pytest.raises(BadInput):
        call("options.black_scholes", spot=100.0)


def test_the_path_of_every_row_exists():
    for row, (module, function) in PATHS.items():
        tree = ast.parse((OPTIONS_DIR / f"{module}.py").read_text(encoding="utf-8"))
        assert function in {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}, (row, module, function)
        assert row in {pid for spec in registry.list("options") if spec.fn.__name__ == function and spec.fn.__module__ == f"hokma.quant.options.{module}" for pid in spec.piece_ids}, row
    for row, module in FILE_ONLY.items():
        assert (OPTIONS_DIR / f"{module}.py").is_file()
        assert row in {pid for spec in registry.list("options") if spec.fn.__module__ == f"hokma.quant.options.{module}" for pid in spec.piece_ids}, row


def test_only_the_lib_file_imports_restricted_libraries_and_no_file_has_an_em_dash():
    restricted = {"scipy", "financetoolkit", "yfinance", "statsmodels", "skfolio"}
    for path in OPTIONS_DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "—" not in text, path.name
        if path.name == "_lib.py":
            continue
        for node in ast.walk(ast.parse(text)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else ([node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
            assert not [n for n in names if n.split(".")[0] in restricted], (path.name, names)


def test_the_contract_multiplier_never_has_a_written_default():
    import inspect

    seen = 0
    for spec in registry.list("options"):
        params = inspect.signature(spec.fn).parameters
        if "multiplier" in params:
            seen += 1
            assert params["multiplier"].default in (inspect.Parameter.empty, None), (spec.name, params["multiplier"].default)
            assert spec.inputs["multiplier"].get("default") is None, spec.name
    assert seen >= 5          # payoff, position_greeks, gamma_scalp, liquidity and structure_profile, management of the sheet is the caller's job
    text = (OPTIONS_DIR / "payoff.py").read_text(encoding="utf-8") + (OPTIONS_DIR / "position_greeks.py").read_text(encoding="utf-8")
    assert "DEFAULT_MULTIPLIER" not in text and "multiplier=100" not in text and "multiplier = 100" not in text


def test_functions_registered_by_the_module_are_not_blocked_and_use_decimals_only_as_input():
    from decimal import Decimal

    res = call("options.gamma_scalp", gamma=0.1, theta_per_day=Decimal("-0.1"), quantity=1, multiplier=Decimal("100"), move=1.0)
    assert res.value is not None and isinstance(res.value["gamma_pnl_usd"], float)


def test_functions_run_in_a_worker_process_and_come_back_whole(monkeypatch, chain):
    """The registry runs package functions in a worker that can be killed: arguments and results must cross the pipe."""
    from hokma.quant import runner

    monkeypatch.setattr(registry, "IN_PROCESS", False)
    try:
        bs = registry.call("options.black_scholes", spot=100.0, strike=100.0, rate=0.05, volatility=0.2, time_to_expiration=1.0)
        assert bs.value == pytest.approx(10.450583572185565, abs=1e-9) and bs.piece_ids == ("FT-020",)
        tree = registry.call("options.binomial_tree", spot=100.0, strike=100.0, rate=0.05, volatility=0.3, time_to_expiration=0.75, n_steps=3, style="european")
        assert tree.value["price"] == pytest.approx(12.92, abs=5e-3)
        res = registry.call("options.surface", contracts=chain["contracts"], spot=chain["underlying_price"], rate=chain["rate"], as_of=chain["as_of"],
                            dividend_yield=chain["dividend_yield"])
        assert len(res.value["expirations"]) == 5
        empty = registry.call("options.black_scholes", spot=float("nan"), strike=100.0, rate=0.05, volatility=0.2, time_to_expiration=1.0)
        assert empty.value is None and empty.warnings
        from decimal import Decimal

        gs = registry.call("options.gamma_scalp", gamma=0.15, theta_per_day=-0.125, quantity=1, multiplier=Decimal("100"), move=10.0)
        assert gs.value["theta_days_paid_by_move"] == pytest.approx(60.0)
        with pytest.raises(BadInput):
            registry.call("options.binomial_tree", spot=100.0, strike=100.0, rate=0.05, volatility=0.3, time_to_expiration=0.75, n_steps=5001)
    finally:
        runner.shutdown()
