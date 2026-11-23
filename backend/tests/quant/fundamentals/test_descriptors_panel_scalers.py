"""SK-090 to SK-104: descriptors, the asset panel with its inactive policy and the cross sectional scalers."""
import math
from statistics import NormalDist

import numpy as np
import pandas as pd
import pytest

from hokma.quant import limits
from hokma.quant.errors import BadInput
from hokma.quant.fundamentals import _lib, descriptors
from hokma.quant.fundamentals.panel import AssetPanel, Field2D, Field3D, FieldCategorical, build_panel, infer_active

from .conftest import call, piece_ids

TOL = 1e-8


@pytest.fixture(scope="module")
def fields(returns_panel):
    """Wide frames over the 1,000 days and five assets of the returns fixture (hand friendly constants for the fundamentals)."""
    rp = returns_panel
    n = len(rp)
    price = (1 + rp).cumprod() * 50
    shares = pd.DataFrame(np.tile([100.0, 200, 300, 400, 500], (n, 1)), index=rp.index, columns=rp.columns)
    mc = price * shares
    return {"returns": rp, "market_cap": mc, "adj_close": price, "book_equity": mc * 0.5, "sales_ttm": mc * 0.8, "net_income_ttm": mc * 0.05,
            "operating_cash_flow_ttm": mc * 0.07, "total_assets": mc * 1.2, "total_debt": mc * 0.3, "cost_of_revenue_ttm": mc * 0.5,
            "adj_volume": shares * 0.01, "adj_shares_outstanding": shares, "dividends_ttm": mc * 0.01, "net_buybacks_ttm": mc * 0.005,
            "enterprise_value": mc * 1.1, "ebitda_ttm": mc * 0.1, "eps_ntm": price * 0.06, "eps_ntm_std": price * 0.01, "dps_ntm": price * 0.012,
            "short_interest": shares * 0.05, "capex_ttm": mc * 0.04}


def M(values):
    """The 2D list of a result as a float array (None is NaN)."""
    return np.array(values, dtype=float)


def latest(res, asset="AAA"):
    return res.value["latest"][asset]


def run(fields, name, **kw):
    return call(f"fundamentals.{name}", fields=fields, **kw)


def test_descriptor_base_and_catalog_SK_090(fields):
    cat = call("fundamentals.descriptor_catalog").value
    assert set(cat) == set(descriptors.BY_NAME) and len(cat) == 46
    assert cat["book_to_price"]["fields"] == ["book_equity", "market_cap"] and cat["ew_momentum"]["params"]["half_life"] == 87.0
    assert all(piece_ids(f"fundamentals.{n}")[0] == s["piece"] for n, s in cat.items())
    res = call("fundamentals.descriptor_set", fields=fields, names=["book_to_price", "log_market_cap", "ew_volatility"])
    assert res.value["latest"]["book_to_price"]["AAA"] == pytest.approx(0.5, abs=TOL)
    assert res.value["latest"]["log_market_cap"]["BBB"] == pytest.approx(math.log(fields["market_cap"].iloc[-1]["BBB"]), abs=TOL)
    # a descriptor that cannot run is listed with its reason; the others still run
    partial = call("fundamentals.descriptor_set", fields={"market_cap": fields["market_cap"]}, names=["log_market_cap", "book_to_price"])
    assert list(partial.value["latest"]) == ["log_market_cap"] and partial.value["skipped"]["book_to_price"] == ["data_missing:book_equity"]
    assert call("fundamentals.descriptor_set", fields={"market_cap": fields["market_cap"]}, names=["book_to_price"]).value is None
    with pytest.raises(BadInput):
        call("fundamentals.descriptor_set", fields=fields, names=["not_a_descriptor"])
    with pytest.raises(BadInput):
        call("fundamentals.descriptor_set", fields=fields, names=[])
    assert "SK-090" in piece_ids("fundamentals.descriptor_set") and "SK-090" in piece_ids("fundamentals.descriptor_catalog")


