"""Statements of every broker into one book schema, the mapping, duplicates, USD only and the student template (SPEC-018: FT-104, FT-107, FT-108)."""
from decimal import Decimal as D
from pathlib import Path

import pytest
from backoffice_support import AAPL, FIXTURES, MIXED, OPT, db, desks_db, env, load_mixed, ready  # noqa: F401

from hokma.backoffice import book, importers
from hokma.backoffice.book import Book

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = ROOT / "kit" / "template" / "backoffice" / "transactions-template.csv"

SCHWAB = '''"Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"
"03/02/2026","Buy","AAPL","APPLE INC","100","$200.00","$1.00","-$20,001.00"
"03/04/2026 as of 03/03/2026","Sell","AAPL","APPLE INC","40","$210.00","$1.00","$8,399.00"
"03/10/2026","Buy to Open","SPY 12/18/2026 600.00 C","SPY CALL","2","$5.00","$1.30","-$1,001.30"
"03/11/2026","Qualified Dividend","AAPL","APPLE INC","","","","$23.00"
'''
ALPACA = '''transaction_time,activity_type,symbol,side,qty,price,commission,id,net_amount
2026-03-02T09:35:00Z,FILL,AAPL,buy,100,200.00,1.00,A1,
2026-03-04T11:00:00Z,FILL,AAPL,sell,40,210.00,1.00,A2,
2026-03-10T10:30:00Z,FILL,SPY261218C00600000,buy,2,5.00,1.30,A3,
2026-03-11T16:00:00Z,DIV,AAPL,,,,,A4,23.00
'''
IBKR = '''CurrencyPrimary,TradeID,DateTime,Buy/Sell,Symbol,Quantity,TradePrice,IBCommission,Multiplier
USD,T1,20260302;093500,BUY,AAPL,100,200.00,-1.00,1
USD,T2,20260304;110000,SELL,AAPL,-40,210.00,-1.00,1
USD,T3,20260310;103000,BUY,SPY   261218C00600000,2,5.00,-1.30,100
'''
SIM = '''filled_at,contract_id,side,qty,price,commission,multiplier,order_ref
2026-03-02 09:35:00,EQ:AAPL,buy,100,200.00,1.00,1,S1:0
2026-03-04 11:00:00,EQ:AAPL,sell,40,210.00,1.00,1,S2:0
2026-03-10 10:30:00,OPT:SPY:2026-12-18:C:600,buy,2,5.00,1.30,100,S3:0
'''
STUDENT = '''date,type,symbol,quantity,price,commission,amount,currency,reference,note
2026-03-02,buy,AAPL,100,200.00,1.00,,USD,R1,
2026-03-04,sell,AAPL,40,210.00,1.00,,USD,R2,
2026-03-10,buy,OPT:SPY:2026-12-18:C:600,2,5.00,1.30,,USD,R3,
2026-03-11,dividend,AAPL,,,,23.00,USD,R4,
'''


def shape(lines):
    """What a book line is, without its reference or the day it is dated."""
    return [(x.kind, x.contract_id, x.qty, x.price, x.multiplier, x.amount) for x in lines]


# What the first three trades are, by hand: buy 100 @ 200 is -20000 with 1.00 of commission; sell 40 @ 210 is +8400 with 1.00;
# buy 2 options @ 5 with multiplier 100 is -1000 with 1.30.
EXPECTED_TRADES = [
    ("fill", AAPL, D(100), D(200), D(1), D(-20000)), ("commission", AAPL, None, None, None, D(-1)),
    ("fill", AAPL, D(-40), D(210), D(1), D(8400)), ("commission", AAPL, None, None, None, D(-1)),
    ("fill", OPT, D(2), D(5), D(100), D(-1000)), ("commission", OPT, None, None, None, D("-1.30"))]


@pytest.mark.parametrize("name,content,has_dividend", [("schwab", SCHWAB, True), ("alpaca", ALPACA, True), ("ibkr", IBKR, False), ("sim", SIM, False),
                                                       ("student", STUDENT, True)])
