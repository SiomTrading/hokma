"""P15: the desks guide (topic 11), the Money desks section of the template CLAUDE.md and the agent table of HOKMA.md."""
import re
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[2] / "kit" / "template"
EM_DASH = chr(0x2014)
DESK_SLUGS = ["desk-chief", "signals", "risk", "bull-case", "bear-case", "judge"]
# Spanish words built from code points so this file stays free of handwritten Spanish.
ES_SETTINGS = "Configuraci" + chr(0xF3) + "n"
ES_REAL_MONEY_BLOCKED = "dinero real est" + chr(0xE1) + " bloqueado"
NON_ENGLISH = re.compile("[" + "".join(chr(c) for c in (0xE1, 0xE9, 0xED, 0xF3, 0xFA, 0xF1, 0xBF, 0xA1)) + "]")


def _read(*parts):
    return TEMPLATE.joinpath(*parts).read_text(encoding="utf-8")


def _levels(text):
    return [len(m.group(1)) for m in re.finditer(r"^(#{1,3}) ", text, re.M)]


def test_both_languages_have_the_same_structure():
    en, es = _read("docs", "learn", "en", "11-desks.md"), _read("docs", "learn", "es", "11-desks.md")
    assert _levels(en) == _levels(es)
    assert len(re.findall(r"^\|.*\|$", en, re.M)) == len(re.findall(r"^\|.*\|$", es, re.M))
    assert len(re.findall(r"^- ", en, re.M)) == len(re.findall(r"^- ", es, re.M))
    assert len(re.findall(r"^\d+\. ", en, re.M)) == len(re.findall(r"^\d+\. ", es, re.M))
    assert en.startswith("# Money desks\n") and es.startswith("# Mesas de dinero\n")


@pytest.mark.parametrize("path", [("docs", "learn", "en", "11-desks.md"), ("docs", "learn", "es", "11-desks.md"),
                                  ("CLAUDE.md",), ("HOKMA.md",), ("docs", "learn", "README.md")])
def test_no_long_dash(path):
    assert EM_DASH not in _read(*path)


@pytest.mark.parametrize("language", ["en", "es"])
def test_guide_covers_the_whole_class(language):
    text = _read("docs", "learn", language, "11-desks.md")
    english = language == "en"
    budgets = ("150,000", "300,000", "600,000") if english else ("150.000", "300.000", "600.000")
    tabs = ("Terminal", "Agents", "Operations", "Rules", "Settings") if english else \
        ("Terminal", "Agentes", "Operaciones", "Reglas", ES_SETTINGS)
    for word in budgets + tabs + ("80%", "100%"):
        assert word in text
    assert ("Real money is blocked" if english else ES_REAL_MONEY_BLOCKED) in text


def test_english_guide_is_english():
    assert not NON_ENGLISH.search(_read("docs", "learn", "en", "11-desks.md"))


def test_claude_section_does_not_promise_creation_and_names_only_real_slugs():
    section = _read("CLAUDE.md").split("## Money desks", 1)[1]
    assert "do not create anything" in section and "From a template" in section
    for slug in ("desk-chief", "hugo", "risk", "iris", "signals", "axel", "vera", "bull-case", "bear-case", "judge"):
        assert f"`{slug}`" in section
    assert "desk_create" not in section and "desk_configure" not in section


def test_hokma_table_lists_the_six_desk_agents_once_each():
    text = _read("HOKMA.md")
    for slug in DESK_SLUGS:
        assert text.count(f"(`{slug}`)") == 1
        assert (TEMPLATE / "agents" / f"{slug}.md").is_file()