def test_value_descriptors_SK_091(fields):
    mc = fields["market_cap"].iloc[-1]
    assert latest(run(fields, "book_to_price"), "CCC") == pytest.approx(0.5, abs=TOL)
    assert latest(run(fields, "cash_flow_to_price"), "CCC") == pytest.approx(0.07, abs=TOL)
    assert latest(run(fields, "sales_to_price"), "EEE") == pytest.approx(0.8, abs=TOL)
    # hand value on a three day panel
    small = {"book_equity": pd.DataFrame({"X": [10.0, 12.0, 15.0]}, index=pd.bdate_range("2024-01-01", periods=3)),
             "market_cap": pd.DataFrame({"X": [20.0, 24.0, 25.0]}, index=pd.bdate_range("2024-01-01", periods=3))}
    assert M(run(small, "book_to_price").value["values"]).ravel().tolist() == pytest.approx([0.5, 0.5, 0.6], abs=TOL)
    assert mc.notna().all()
    assert "SK-091" in piece_ids("fundamentals.book_to_price")


def test_earnings_yield_descriptors_SK_092(fields):
    assert latest(run(fields, "earnings_to_price")) == pytest.approx(0.05, abs=TOL)
    assert latest(run(fields, "ebitda_to_enterprise_value")) == pytest.approx(0.1 / 1.1, abs=TOL)
    assert latest(run(fields, "forward_earnings_to_price")) == pytest.approx(0.06, abs=TOL)
    assert "SK-092" in piece_ids("fundamentals.forward_earnings_to_price")


def test_dividend_descriptors_SK_093(fields):
    assert latest(run(fields, "dividend_to_price")) == pytest.approx(0.01, abs=TOL)
    assert latest(run(fields, "forward_dividend_to_price")) == pytest.approx(0.012, abs=TOL)
    assert latest(run(fields, "shareholder_yield_descriptor")) == pytest.approx(0.015, abs=TOL)
    assert "SK-093" in piece_ids("fundamentals.dividend_to_price")


def test_profitability_descriptors_SK_094(fields):
    assert latest(run(fields, "gross_margin_descriptor")) == pytest.approx((0.8 - 0.5) / 0.8, abs=TOL)
    assert latest(run(fields, "gross_profitability")) == pytest.approx(0.3 / 1.2, abs=TOL)
    assert latest(run(fields, "return_on_assets_descriptor")) == pytest.approx(0.05 / 1.2, abs=TOL)
    assert latest(run(fields, "return_on_equity_descriptor")) == pytest.approx(0.05 / 0.5, abs=TOL)
    assert latest(run(fields, "cash_flow_to_assets")) == pytest.approx(0.07 / 1.2, abs=TOL)
    assert latest(run(fields, "asset_turnover_descriptor")) == pytest.approx(0.8 / 1.2, abs=TOL)
    assert latest(run(fields, "sales_to_enterprise_value")) == pytest.approx(0.8 / 1.1, abs=TOL)
    assert "SK-094" in piece_ids("fundamentals.gross_profitability")


def test_growth_descriptors_SK_095(fields):
    sales = fields["sales_ttm"]
    res = run(fields, "sales_growth_rate", lag=5)
    assert M(res.value["values"])[-1].tolist() == pytest.approx((sales.iloc[-1] / sales.iloc[-6] - 1).tolist(), abs=TOL)
    assert np.isnan(M(res.value["values"])[4]).all() and np.isfinite(M(res.value["values"])[5]).all()          # warm up stays empty
    generic = run(fields, "growth_rate", field="sales_ttm", lag=5)
    assert generic.value["latest"] == pytest.approx(res.value["latest"], abs=TOL)
    assets = run(fields, "assets_growth_rate", lag=3)
    assert latest(assets) == pytest.approx(fields["total_assets"].iloc[-1]["AAA"] / fields["total_assets"].iloc[-4]["AAA"] - 1, abs=TOL)
    assert latest(run(fields, "issuance_growth_rate", lag=7)) == pytest.approx(0.0, abs=TOL)
    ecp = run(fields, "earnings_change_to_price", lag=4)
    ni, mc = fields["net_income_ttm"], fields["market_cap"]
    assert latest(ecp) == pytest.approx((ni.iloc[-1]["AAA"] - ni.iloc[-5]["AAA"]) / mc.iloc[-1]["AAA"], abs=TOL)
    cts = run(fields, "change_to_scale", field="net_income_ttm", scale_field="total_assets", lag=4)
    origin = _lib.sk_descriptor.ChangeToScale(field="net_income_ttm", scale_field="total_assets", lag=4).fit_transform(
        _lib.sk_panel({"net_income_ttm": ni, "total_assets": fields["total_assets"]}))
    assert M(cts.value["values"]) == pytest.approx(origin, abs=TOL, nan_ok=True)
    assert run(fields, "change_in_intensity", field="net_income_ttm", scale_field="total_assets", lag=4).value is not None
    assert run(fields, "capex_to_assets_change_in_intensity", lag=4).value is not None
    with pytest.raises(BadInput):
        run(fields, "growth_rate", field="sales_ttm")                  # lag is required
    with pytest.raises(BadInput):
        run(fields, "growth_rate", field="../x", lag=2)
    assert run(fields, "growth_rate", field="not_a_field", lag=2).warnings == ("data_missing:not_a_field",)
    negative = {"sales_ttm": -fields["sales_ttm"]}
    assert run(negative, "sales_growth_rate", lag=2).value is None        # growth over negative sales is refused by the library: a warning, not a crash
    assert "SK-095" in piece_ids("fundamentals.sales_growth_rate")


