"""SPEC-016 T22 (3.10, guide 7.10 and 7.14): the decoder of each futures service has its own field order, and a test fixes it."""
from hokma.market.adapters.schwab import chart_equity_bar, chart_futures_bar, future_quote_of

MINUTE = 1_700_000_040_000


def test_chart_futures_has_its_own_order_and_the_equity_decoder_would_shift_every_price():
    fields = {"1": MINUTE, "2": 28300.25, "3": 28305.0, "4": 28299.5, "5": 28302.75, "6": 118}
    bar = chart_futures_bar("/NQZ26", fields)
    assert (bar.symbol, bar.start_ms, bar.o, bar.h, bar.l, bar.c, bar.v) == ("/NQZ26", MINUTE, 28300.25, 28305.0, 28299.5, 28302.75, 118.0)
    wrong = chart_equity_bar("/NQZ26", fields)                 # equity reads 1 as open and 7 as time: it must NOT be used for futures
    assert wrong is None or (wrong.o, wrong.c) != (bar.o, bar.c)


def test_a_futures_message_without_its_time_or_prices_is_no_bar():
    assert chart_futures_bar("/NQZ26", {"2": 1, "3": 2, "4": 0.5, "5": 1.5}) is None
    assert chart_futures_bar("/NQZ26", {"1": MINUTE, "2": 1, "3": 2}) is None


def test_the_futures_quote_takes_the_fields_of_the_futures_table():
    fields = {"1": 28300.0, "2": 28300.5, "3": 28300.25, "4": 7, "5": 9, "8": 1_500_000, "9": 3, "10": 1_700_000_000_100, "11": 1_700_000_000_200,
              "12": 28400.0, "13": 28100.0, "14": 28250.0, "18": 28200.0, "22": "Normal", "24": 28300.25}
    quote = future_quote_of("/NQZ26", fields, False, 0)
    assert (quote.bid, quote.ask, quote.last, quote.mark) == (28300.0, 28300.5, 28300.25, 28300.25)
    assert (quote.bid_size, quote.ask_size, quote.last_size, quote.volume) == (7, 9, 3, 1_500_000)
    assert (quote.open, quote.high, quote.low, quote.prev_close, quote.status) == (28200.0, 28400.0, 28100.0, 28250.0, "Normal")
    assert quote.ts_ms == 1_700_000_000_200 and quote.mark_calculated is False


def test_without_a_mark_the_futures_quote_gets_the_mean_marked_as_calculated():
    quote = future_quote_of("/NQZ26", {"1": 10.0, "2": 10.5}, True, 5)
    assert quote.mark == 10.25 and quote.mark_calculated is True and quote.delayed is True and quote.ts_ms == 5