def test_every_broker_normalizes_to_the_same_book_schema_FT_104(name, content, has_dividend):
    lines = importers.parse(name, content)
    trades = [x for x in lines if x.kind != "dividend"]
    assert shape(trades) == EXPECTED_TRADES
    if has_dividend:
        dividend = [x for x in lines if x.kind == "dividend"]
        assert shape(dividend) == [("dividend", AAPL, None, None, None, D(23))]
    assert all(x.ref and x.occurred_at[:4] == "2026" for x in lines)


def test_the_dates_of_every_format_are_read_as_utc_moments_FT_104():
    schwab = importers.parse("schwab", SCHWAB)
    assert schwab[2].occurred_at == "2026-03-04 00:00:00"          # "as of" is the date the broker booked it, the first one
    assert importers.parse("alpaca", ALPACA)[0].occurred_at == "2026-03-02 09:35:00"
    assert importers.parse("ibkr", IBKR)[0].occurred_at == "2026-03-02 09:35:00"


def test_a_missing_required_column_gives_a_readable_error_with_its_name_FT_104():
    headless = STUDENT.replace("price,", "cost,", 1)
    with pytest.raises(importers.StatementError) as raised:
        importers.parse("student", headless)
    assert "missing required column 'price'" in str(raised.value) and raised.value.errors
    with pytest.raises(importers.StatementError) as raised:
        importers.parse("ibkr", IBKR.replace("TradePrice", "Px"))
    assert "missing required column 'price'" in str(raised.value) and "tradeprice" in str(raised.value)
    with pytest.raises(importers.StatementError) as raised:
        importers.parse("schwab", SCHWAB.replace('"Action",', ""))
    assert "'type'" in str(raised.value)
    with pytest.raises(importers.StatementError, match="unknown broker"):
        importers.parse("nowhere", STUDENT)
    with pytest.raises(importers.StatementError, match="empty"):
        importers.parse("student", "")


def test_a_bad_row_names_its_row_and_column_and_nothing_is_written_FT_104(ready):
    bad = STUDENT + "2026-03-12,buy,AAPL,ten,200.00,1.00,,USD,R5,\n2026-03-12,wire,AAPL,1,200.00,0,,USD,R6,\n"
    with pytest.raises(importers.StatementError) as raised:
        importers.import_into(ready.account_id, "student", bad, db=ready.db)
    text = str(raised.value)
    assert "row 6: column 'quantity'" in text and "row 7: column 'type'" in text
    assert book.entries(ready.account_id, db=ready.db, since_reset=False) == []          # all or nothing


def test_the_multiplier_comes_from_the_contract_sheet_not_from_the_statement_FT_104():
    lying = SIM.replace("EQ:AAPL,buy,100,200.00,1.00,1,", "EQ:AAPL,buy,100,200.00,1.00,50,")
    with pytest.raises(importers.StatementError) as raised:
        importers.parse("sim", lying)
    assert "column 'multiplier' is 50 but the contract sheet of EQ:AAPL says 1" in str(raised.value)
    unknown = STUDENT.replace("AAPL,100", "NOPE123,100")
    with pytest.raises(importers.StatementError, match="NOPE123"):
        importers.parse("student", unknown)


def test_the_mapping_is_read_from_the_yaml_with_conversion_off_FT_107():
    mapping = importers.load_mapping()
    assert mapping["currency"] == {"convert": False, "accepted": ["USD"]}
    assert set(mapping["brokers"]) == {"student", "schwab", "alpaca", "ibkr", "sim"}
    assert importers.MAPPING_PATH.name == "mapping.yaml" and importers.MAPPING_PATH.is_file()
    assert "tradeprice" in [a.lower() for a in mapping["brokers"]["ibkr"]["columns"]["price"]]


def test_a_mapping_that_turns_the_conversion_on_is_refused_FT_107(tmp_path, monkeypatch):
    changed = tmp_path / "mapping.yaml"
    changed.write_text(importers.MAPPING_PATH.read_text(encoding="utf-8").replace("convert: false", "convert: true"), encoding="utf-8")
    monkeypatch.setattr(importers, "MAPPING_PATH", changed)
    importers.load_mapping.cache_clear()
    try:
        with pytest.raises(importers.StatementError, match="conversion must stay off"):
            importers.load_mapping()
    finally:
        monkeypatch.undo()
        importers.load_mapping.cache_clear()
    assert importers.load_mapping()["currency"]["convert"] is False