def test_leverage_descriptors_SK_096(fields):
    assert latest(run(fields, "book_leverage")) == pytest.approx(0.3 / (0.3 + 0.5), abs=TOL)      # debt over debt plus book equity
    assert latest(run(fields, "market_leverage")) == pytest.approx(0.3 / (0.3 + 1.0), abs=TOL)
    assert latest(run(fields, "debt_to_assets")) == pytest.approx(0.3 / 1.2, abs=TOL)
    assert "SK-096" in piece_ids("fundamentals.book_leverage")


def test_earnings_quality_descriptors_SK_097(fields):
    assert latest(run(fields, "accruals_cash_flow")) == pytest.approx((0.05 - 0.07) / 1.2, abs=TOL)
    assert latest(run(fields, "analyst_dispersion_to_price")) == pytest.approx(0.01, abs=TOL)
    assert "SK-097" in piece_ids("fundamentals.accruals_cash_flow")


def test_momentum_and_reversal_descriptors_SK_098(fields):
    r = fields["returns"]
    logs = np.log1p(r)
    window, skip = 60, 5
    res = run(fields, "rolling_momentum", window=window, skip=skip)
    hand = logs.iloc[len(r) - skip - window: len(r) - skip].sum()
    assert res.value["latest"] == pytest.approx(hand.to_dict(), abs=TOL)
    rev = run(fields, "reversal", window=10)
    assert rev.value["latest"] == pytest.approx((-logs.iloc[-10:].sum()).to_dict(), abs=TOL)
    ew = run(fields, "ew_momentum", half_life=30.0, skip=5)
    assert np.isfinite(M(ew.value["values"])[-1]).all()
    origin = _lib.sk_descriptor.EWMomentum(half_life=30.0, skip=5).fit_transform(_lib.sk_panel({"returns": r}))
    assert M(ew.value["values"]) == pytest.approx(origin, abs=TOL, nan_ok=True)
    with pytest.raises(BadInput):
        run(fields, "ew_momentum", half_life=0)
    with pytest.raises(BadInput):
        run(fields, "ew_momentum", half_life=float("nan"))
    with pytest.raises(BadInput):
        run(fields, "rolling_momentum", window=0)
    with pytest.raises(BadInput):
        run(fields, "rolling_momentum", window=limits.PARAM_CAPS["window"] + 1)         # the cap of the registry applies
    assert run({"returns": r.iloc[:5]}, "rolling_momentum", window=252).value is None       # not enough history
    assert "SK-098" in piece_ids("fundamentals.ew_momentum")


def test_volatility_descriptors_SK_099(fields):
    r = fields["returns"]
    res = run(fields, "ew_volatility", half_life=20.0)
    origin = _lib.sk_descriptor.EWVolatility(half_life=20.0).fit_transform(_lib.sk_panel({"returns": r}))
    assert M(res.value["values"]) == pytest.approx(origin, abs=TOL, nan_ok=True)
    # an exponentially weighted volatility of a constant return series is the constant moves around zero: positive and finite
    assert all(v > 0 for v in res.value["latest"].values())
    down = run(fields, "ew_downside_volatility", half_life=20.0).value["latest"]
    assert all(down[a] < res.value["latest"][a] for a in down)         # downside volatility is below total volatility for symmetric returns
    assert run(fields, "ew_residual_volatility").value is not None and run(fields, "ew_residual_downside_volatility").value is not None
    assert "SK-099" in piece_ids("fundamentals.ew_volatility")


