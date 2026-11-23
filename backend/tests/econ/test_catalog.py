"""The catalog of macro releases and the skill that uses it (SPEC-018 B7: ECT-063). Values are computed by hand."""
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from hokma.desks.core import skills
from hokma.econ_calendar import catalog, events as calendar

ROOT = Path(__file__).resolve().parents[3]
SKILLS = ROOT / "kit" / "template" / "desk" / "skills"
UTC = timezone.utc
FIXTURES = Path(__file__).parent / "fixtures"
STAMP = datetime(2026, 10, 15, 12, 30, tzinfo=UTC)


def test_every_release_has_a_time_in_new_york_a_class_and_the_report_parts():
    assert len({r.key for r in catalog.CATALOG}) == len(catalog.CATALOG) >= 10
    for r in catalog.CATALOG:
        assert r.classification in catalog.CLASSES and r.components and r.match and r.country == "US"
    assert catalog.FIELDS == ("actual", "consensus", "previous", "revised")
    nfp, cpi, fomc = catalog.get("nfp"), catalog.get("cpi"), catalog.get("fomc")
    assert (nfp.time_et.hour, nfp.time_et.minute, cpi.time_et.hour, cpi.time_et.minute) == (8, 30, 8, 30)
    assert fomc.time_et.hour == 14 and fomc.classification == "policy" and "revision of the two prior months" in nfp.components
    with pytest.raises(catalog.CatalogError):
        catalog.get("nope")


def test_the_jobs_report_is_the_first_friday_at_0830_new_york_with_daylight_saving():
    # 2026-11-01 is a Sunday: the first Friday is the 6th, in standard time (UTC-5): 08:30 is 13:30 UTC
    assert catalog.release_days("nfp", 2026, 11) == (date(2026, 11, 6),)
    assert catalog.release_at("nfp", date(2026, 11, 6)) == datetime(2026, 11, 6, 13, 30, tzinfo=UTC)
    # 2026-10-02 is the first Friday of October, in daylight time (UTC-4): 08:30 is 12:30 UTC
    assert catalog.release_days("nfp", 2026, 10) == (date(2026, 10, 2),)
    assert catalog.release_at("nfp", date(2026, 10, 2)) == datetime(2026, 10, 2, 12, 30, tzinfo=UTC)
    assert catalog.get("nfp").exact


def test_nominal_days_of_the_other_releases():
    assert catalog.release_days("jobless_claims", 2026, 11) == tuple(date(2026, 11, d) for d in (5, 12, 19, 26))   # every Thursday
    assert catalog.release_days("cpi", 2026, 11) == (date(2026, 11, 13),)               # the 13th is a Friday
    assert catalog.release_days("cpi", 2026, 6) == (date(2026, 6, 15),)                 # the 13th is a Saturday: the next weekday
    assert catalog.release_days("ism_manufacturing", 2026, 11) == (date(2026, 11, 2),)  # first business day (the 1st is a Sunday)
    assert catalog.release_days("ism_services", 2026, 11) == (date(2026, 11, 4),)       # third business day: 2, 3, 4
    assert catalog.release_days("pce", 2026, 11) == (date(2026, 11, 30),)               # last weekday of the month
    assert catalog.release_days("fomc", 2026, 11) == () and not catalog.get("fomc").exact    # published by the central bank, no rule
    assert not catalog.get("cpi").exact
    assert catalog.release_at("ism_manufacturing", date(2026, 11, 2)) == datetime(2026, 11, 2, 15, 0, tzinfo=UTC)   # 10:00 UTC-5
    with pytest.raises(catalog.CatalogError):
        catalog.release_days("nfp", 2026, 13)


def test_an_event_is_found_by_whole_words_and_country():
    assert catalog.find("Core CPI m/m").key == "cpi"
    assert catalog.find("Non-Farm Employment Change").key == "nfp"
    assert catalog.find("Unemployment Claims").key == "jobless_claims"
    assert catalog.find("ISM Manufacturing PMI").key == "ism_manufacturing"
    assert catalog.find("FOMC Statement").key == "fomc" and catalog.find("FOMC Meeting Minutes") is None   # minutes are not the decision
    assert catalog.find("Shipping Index") is None                  # "ppi" inside a word is not the producer price index
    assert catalog.find("CPI m/m", "GB") is None and catalog.find("", "US") is None


def test_the_standardized_surprise_by_hand():
    # (175 - 150) / 50 = 0.5 ; (3.1 - 3.3) / 0.1 = -2
    assert catalog.surprise(actual=175, consensus=150, history_std=50) == catalog.Surprise("ok", Decimal("0.5"))
    assert catalog.surprise(actual="3.1", consensus=3.3, history_std=Decimal("0.1")).value == Decimal("-2")
    assert catalog.surprise(actual=0.4, consensus=0.3, history_std=0.1).value == Decimal("1")        # 0.4 - 0.3 is exact in Decimal
    assert catalog.surprise(actual=1, consensus=4, history_std=9).value == Decimal("-0.333333")      # 6 places
    assert catalog.surprise(actual=100, consensus=100, history_std=7).value == Decimal("0")          # a real zero is a zero