def test_a_row_in_another_currency_is_rejected_with_an_error_FT_107(ready):
    eur = IBKR.replace("USD,T2", "EUR,T2")
    with pytest.raises(importers.StatementError) as raised:
        importers.import_into(ready.account_id, "ibkr", eur, db=ready.db)
    assert "row 3: column 'currency' is 'EUR': conversion is off, the book holds USD only" in str(raised.value)
    assert book.entries(ready.account_id, db=ready.db, since_reset=False) == []
    lower = STUDENT.replace("USD,R2", "eur,R2")
    with pytest.raises(importers.StatementError, match="EUR"):
        importers.parse("student", lower)


def test_a_statement_imported_twice_is_not_written_twice_FT_107(ready):
    first = importers.import_into(ready.account_id, "student", STUDENT, db=ready.db)
    assert first.inserted == 7 and first.duplicates == []          # 3 fills, 3 commissions, 1 dividend
    cash = book.cash(ready.account_id, db=ready.db)
    second = importers.import_into(ready.account_id, "student", STUDENT, db=ready.db)
    assert second.inserted == 0 and len(second.duplicates) == 7
    assert book.cash(ready.account_id, db=ready.db) == cash and len(book.entries(ready.account_id, db=ready.db, since_reset=False)) == 7


def test_a_repeated_row_inside_one_file_is_read_once_and_reported_FT_107(ready):
    twice = STUDENT + "2026-03-02,buy,AAPL,100,200.00,1.00,,USD,R1,\n"
    report = importers.import_into(ready.account_id, "student", twice, db=ready.db)
    assert report.inserted == 7 and len(report.duplicates) == 2 and "repeats an earlier row" in report.duplicates[0]
    clash = STUDENT + "2026-03-02,buy,AAPL,999,200.00,1.00,,USD,R1,\n"
    with pytest.raises(importers.StatementError, match="reference 'import:R1' is used twice with different content"):
        importers.parse("student", clash)


def test_rows_without_a_reference_are_recognised_by_their_content_FT_107(ready):
    no_ref = "date,type,symbol,quantity,price,commission,amount\n2026-03-02,buy,AAPL,100,200.00,1.00,\n"
    assert importers.import_into(ready.account_id, "student", no_ref, db=ready.db).inserted == 2
    again = importers.import_into(ready.account_id, "student", no_ref, db=ready.db)
    assert again.inserted == 0 and len(again.duplicates) == 2


def test_the_mixed_fixture_is_a_student_csv_and_loads_in_full_FT_104(ready):
    report = load_mixed(ready)
    assert report.inserted == 27 and book.cash(ready.account_id, db=ready.db) == D("1396455.05")
    assert Book.load(ready.account_id, db=ready.db).realized() == D(3175)
    assert MIXED.parent == FIXTURES and len(MIXED.read_text(encoding="utf-8").splitlines()) == 15


def test_the_student_template_imports_without_error_into_an_empty_book_FT_108(ready):
    assert TEMPLATE.is_file() and TEMPLATE.read_text(encoding="utf-8").splitlines()[0] == "date,type,symbol,quantity,price,commission,amount,currency,reference,note"
    empty = Book.load(ready.account_id, db=ready.db)
    assert empty.entries == [] and empty.summary() == {"positions": {}, "realized": "0", "commissions": "0", "net_realized": "0"}
    report = importers.import_into(ready.account_id, "student", TEMPLATE.read_text(encoding="utf-8"), db=ready.db)
    assert report.inserted == 6 and report.duplicates == []           # deposit, buy and its commission, sell and its commission, dividend
    after = Book.load(ready.account_id, db=ready.db)
    pos = after.cost_positions()[AAPL]
    assert (pos.qty, pos.avg_price, pos.realized, pos.commissions) == (D(6), D(200), D(40), D(2))      # buy 10 @ 200, sell 4 @ 210: (210 - 200) * 4
    assert book.cash(ready.account_id, db=ready.db) == D(1_000_000) + D("8840.5")     # +10000 - 2001 + 839 + 2.5
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "Example" in text and all(ord(c) < 128 for c in text)       # English, ascii