def test_sensitivity_descriptors_SK_100(fields):
    beta = run(fields, "ew_market_beta", half_life=60.0)
    origin = _lib.sk_descriptor.EWMarketBeta(half_life=60.0).fit_transform(_lib.sk_panel({"returns": fields["returns"], "market_cap": fields["market_cap"]}))
    assert M(beta.value["values"]) == pytest.approx(origin, abs=TOL, nan_ok=True)
    # a cap weighted market made of the assets themselves: the average beta weighted by capitalization is close to one
    weights = fields["market_cap"].iloc[-1] / fields["market_cap"].iloc[-1].sum()
    assert sum(weights[a] * beta.value["latest"][a] for a in weights.index) == pytest.approx(1.0, abs=0.05)
    assert run(fields, "ew_downside_beta").value is not None
    ref = pd.Series(fields["returns"].mean(axis=1).to_numpy(), index=fields["returns"].index)
    assert run(fields, "ew_macro_sensitivity", reference_returns=ref).value is not None
    assert run(fields, "ew_macro_sensitivity", reference_returns=ref.iloc[:10]).value is None        # length must match: a warning, not a crash
    assert "SK-100" in piece_ids("fundamentals.ew_market_beta")


def test_liquidity_descriptors_SK_101(fields):
    assert latest(run(fields, "ew_share_turnover")) == pytest.approx(0.01, abs=TOL)
    am = run(fields, "ew_amihud_illiquidity", half_life=63.0)
    origin = _lib.sk_descriptor.EWAmihudIlliquidity(half_life=63.0).fit_transform(
        _lib.sk_panel({k: fields[k] for k in ("returns", "adj_close", "adj_volume")}))
    assert M(am.value["values"]) == pytest.approx(origin, abs=TOL, nan_ok=True)
    assert all(v > 0 for v in am.value["latest"].values())
    assert "SK-101" in piece_ids("fundamentals.ew_amihud_illiquidity")


def test_other_descriptors_SK_102(fields):
    r = fields["returns"]
    assert run(fields, "max_return", window=21).value["latest"] == pytest.approx(r.iloc[-21:].max().to_dict(), abs=TOL)
    assert latest(run(fields, "log_market_cap")) == pytest.approx(math.log(fields["market_cap"].iloc[-1]["AAA"]), abs=TOL)
    assert latest(run(fields, "short_interest_descriptor")) == pytest.approx(0.05, abs=TOL)
    assert latest(run(fields, "days_to_cover", half_life=21.0)) == pytest.approx(5.0, abs=1e-6)
    assert run(fields, "passthrough", field="sales_ttm").value["latest"] == pytest.approx(fields["sales_ttm"].iloc[-1].to_dict(), abs=TOL)
    assert run({"market_cap": fields["market_cap"].iloc[0:0]}, "log_market_cap").value is None
    assert run({}, "log_market_cap").value is None
    nan_cap = {"market_cap": fields["market_cap"] * np.nan}
    assert run(nan_cap, "log_market_cap").value is None
    with pytest.raises(BadInput):
        run([1, 2], "log_market_cap")
    with pytest.raises(BadInput):
        run({"market_cap": [1, 2, 3]}, "log_market_cap")
    assert "SK-102" in piece_ids("fundamentals.log_market_cap")


def test_every_descriptor_matches_the_library_SK_090(fields):
    """Every registered descriptor is the library class on the same panel (1e-8)."""
    ref = pd.Series(fields["returns"].mean(axis=1).to_numpy() * 0.5, index=fields["returns"].index)
    for spec in descriptors.SPECS:
        kw = {}
        if spec.public == "growth_rate":
            kw = {"field": "sales_ttm", "lag": 5}
        elif spec.public in ("change_in_intensity", "change_to_scale"):
            kw = {"field": "net_income_ttm", "scale_field": "total_assets", "lag": 5}
        elif spec.public == "passthrough":
            kw = {"field": "sales_ttm"}
        elif spec.public == "ew_macro_sensitivity":
            kw = {"reference_returns": ref}
        res = run(fields, spec.public, **kw)
        assert res.value is not None, (spec.public, res.warnings)
        params = {k: v for k, v in kw.items() if k not in ("reference_returns",)}
        needed = list(spec.fields) + [params[f] for f in spec.field_params]
        instance = getattr(_lib.sk_descriptor, spec.cls)(**{k: v for k, v in res.params.items()})
        fit = {"reference_returns": ref.to_numpy()} if "reference_returns" in kw else {}
        origin = instance.fit_transform(_lib.sk_panel({f: fields[f] for f in dict.fromkeys(needed)}), **fit)
        assert np.asarray(res.value["values"], dtype=float) == pytest.approx(np.where(np.isfinite(origin), origin, np.nan), abs=TOL, nan_ok=True), spec.public


