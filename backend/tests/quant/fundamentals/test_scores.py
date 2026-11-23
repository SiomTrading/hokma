"""FT-007 to FT-012: bankruptcy and manipulation scores, with hand values and library parity."""
import math

import numpy as np
import pandas as pd
import pytest

from hokma.quant.errors import BadInput
from hokma.quant.fundamentals import _lib

from .conftest import AS_OF, call, latest, piece_ids

TOL = 1e-8


def ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def test_altman_z_FT_007(alfa, bravo, charlie):
    res = call("fundamentals.altman_z", statements=alfa, as_of=AS_OF)
    wc, re, ebit, mve, tl, s, ta = 320, 790, 305, 5880, 740, 1300, 1900
    hand = 1.2 * wc / ta + 1.4 * re / ta + 3.3 * ebit / ta + 0.6 * mve / tl + 1.0 * s / ta
    assert latest(res, "altman_z") == pytest.approx(hand, abs=TOL)
    assert res.value["zone"] == "safe" and res.params["safe_above"] == 2.99 and res.params["distress_below"] == 1.81
    # library parity on the same ratios
    origin = _lib.ft_altman.get_altman_z_score(wc / ta, re / ta, ebit / ta, mve / tl, s / ta, model="original")
    assert latest(res, "altman_z") == pytest.approx(origin, abs=TOL)
    # the private model uses book equity (equity 1160 over liabilities 740) and its own cut-offs
    private = call("fundamentals.altman_z", statements=alfa, as_of=AS_OF, model="private")
    assert latest(private, "altman_z") == pytest.approx(0.717 * wc / ta + 0.847 * re / ta + 3.107 * ebit / ta + 0.42 * 1160 / 740 + 0.998 * s / ta, abs=TOL)
    assert private.value["zone"] == "grey"
    emerging = call("fundamentals.altman_z", statements=alfa, as_of=AS_OF, model="emerging")
    assert latest(emerging, "altman_z") == pytest.approx(3.25 + 6.56 * wc / ta + 3.26 * re / ta + 6.72 * ebit / ta + 1.05 * 1160 / 740, abs=TOL)
    assert call("fundamentals.altman_z", statements=bravo, as_of=AS_OF).value["zone"] == "distress"
    missing = call("fundamentals.altman_z", statements=charlie, as_of=AS_OF)
    assert missing.value is None and "data_missing:retained_earnings" in missing.warnings
    with pytest.raises(BadInput):
        call("fundamentals.altman_z", statements=alfa, model="made up")
    assert call("fundamentals.altman_z", statements=alfa.iloc[0:0]).value is None
    assert call("fundamentals.altman_z", statements=pd.DataFrame({"total_assets": [np.nan]}, index=["x"])).value is None
    assert "FT-007" in piece_ids("fundamentals.altman_z")


def test_beneish_m_FT_008(alfa, charlie):
    res = call("fundamentals.beneish_m", statements=alfa, as_of=AS_OF)
    idx = {k: v[-1] for k, v in res.value["indexes"].items()}
    # hand values of the indexes (2023 against 2022)
    assert idx["sgi"] == pytest.approx(1300 / 1200, abs=TOL)
    assert idx["tata"] == pytest.approx((237.6 - 290) / 1900, abs=TOL)
    assert idx["dsri"] == pytest.approx((150 / 1300) / (140 / 1200), abs=TOL)
    assert idx["lvgi"] == pytest.approx(((330 + 220) / 1900) / ((320 + 230) / 1750), abs=TOL)
    assert idx["aqi"] == pytest.approx((1 - (650 + 680) / 1900) / (1 - (600 + 650) / 1750), abs=TOL)
    assert idx["sgai"] == pytest.approx((180 / 1300) / (170 / 1200), abs=TOL)
    hand = (-4.84 + 0.92 * idx["dsri"] + 0.528 * idx["gmi"] + 0.404 * idx["aqi"] + 0.892 * idx["sgi"] + 0.115 * idx["depi"]
            - 0.172 * idx["sgai"] + 4.679 * idx["tata"] - 0.327 * idx["lvgi"])
    assert latest(res, "beneish_m") == pytest.approx(hand, abs=TOL)
    assert res.value["series"]["beneish_m"][0] is None            # the first period has no prior period
    assert res.value["likely_manipulator"] is False
    miss = call("fundamentals.beneish_m", statements=charlie, as_of=AS_OF)
    assert miss.value is None and any(w.startswith("data_missing:") for w in miss.warnings)
    assert call("fundamentals.beneish_m", statements=alfa.iloc[0:0]).value is None
    with pytest.raises(BadInput):
        call("fundamentals.beneish_m", statements=None)
    assert "FT-008" in piece_ids("fundamentals.beneish_m")


