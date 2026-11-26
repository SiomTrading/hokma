"""The value and fundamental strategies run in backtest with the fixtures of H3 (SPEC-020: AH-30, AH-31): the real daily bars of SPY and the three
companies of the fundamentals fixture. The facts are point in time and the dollar neutral book is checked at every rebalance."""
import json
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

import pandas as pd
import pytest
from backtest import support
from backtest.support import requires_engine
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma import backtest
from hokma.backtest import grid
from hokma.backtest.data_feed import data_hash
from hokma.backtest.runner import InstrumentData, Services
from hokma.backtest.ticks import Bar
from hokma.contracts.identity import ContractSheet
from hokma.desks.core.signal import SignalContext
from hokma.factory import book, fundamental_facts as ff
from hokma.factory.alphas.fundamental_rank import FundamentalRank
from hokma.factory.alphas.value_screen import ValueScreen
from hokma.factory.parts import build_composite

FIXTURES = Path(__file__).resolve().parents[1] / "quant" / "fixtures"
PIT = json.loads((FIXTURES / "statements_pit.json").read_text(encoding="utf-8"))
SPY_FRAME = pd.read_csv(FIXTURES / "ohlcv_daily_spy.csv", parse_dates=["date"]).set_index("date")
T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
SHEET = ContractSheet("EQ:SPY", "equity", None, "SPY", D(1), D("0.01"), D("0.01"), None, None, None, "USD", "h3 fixture", T0)    # type: ignore[arg-type]
INSTRUMENT = "EQ:SPY"


def statements(company: str) -> pd.DataFrame:
    frame = pd.DataFrame(PIT["companies"][company]["rows"]).set_index("period_end")
    numeric = [c for c in frame.columns if c != "filed"]
    frame[numeric] = frame[numeric].astype(float)
    return frame


def bars() -> list[Bar]:
    return [Bar(ts.tz_localize("UTC").to_pydatetime(), D(repr(float(r.open))), D(repr(float(r.high))), D(repr(float(r.low))), D(repr(float(r.close))), D(repr(float(r.volume))))
            for ts, r in SPY_FRAME.iterrows()]


def source(instrument, spec):
    rows = bars()
    return InstrumentData(INSTRUMENT, tuple(rows), data_hash(SPY_FRAME), SHEET)


def spec(**over):
    rows = bars()
    return support.spec(strategy_id="value_alfa", params={}, line="LP", instruments=(INSTRUMENT,), bar="1d", start=rows[0].ts, end=rows[-1].ts,
                        as_of=rows[-1].ts, capital_usd=D(100_000), **over)


@requires_engine
def test_the_value_screen_runs_in_a_backtest_of_lp_with_the_yields_of_h3(db, tmp_path):
    """ALFA meets all three criteria at every filing but one (2024-08: its free cash flow yield falls under 0.03) and keeps a long view: one long trade
    that enters on the open after the first rebalance that follows the first filing (2021-02-20): the first session of March 2021."""
    rows = bars()
    dates = [b.ts.date() for b in rows]
    monthly = grid.rebalance_grid(dates, "monthly")
    feeds = ff.step_feeds(dates, monthly, lambda day: ff.value_facts(statements("ALFA"), day))
    assert set(feeds) == set(ff.VALUE_FACTS) and pd.isna(feeds["earnings_yield"][:40]).all()
    alpha = ValueScreen(asset_class="equity", min_earnings_yield=0.03, min_fcf_yield=0.03, min_book_to_price=0.15)
    strategy = build_composite(alpha, sheet=SHEET, desk_id=1, line="LP", feeds=feeds, tag="value_alfa")
    result = backtest.run(spec(), services=Services(db=db, source=source, curve_dir=tmp_path, strategy=lambda params: strategy))
    assert result.status == "done" and result.trades, result.warnings
    first = result.trades[0]
    entry_day = next(d for d in dates if d >= pd.Timestamp("2021-03-01").date())
    assert first.side == "long" and first.qty == 1 and first.entry_at.date() > entry_day and first.entry_tag == "value_alfa"
    assert first.entry_at.date() == dates[dates.index(entry_day) + 1]          # the order of the rebalance close fills at the next open


@requires_engine
def test_the_facts_of_a_name_with_no_filing_yet_leave_the_alpha_without_a_view(db, tmp_path):
    rows = bars()
    dates = [b.ts.date() for b in rows]
    feeds = ff.step_feeds(dates, grid.rebalance_grid(dates, "monthly"), lambda day: {k: None for k in ff.VALUE_FACTS})     # no statement was ever filed
    alpha = ValueScreen(asset_class="equity")
    strategy = build_composite(alpha, sheet=SHEET, desk_id=1, line="LP", feeds=feeds)
    result = backtest.run(spec(), services=Services(db=db, source=source, curve_dir=tmp_path, strategy=lambda params: strategy))
    assert result.status == "done" and result.trades == () and "no_trades" in result.warnings


def universe():
    closes = SPY_FRAME["close"]
    return {"ALFA": ff.Name(statements("ALFA"), closes.iloc[600:900]), "BRAVO": ff.Name(statements("BRAVO"), closes.iloc[650:950]),
            "CHARLIE": ff.Name(statements("CHARLIE"), closes.iloc[700:1000])}


def views_at(day) -> dict[str, float]:
    """The view of fundamental_ls for every name that has its three ranks on `day`: a name that cannot be ranked abstains and is left out."""
    alpha = FundamentalRank(book="dollar_neutral")
    out = {}
    for name, ranks in ff.rank_facts(universe(), day, periods=3, skip=0).items():
        facts = {k: [float("nan") if v is None else v] for k, v in ranks.items()}
        signal = alpha.signal(SignalContext(source="alpha.fundamental_rank", desk_id=1, line="LP", symbol=name, as_of=datetime(2024, 6, 3, tzinfo=timezone.utc),
                                            horizon="months", snapshot_hash="t", facts=facts))
        if not signal.abstained and signal.value is not None:
            out[name] = float(signal.value)
    return out


def test_the_long_short_book_is_neutral_in_dollars_at_every_rebalance_of_the_grid():
    """CHARLIE has no F-score, so it has no quality rank and abstains. ALFA ranks high on value and quality (a long view) and BRAVO low on both (a short
    view): at each monthly rebalance of 2024 the book holds both and nets zero to the cent."""
    days = [d for d in grid.rebalance_grid(SPY_FRAME.index, "monthly") if d.year == 2024 and d >= pd.Timestamp("2024-03-01").date()]
    assert len(days) >= 6
    books = {d: book.dollar_neutral_book(views_at(d), D("100000")) for d in days}
    assert all(set(b) == {"ALFA", "BRAVO"} and b["ALFA"] > 0 > b["BRAVO"] for b in books.values())
    assert book.neutral_gaps(books) == ()
    assert all(book.net_usd(b) == 0 and b["ALFA"] == D("50000.00") for b in books.values())


def test_a_rebalance_whose_views_are_all_on_one_side_is_reported_not_filled():
    assert book.neutral_gaps({pd.Timestamp("2024-04-01").date(): book.dollar_neutral_book({"ALFA": 0.4}, D("100000"))}) == (pd.Timestamp("2024-04-01").date(),)