def test_panel_inactive_policy_SK_103(returns_panel):
    r = returns_panel.copy()
    r.iloc[-100:, 4] = np.nan                                         # EEE stops trading 100 days before the end
    res = call("fundamentals.panel", fields={"returns": r})
    assert res.value["inactive_cells"] == 100 and res.value["last_active"]["EEE"] == str(r.index[-101])[:10]
    assert res.value["first_active"]["AAA"] == "2020-01-01" and res.value["latest"]["returns"]["EEE"] is None
    p = build_panel({"returns": r})
    assert isinstance(p, AssetPanel) and p.inactive_cells == 100
    values = p.fields["returns"].values
    assert values.iloc[-100:, 4].isna().all() and values.iloc[-101, 4] == r.iloc[-101, 4]        # nothing is filled forward after the last trade
    # an explicit universe mask: values of an inactive asset are cleared and counted, never kept
    mask = pd.DataFrame(True, index=r.index, columns=r.columns)
    mask.iloc[:50, 0] = False
    cleared = build_panel({"returns": returns_panel}, mask)
    assert cleared.cleared_cells == 50 and cleared.fields["returns"].values.iloc[:50, 0].isna().all()
    assert call("fundamentals.panel", fields={"returns": returns_panel}, active=mask).warnings == ("cleared 50 value(s) of inactive assets",)
    # a gap inside the life of an asset is a missing value of an active asset, and stays missing
    gap = returns_panel.copy()
    gap.iloc[10, 2] = np.nan
    assert infer_active(gap).iloc[10, 2] and np.isnan(build_panel({"returns": gap}).fields["returns"].values.iloc[10, 2])
    # a descriptor sees the policy: a delisted asset has no market cap after its last day
    caps = pd.DataFrame({"A": [10.0, 11.0, np.nan, np.nan], "B": [5.0, 5.0, 5.0, 5.0]}, index=pd.bdate_range("2024-01-01", periods=4))
    out = M(run({"market_cap": caps}, "log_market_cap").value["values"])
    assert np.isnan(out[2:, 0]).all() and out[3, 1] == pytest.approx(math.log(5.0), abs=TOL)
    # categorical and three dimensional fields
    codes = pd.DataFrame(0, index=r.index, columns=r.columns)
    full = build_panel({"returns": r}, categorical={"sector": (codes, ("tech", "bank"))}, three_d={"exposure": {"size": r * 0 + 1, "value": r * 0 + 2}})
    sector, exposure = full.fields["sector"], full.fields["exposure"]
    assert isinstance(sector, FieldCategorical) and sector.decode().iloc[0, 0] == "tech" and sector.codes.iloc[-1, 4] == -1
    assert isinstance(exposure, Field3D) and exposure.labels == ("size", "value") and exposure.values["size"].iloc[-1, 4] != exposure.values["size"].iloc[-1, 4]
    assert isinstance(full.fields["returns"], Field2D)
    view = full.view("2020-02-03", "2020-03-02")
    assert view.index[0] == pd.Timestamp("2020-02-03") and view.index[-1] == pd.Timestamp("2020-03-02") and len(view.active) == len(view.fields["returns"].values)
    with pytest.raises(BadInput):
        build_panel({"a": r, "b": r.iloc[:-1]})
    with pytest.raises(BadInput):
        build_panel({"a": r}, categorical={"sector": (codes + 5, ("tech", "bank"))})
    with pytest.raises(BadInput):
        call("fundamentals.panel", fields={"a": "not a frame"})
    assert call("fundamentals.panel", fields={}).value is None
    assert call("fundamentals.panel", fields={"returns": r * np.nan}).value is None
    assert "SK-103" in piece_ids("fundamentals.panel")