def test_piotroski_f_FT_009(alfa, bravo, charlie):
    res = call("fundamentals.piotroski_f", statements=alfa, as_of=AS_OF)
    # hand count of 2023 (7): ROA>0, cash flow>0, ROA up, cash flow above income, leverage down, current ratio up, gross margin up;
    # fails: shares were issued (5) and asset turnover fell (1300/1750 against 1200/1600)
    assert latest(res, "piotroski_f") == 7
    crit = {k: v[-1] for k, v in res.value["criteria"].items()}
    assert crit == {"roa_positive": 1, "cash_flow_positive": 1, "roa_improves": 1, "cash_flow_above_income": 1, "leverage_falls": 1,
                    "current_ratio_improves": 1, "no_new_shares": 0, "gross_margin_improves": 1, "asset_turnover_improves": 0}
    assert res.value["series"]["piotroski_f"][2] == 8              # 2022: asset turnover 1200/1600 above 1100/1500
    assert res.value["series"]["piotroski_f"][:2] == [None, None]     # two periods back are needed: nothing is scored on a missing input
    assert latest(call("fundamentals.piotroski_f", statements=bravo, as_of=AS_OF), "piotroski_f") == 1
    # the library counts a missing input as a failed test; here the period gets no score instead
    no_issuance = call("fundamentals.piotroski_f", statements=alfa.drop(columns=["issuance"]), as_of=AS_OF)
    assert no_issuance.value is None and "data_missing:issuance" in no_issuance.warnings
    gap = alfa.copy()
    gap.loc["2022-12-31", "current_assets"] = np.nan
    gapped = call("fundamentals.piotroski_f", statements=gap, as_of=AS_OF)
    assert gapped.value is None and "no period could be scored" in gapped.warnings
    early_gap = alfa.copy()
    early_gap.loc["2020-12-31", "current_assets"] = np.nan                  # the oldest period is not an input of 2022 or 2023
    assert call("fundamentals.piotroski_f", statements=early_gap, as_of=AS_OF).value["series"]["piotroski_f"] == [None, None, 8, 7]
    assert call("fundamentals.piotroski_f", statements=charlie, as_of=AS_OF).value is None
    assert call("fundamentals.piotroski_f", statements=alfa.iloc[0:0]).value is None
    assert "FT-009" in piece_ids("fundamentals.piotroski_f")


def test_ohlson_o_FT_010(alfa, bravo, charlie):
    res = call("fundamentals.ohlson_o", statements=alfa, as_of=AS_OF)
    o = (-1.32 - 0.407 * math.log(1900) + 6.03 * 740 / 1900 - 1.43 * 320 / 1900 + 0.0757 * 330 / 650 - 1.72 * 0 - 2.37 * 237.6 / 1900
         - 1.83 * (237.6 + 46) / 740 + 0.285 * 0 - 0.521 * (237.6 - 208.8) / (237.6 + 208.8))
    assert latest(res, "ohlson_o") == pytest.approx(o, abs=TOL)
    assert res.value["bankruptcy_probability"][-1] == pytest.approx(1 / (1 + math.exp(-o)), abs=TOL)
    assert res.value["series"]["ohlson_o"][0] is None
    b = call("fundamentals.ohlson_o", statements=bravo, as_of=AS_OF)
    assert latest(b, "ohlson_o") > 0 and b.value["bankruptcy_probability"][-1] > 0.5
    miss = call("fundamentals.ohlson_o", statements=charlie, as_of=AS_OF)
    assert miss.value is None and miss.warnings
    assert call("fundamentals.ohlson_o", statements=alfa.iloc[0:0]).value is None
    assert "FT-010" in piece_ids("fundamentals.ohlson_o")


