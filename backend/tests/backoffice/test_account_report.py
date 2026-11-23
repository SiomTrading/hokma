"""Account report (SPEC-018: AFS-043). Returns are money weighted on a realized basis; values by hand."""
from decimal import Decimal

from exec_support import db, desks_db, env, ready  # noqa: F401  (fixtures)

from hokma.backoffice import book as book_module
from hokma.backoffice.reports import account_report as ar
from hokma.backoffice.reports.account_report import Benchmark

D = Decimal
NQ = "FUT:NQ:2026-12"


def fill_march(r):
    def add(kind, amount, day, ref, **kw):
        book_module.append(r.account_id, db=r.db, kind=kind, amount=D(amount), source="import", source_ref=ref, occurred_at=f"{day} 15:00:00", desk_id=r.desk_id, **kw)

    add("deposit", "5000", "2026-03-02", "A1")
    add("fill", "-840000", "2026-03-03", "A2", contract_id=NQ, qty=D(2), price=D(21000), multiplier=D(20))
    add("commission", "-5", "2026-03-03", "A3")
    add("fill", "422000", "2026-03-10", "A4", contract_id=NQ, qty=D(-1), price=D(21100), multiplier=D(20))
    add("commission", "-2.5", "2026-03-10", "A5")
    add("dividend", "12.5", "2026-03-12", "A6")
    add("withdrawal", "-2000", "2026-03-20", "A7")


BENCH = Benchmark("SPY", period=D("0.01"), year=D("0.02"), inception=D("0.02"), allocation={"future": D("0.5"), "cash": D("0.5")}, fixed_on="2026-01-02")


def report(ready, **kw):
    kw.setdefault("benchmark", BENCH)
    return ar.build_report(ready.account_id, start="2026-03-01", end="2026-03-31", db=ready.db, **kw)


def test_the_period_return_is_money_weighted_and_net_of_commissions(ready):
    fill_march(ready)
    r = report(ready)
    p = r.returns["period"]
    # net result: realized 2000 + dividend 12.5 - commissions 7.5 = 2005 ; flows 5000 - 2000 = 3000 ; opening 1,000,000 ; closing 1,005,005
    assert (p.opening, p.closing, p.flows, p.net_result, p.costs) == (D(1000000), D(1005005), D(3000), D(2005), D("7.5"))
    # weights over 31 days: the deposit of 03-02 stays 30 days, the withdrawal of 03-20 stays 12 days
    assert abs(p.net_return - D(2005 * 31) / D(31126000)) < D("1e-20") and abs(p.gross_return - D("2012.5") * 31 / D(31126000)) < D("1e-20")
    assert p.gross_return > p.net_return


def test_the_year_and_the_inception_start_with_the_account(ready):
    fill_march(ready)
    r = report(ready)
    assert r.returns["year"].start == "2026-03-02" == r.returns["since_inception"].start          # the account begins on its first entry
    # 30 days: the deposit weighs 1, the withdrawal 12 / 30: denominator 1,000,000 + 5000 - 2000 * 12 / 30 = 1,004,200
    assert abs(r.returns["since_inception"].net_return - D(2005) / D(1004200)) < D("1e-20")


def test_the_benchmark_is_the_one_fixed_beforehand_and_missing_figures_are_not_zero(ready):
    fill_march(ready)
    r = report(ready)
    assert r.benchmark == "SPY" and r.benchmark_returns == {"period": D("0.01"), "year": D("0.02"), "since_inception": D("0.02")} and "benchmark" not in r.missing
    bare = report(ready, benchmark=None)
    assert bare.benchmark is None and "benchmark" in bare.missing and any("No benchmark" in n for n in bare.notices)
    named = report(ready, benchmark="SPY")
    assert named.benchmark == "SPY" and named.benchmark_returns["period"] is None and "benchmark_period" in named.missing


def test_the_declared_benchmark_of_the_desk_is_used_when_none_is_given(ready):
    fill_march(ready)
    with ready.db.write() as u:
        u.session.execute("UPDATE desk_profile SET benchmark = 'SPY' WHERE desk_id = :d", {"d": ready.desk_id})
    r = ar.build_report(ready.account_id, start="2026-03-01", end="2026-03-31", desk_id=ready.desk_id, db=ready.db)
    assert r.benchmark == "SPY"


def test_positions_carry_their_return_and_an_unpriced_one_is_not_valued_as_zero(ready):
    fill_march(ready)
    priced = report(ready, marks={NQ: D(21050)})
    pos = priced.positions[0]
    assert pos["contract_id"] == NQ and pos["qty"] == "1" and pos["avg_price"] == "21000" and pos["unrealized"] == "1000" and pos["value"] == "421000"
    assert abs(D(pos["return_pct"]) - (D(21050) / D(21000) - 1) * 100) < D("1e-20")
    bare = report(ready)
    assert bare.positions[0]["return_pct"] is None and bare.positions[0]["valued_at"] == "cost" and f"unpriced:{NQ}" in bare.missing


def test_allocation_activity_and_notices(ready):
    fill_march(ready)
    r = report(ready, marks={NQ: D(21050)})
    # cash 1,000,000 - 414,995 = 585,005 ; future 421,000 ; total 1,006,005
    by = {a["class"]: a for a in r.allocation}
    assert by["cash"]["value"] == "585005" and by["future"]["value"] == "421000"
    assert abs(D(by["future"]["weight"]) - D(421000) / D(1006005)) < D("1e-20") and by["future"]["benchmark_weight"] == "0.5"
    assert r.activity == {"trades": 2, "bought": "840000", "sold": "422000", "deposits": "5000", "withdrawals": "-2000", "dividends": "12.5", "interest": "0",
                          "commissions": "-7.5"}
    assert set(r.notices) >= {ar.NOTICES["past_performance"], ar.NOTICES["realized_basis"], ar.NOTICES["net_of_costs"]}


def test_the_html_escapes_every_text_and_holds_no_brand(ready):
    fill_march(ready)
    r = report(ready, benchmark=Benchmark("<script>alert(1)</script>", period=D("0.01")))
    html = ar.render_html(r)
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<h1>Account report 2026-03-01 to 2026-03-31</h1>" in html
    assert "<table>" in html and "Past results do not guarantee" in html


def test_the_asset_class_comes_only_from_the_exact_canonical_id():
    assert ar.asset_class(NQ) == "future" and ar.asset_class("EQ:SPY") == "equity"
    assert ar.asset_class("fut:nq:2026-12") == "unknown" and ar.asset_class("EQ:SPY ") == "unknown"