def test_scalers_SK_104():
    dates = pd.bdate_range("2024-01-01", periods=2)
    x = pd.DataFrame([[1.0, 2, 3, 4, 5], [10.0, 20, 30, 40, 500]], index=dates, columns=list("ABCDE"))
    std = M(call("fundamentals.cs_scale", values=x, method="standard", min_group_size=3).value["values"])
    # z score by date with the sample standard deviation: (x - 3) / sqrt(2.5)
    assert std[0] == pytest.approx([(v - 3) / math.sqrt(2.5) for v in (1, 2, 3, 4, 5)], abs=TOL)
    row = np.array([10.0, 20, 30, 40, 500])
    assert std[1] == pytest.approx(((row - row.mean()) / row.std(ddof=1)).tolist(), abs=TOL)
    win = M(call("fundamentals.cs_scale", values=x, method="winsorize", low=0.2, high=0.8).value["values"])
    assert win[0] == pytest.approx(np.clip([1, 2, 3, 4, 5], *np.quantile([1, 2, 3, 4, 5], [0.2, 0.8])).tolist(), abs=TOL)       # 1.8 .. 4.2
    assert win[1] == pytest.approx(np.clip(row, *np.quantile(row, [0.2, 0.8])).tolist(), abs=TOL)
    rank = M(call("fundamentals.cs_scale", values=x, method="gaussian_rank", min_group_size=3).value["values"])
    z = np.array([NormalDist().inv_cdf((k - 0.5) / 5) for k in range(1, 6)])
    assert rank[0] == pytest.approx((z / z.std(ddof=1)).tolist(), abs=1e-6)
    assert rank[1] == pytest.approx(rank[0].tolist(), abs=TOL)                    # ranks do not care about the outlier
    pct = M(call("fundamentals.cs_scale", values=x, method="percentile_rank", min_group_size=3).value["values"])
    assert pct[0] == pytest.approx([0.1, 0.3, 0.5, 0.7, 0.9], abs=TOL)
    tanh = M(call("fundamentals.cs_scale", values=x, method="tanh", knee=1.0).value["values"])
    origin = _lib.sk_preprocessing.CSTanhShrinker(knee=1.0).fit_transform(x.to_numpy())
    assert tanh == pytest.approx(origin, abs=TOL) and tanh[0][2] == pytest.approx(3.0, abs=TOL)       # the middle of the cross section does not move
    # no look at future dates: changing the last date does not change the earlier one
    changed = x.copy()
    changed.iloc[1] = [1.0, 1, 1, 1, 1000]
    for method, kw in (("standard", {"min_group_size": 3}), ("winsorize", {}), ("gaussian_rank", {"min_group_size": 3}), ("percentile_rank", {"min_group_size": 3}), ("tanh", {})):
        a = M(call("fundamentals.cs_scale", values=x, method=method, **kw).value["values"])
        b = M(call("fundamentals.cs_scale", values=changed, method=method, **kw).value["values"])
        assert a[0] == pytest.approx(b[0].tolist(), abs=TOL), method
    # an inactive asset (NaN) is left out of the cross section of its date and comes back as NaN
    gap = x.copy()
    gap.iloc[0, 1] = np.nan
    out = M(call("fundamentals.cs_scale", values=gap, method="standard", min_group_size=3).value["values"])
    assert np.isnan(out[0][1]) and out[0][0] == pytest.approx(((1 - np.mean([1, 3, 4, 5])) / np.std([1, 3, 4, 5], ddof=1)), abs=TOL)
    assert call("fundamentals.cs_scale", values=x.iloc[0:0], method="standard").value is None
    assert call("fundamentals.cs_scale", values=x * np.nan, method="percentile_rank").value is None
    for bad in (dict(method="zscore"), dict(method="winsorize", low=0.9, high=0.1), dict(method="standard", min_group_size=0), dict(method="tanh", knee=0)):
        with pytest.raises(BadInput):
            call("fundamentals.cs_scale", values=x, **bad)
    with pytest.raises(BadInput):
        call("fundamentals.cs_scale", values=[1, 2, 3], method="standard")
    assert "SK-104" in piece_ids("fundamentals.cs_scale")
