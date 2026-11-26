"""Alphas of the strategy library, block A (SPEC-020: AH-30, AH-31, AH-32, AS-006). Expected values are computed by hand."""
from datetime import datetime, timezone

import numpy as np
import pytest

from hokma.desks.core.signal import AlphaModel, SignalContext
from hokma.factory.alphas.fundamental_rank import FundamentalRank
from hokma.factory.alphas.inflection import Inflection
from hokma.factory.alphas.premium_wheel import PremiumWheel
from hokma.factory.alphas.value_screen import CRITERIA_BY_ASSET_CLASS, ValueScreen
from hokma.factory.params import ParamError, validate

AS_OF = datetime(2025, 3, 3, 21, 0, tzinfo=timezone.utc)


def ctx(line: str = "LP", **facts) -> SignalContext:
    return SignalContext("alpha.test", 7, line, "SPY", AS_OF, "days", "h" * 8, facts={k: np.asarray(v, dtype=float) for k, v in facts.items()})


def test_all_four_are_alpha_models_with_a_schema_and_defaults_that_build():
    for klass in (ValueScreen, FundamentalRank, Inflection, PremiumWheel):
        alpha = klass(**validate(klass.SCHEMA, {}))
        assert isinstance(alpha, AlphaModel) and alpha.lookback >= 1 and set(alpha.FACTS)


def test_value_screen_equity_counts_the_criteria_met():
    alpha = ValueScreen()                                           # minimums 0.08, 0.06 and 1.0
    signal = alpha.signal(ctx(earnings_yield=[0.10], fcf_yield=[0.05], book_to_price=[1.2]))
    assert signal.value == pytest.approx(2 / 3)                     # earnings yield and book to price pass, free cash flow yield fails
    assert [c.passed for c in signal.criteria] == [True, False, True]
    assert alpha.signal(ctx(earnings_yield=[0.08], fcf_yield=[0.06], book_to_price=[1.0])).value == 1.0     # at the limit passes
    assert alpha.signal(ctx(earnings_yield=[0.01], fcf_yield=[0.0], book_to_price=[0.2])).value == 0.0      # flat, never short
    missing = alpha.signal(ctx(earnings_yield=[0.10], book_to_price=[1.2]))
    assert missing.abstained and missing.data_missing == ("fcf_yield",)


def test_value_screen_declares_which_criteria_exist_for_each_asset_class():
    assert CRITERIA_BY_ASSET_CLASS == {"equity": ("earnings_yield", "fcf_yield", "book_to_price"),
                                       "etf": ("earnings_yield", "book_to_price"), "future": ("price_to_average",)}
    etf = ValueScreen(asset_class="etf", min_earnings_yield=0.07)
    assert etf.FACTS == ("earnings_yield", "book_to_price")
    assert etf.signal(ctx(earnings_yield=[0.07], book_to_price=[0.9])).value == 0.5        # 0.07 passes, 0.9 is below 1.0
    future = ValueScreen(asset_class="future", max_price_to_average=0.8)
    assert future.FACTS == ("price_to_average",)
    assert future.signal(ctx("SWC", price_to_average=[0.7])).value == 1.0
    assert future.signal(ctx("SWC", price_to_average=[0.9])).value == 0.0
    with pytest.raises(ParamError, match="asset_class"):
        validate(ValueScreen.SCHEMA, {"asset_class": "crypto"})


def test_fundamental_rank_weighted_composite():
    alpha = FundamentalRank()                                       # weights 0.4, 0.4, 0.2
    signal = alpha.signal(ctx(value_rank=[0.9], quality_rank=[0.8], momentum_rank=[0.5]))
    assert signal.value == pytest.approx(0.56)                      # composite 0.36 + 0.32 + 0.10 = 0.78; 2 * (0.78 - 0.5)
    assert alpha.signal(ctx(value_rank=[0.3], quality_rank=[0.3], momentum_rank=[0.3])).value == pytest.approx(-0.4)
    assert alpha.signal(ctx(value_rank=[0.5], quality_rank=[0.5], momentum_rank=[0.5])).value == 0.0
    flat = FundamentalRank(book="long_only").signal(ctx(value_rank=[0.3], quality_rank=[0.3], momentum_rank=[0.3]))
    assert flat.value == 0.0
    only_price = FundamentalRank(weight_value=0.5, weight_quality=0.0, weight_momentum=0.5)
    assert only_price.signal(ctx(value_rank=[1.0], quality_rank=[0.0], momentum_rank=[0.5])).value == pytest.approx(0.5)   # (0.5 + 0.25) / 1 = 0.75
    assert alpha.signal(ctx(value_rank=[1.2], quality_rank=[0.8], momentum_rank=[0.5])).abstained
    assert alpha.signal(ctx(value_rank=[0.9], quality_rank=[0.8])).data_missing == ("momentum_rank",)
    with pytest.raises(ValueError):
        FundamentalRank(weight_value=0.0, weight_quality=0.0, weight_momentum=0.0)


def test_inflection_is_the_change_over_the_window():
    growth = [0.02, 0.03, 0.04, 0.05, 0.07]
    alpha = Inflection(window=4, cap=0.05)
    assert alpha.lookback == 5
    assert alpha.signal(ctx(growth=growth)).value == pytest.approx(1.0)                      # 0.07 - 0.02 = 0.05 over a cap of 0.05
    assert Inflection(window=4, cap=0.10).signal(ctx(growth=growth)).value == pytest.approx(0.5)
    assert Inflection(window=2, cap=0.05).signal(ctx(growth=growth)).value == pytest.approx(0.6)    # 0.07 - 0.04 = 0.03
    assert Inflection(window=2, cap=0.05).signal(ctx(growth=[0.07, 0.05, 0.04])).value == pytest.approx(-0.6)
    assert alpha.signal(ctx(growth=growth[1:])).abstained                                    # four values: one short of the window
    price = Inflection(window=2, cap=0.10, source="price_roc")
    assert price.FACTS == ("price_roc",) and price.signal(ctx("SWC", price_roc=[0.01, 0.02, 0.05])).value == pytest.approx(0.4)
    assert Inflection(source="macro_change").FACTS == ("macro_change",)
    with pytest.raises(ParamError, match="source"):
        validate(Inflection.SCHEMA, {"source": "close"})


def test_premium_wheel_view_and_phase():
    alpha = PremiumWheel(threshold=1.2, cap=0.6, lot=100)
    puts = alpha.signal(ctx("SWO", iv_rv_ratio=[1.5], shares_held=[0.0]))
    assert puts.value == pytest.approx(0.5)                                                  # (1.5 - 1.2) / 0.6
    assert "cash secured puts" in puts.reason
    assert "covered calls" in alpha.signal(ctx("SWO", iv_rv_ratio=[1.5], shares_held=[100.0])).reason
    assert alpha.signal(ctx("SWO", iv_rv_ratio=[3.0], shares_held=[0.0])).value == 1.0
    assert alpha.signal(ctx("SWO", iv_rv_ratio=[1.0], shares_held=[0.0])).value == 0.0       # never negative
    assert alpha.signal(ctx("SWO", iv_rv_ratio=[1.5])).data_missing == ("iv_rv_ratio", "shares_held")


def test_premium_wheel_cannot_drop_its_switch_requirement():
    assert PremiumWheel.REQUIRES_PREMIUM_SWITCH is True and PremiumWheel().requires_premium_switch is True
    with pytest.raises(ValueError, match="premium switch"):
        PremiumWheel(requires_premium_switch=False)