@pytest.mark.parametrize("kw,reason", [({"actual": 1, "consensus": None, "history_std": 1}, "no_consensus"),
                                       ({"actual": None, "consensus": 1, "history_std": 1}, "no_actual"),
                                       ({"actual": 1, "consensus": 1, "history_std": None}, "no_history"),
                                       ({"actual": 1, "consensus": 1, "history_std": 0}, "no_history"),
                                       ({"actual": 1, "consensus": 1, "history_std": -2}, "no_history"),
                                       ({"actual": 1, "consensus": "n/a", "history_std": 1}, "no_consensus"),
                                       ({"actual": 1, "consensus": float("nan"), "history_std": 1}, "no_consensus")])
def test_a_missing_consensus_actual_or_history_is_no_data_never_a_number(kw, reason):
    result = catalog.surprise(**kw)
    assert (result.status, result.value, result.reason) == ("NO_DATA", None, reason)


def test_monthly_to_annual_by_hand():
    assert catalog.annualize_monthly("0.2") == Decimal("2.43")     # 1.002 ^ 12 = 1.0242657...
    assert catalog.annualize_monthly(0.3) == Decimal("3.66")       # 1.003 ^ 12 = 1.0366
    assert catalog.annualize_monthly(0) == Decimal("0.00")


def test_the_table_has_the_four_figures_and_no_data_where_the_provider_has_none():
    ff = calendar.parse_forex_factory((FIXTURES / "forexfactory_week.json").read_text(), STAMP)
    fh = calendar.parse_finnhub((FIXTURES / "finnhub_calendar.json").read_text(), STAMP)
    rows = {r.name: r for r in catalog.table(ff + fh, {"cpi": "0.1"})}
    cpi_ff, cpi_fh = rows["CPI m/m"], rows["CPI MoM"]
    assert (cpi_ff.release, cpi_ff.consensus, cpi_ff.previous, cpi_ff.actual, cpi_ff.revised) == ("cpi", 0.3, 0.2, None, None)
    assert cpi_ff.consensus_status == "ok" and cpi_ff.surprise.reason == "no_actual"            # not released yet
    assert cpi_fh.surprise == catalog.Surprise("ok", Decimal("1"))                               # (0.4 - 0.3) / 0.1
    minutes = rows["FOMC Minutes"]
    assert minutes.release is None and minutes.consensus_status == "NO_DATA" and minutes.surprise.status == "NO_DATA"
    assert rows["ECB Press Conference"].consensus_status == "NO_DATA" and rows["ECB Press Conference"].release is None
    assert rows["Retail Sales y/y"].release is None                                              # a Chinese release is not the US one
    claims = rows["Unemployment Claims"]
    assert claims.release == "jobless_claims" and claims.surprise.reason == "no_actual"
    cpi_no_std = {r.name: r for r in catalog.table(fh)}["CPI MoM"]
    assert cpi_no_std.surprise.reason == "no_history"                                            # no deviation given: no number
    assert [r.scheduled_at for r in catalog.table(ff + fh)] == sorted(r.scheduled_at for r in catalog.table(ff + fh))


# ---------------------------------------------------------------------------------------------- the skill

SKILL = SKILLS / "economic-releases" / "SKILL.md"


def test_the_skill_follows_the_skill_format_exactly():
    skill = skills.load_skill(SKILL)                                   # six fields, no others, folder name = name
    assert (skill.name, skill.version) == ("economic-releases", "1.0.0") and SKILL.parent.name == skill.name
    assert skill.patterns and skill.avoid and re.search(r"\buse\b", skill.description, re.I)
    assert "50 separates growth from contraction" in skill.body
    raw = SKILL.read_text(encoding="utf-8")
    meta, _ = skills.split_frontmatter(raw)
    assert list(meta) == list(skills.REQUIRED)
    assert any(s.name == "economic-releases" for s in skills.load_skills(SKILLS))
    manifest = SKILLS / skills.MANIFEST_NAME
    if manifest.exists():                                              # when the folder has a manifest, the skill is in it and current
        entry = skills.read_manifest(SKILLS)["skills"].get("economic-releases")
        assert entry == {"version": skill.version, "digest": skill.digest()}


CLAIMS = [r"\b\d+(\.\d+)?\s*(bps?|basis points|pips?)\b", r"S&P|\bnasdaq\b|\btreasur|\byields?\b|\bEUR/USD\b",
          r"\b(rally|rallies|sell[- ]?offs?|sells? off|surges?|plunges?|hawkish|dovish|bullish|bearish|goldilocks|stagflation)\b",
          r"\b(moves?|rises?|rose|falls?|fell|jumps?|drops?|gains?)\b[^.\n]{0,60}\d+(\.\d+)?\s*%",
          r"\b(typically|usually|historically|on average)\b[^.\n]{0,80}\b(market|price|index|stocks?)\b"]


def test_the_skill_states_no_market_effect_without_a_source():
    text = SKILL.read_text(encoding="utf-8")
    hits = [(p, m.group(0)) for p in CLAIMS for m in re.finditer(p, text, re.I | re.S)]
    assert hits == []
    skill = skills.load_skill(SKILL)
    assert "does not claim" in skill.body and "measured from price data" in skill.body       # the limit is written in the skill
    assert any("how far or in which direction a market moves" in a for a in skill.avoid)


def test_the_integrity_check_catches_what_it_is_meant_to_catch():
    for sample in ("A hot print sends 2Y yields up 10 bps.", "The S&P usually falls 1.5% on a miss.", "The dollar rallies on a beat.",
                   "Payrolls typically move the market on the day."):
        assert any(re.search(p, sample, re.I | re.S) for p in CLAIMS), sample
    assert not any(re.search(p, "Show actual, consensus, previous and revised side by side.", re.I) for p in CLAIMS)