def test_zmijewski_FT_011(alfa, bravo, charlie):
    res = call("fundamentals.zmijewski", statements=alfa, as_of=AS_OF)
    z = -4.336 - 4.513 * 237.6 / 1900 + 5.679 * 740 / 1900 - 0.004 * 650 / 330
    assert latest(res, "zmijewski") == pytest.approx(z, abs=TOL)
    assert res.value["bankruptcy_probability"][-1] == pytest.approx(ncdf(z), abs=TOL)
    assert _lib.ft_zmijewski.get_zmijewski_score(237.6 / 1900, 740 / 1900, 650 / 330) == pytest.approx(z, abs=TOL)
    assert call("fundamentals.zmijewski", statements=bravo, as_of=AS_OF).value["bankruptcy_probability"][-1] > 0.9
    assert call("fundamentals.zmijewski", statements=charlie, as_of=AS_OF).value is not None      # three inputs are enough
    assert call("fundamentals.zmijewski", statements=charlie.drop(columns=["net_income"]), as_of=AS_OF).value is None
    assert call("fundamentals.zmijewski", statements=alfa.iloc[0:0]).value is None
    assert "FT-011" in piece_ids("fundamentals.zmijewski")


def test_springate_grover_fulmer_FT_012(alfa, bravo, charlie):
    res = call("fundamentals.springate_grover_fulmer", statements=alfa, as_of=AS_OF)
    ta = 1900
    springate = 1.03 * 320 / ta + 3.07 * 305 / ta + 0.66 * 297 / 330 + 0.4 * 1300 / ta
    grover = 1.65 * 320 / ta + 3.404 * 305 / ta - 0.016 * 237.6 / ta + 0.057
    fulmer = (5.528 * 790 / ta + 0.212 * 1300 / ta + 0.073 * 297 / 1160 + 1.270 * 290 / 740 - 0.120 * 260 / ta + 2.335 * 330 / ta
              + 0.575 * math.log(ta - 100) + 1.083 * 320 / 740 + 0.894 * math.log(305 / 8) - 6.075)
    v = res.value["latest"]
    assert v["springate"] == pytest.approx(springate, abs=TOL)
    assert v["grover"] == pytest.approx(grover, abs=TOL)
    assert v["fulmer_h"] == pytest.approx(fulmer, abs=TOL)
    assert res.value["failing"] == {"springate": False, "grover": False, "fulmer_h": False}
    bad = call("fundamentals.springate_grover_fulmer", statements=bravo, as_of=AS_OF)
    assert bad.value["failing"]["springate"] is True and bad.value["failing"]["grover"] is True
    assert bad.value["series"]["fulmer_h"][-1] is None            # log of a negative EBIT over interest is not a number: no score, not a made up one
    part = call("fundamentals.springate_grover_fulmer", statements=charlie, as_of=AS_OF)
    assert set(part.value["latest"]) == {"grover"} and set(part.value["skipped"]) == {"springate", "fulmer_h"}
    assert call("fundamentals.springate_grover_fulmer", statements=alfa.iloc[0:0]).value is None
    assert call("fundamentals.springate_grover_fulmer", statements=pd.DataFrame({"revenue": [1.0]}, index=["x"])).value is None
    assert "FT-012" in piece_ids("fundamentals.springate_grover_fulmer")
