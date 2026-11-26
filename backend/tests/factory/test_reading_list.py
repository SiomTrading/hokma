"""AS-021 and AS-022: the reading list by line, without affiliate links, and the link check that runs only in CI."""
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

ROOT = Path(__file__).resolve().parents[3]
LIST = ROOT / "docs" / "research" / "reading-list.md"
WORKFLOW = ROOT / ".github" / "workflows" / "links.yml"
SHORT_HOSTS = {"amzn.to", "a.co", "bit.ly"}
AFFILIATE_PARAMS = {"tag", "linkcode", "creativeasin", "creative", "linkid", "ref", "affid", "aff", "utm_source", "utm_medium", "utm_campaign"}


def affiliate(url: str) -> bool:
    """True for a shortened shop link, a shop link at all, or any parameter that tracks or credits an affiliate."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    return host in SHORT_HOSTS or "amazon." in host or bool({k.lower() for k in parse_qs(parsed.query)} & AFFILIATE_PARAMS)


def text() -> str:
    return LIST.read_text(encoding="utf-8")


def section(name_pattern: str) -> str:
    """The body of the first heading (any level) whose title matches, up to the next heading of the same or higher level."""
    lines = text().splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(#+) (.+)$", line)
        if m and re.match(name_pattern, m.group(2)):
            level = len(m.group(1))
            body = []
            for nxt in lines[i + 1:]:
                h = re.match(r"^(#+) ", nxt)
                if h and len(h.group(1)) <= level:
                    break
                body.append(nxt)
            return "\n".join(body)
    raise AssertionError(f"no heading matches {name_pattern!r}")


def items(body: str) -> list[str]:
    return [line for line in body.splitlines() if line.startswith("- ")]


def urls(body: str) -> list[str]:
    return re.findall(r"\((https?://[^)\s]+)\)", body)


def test_every_line_has_its_section():
    for line in ("DT", "SWC", "SWO", "LP", "RF"):
        section(rf"{line}: ")


def test_there_are_55_books_each_listed_once_and_without_a_link():
    book_lines = []
    for name in (r"DT: ", r"SWC: ", r"SWO: ", r"LP: ", r"Coding", r"Machine learning", r"Crypto", r"Background"):
        book_lines += items(section(name))
    assert len(book_lines) == 55
    assert len({re.sub(r" \[also .+\]$", "", b) for b in book_lines}) == 55
    assert not any("http" in b or "](" in b for b in book_lines)
    assert items(section(r"RF: ")) == [], "the fixed income section lists no book of its own"


def test_the_other_sections_have_their_counts():
    assert len(items(section(r"Videos"))) == 22
    assert len(items(section(r"Blogs"))) == 12
    assert len(items(section(r"Courses"))) == 10


def test_no_link_is_an_affiliate_or_tracking_link():
    found = urls(text())
    assert len(found) == 22 + 12 + 10
    assert [u for u in found if affiliate(u)] == []


def test_the_affiliate_detector_fires_on_the_forms_of_the_source_list():
    for bad in ("https://www.amazon.fr/gp/product/0060555661/ref=as_li_qf_asin_il_tl?ie=UTF8&tag=abc-21&linkCode=as2",
                "https://amzn.to/3QN2VdU", "https://example.org/page?utm_source=x"):
        assert affiliate(bad), bad
    assert not affiliate("https://www.youtube.com/watch?v=abc&t=10s&list=PL1")


def test_links_are_https_or_known_plain_http_blogs():
    plain = [u for u in urls(text()) if u.startswith("http://")]
    assert len(plain) <= 3, plain


def test_the_notices_register_the_origin_of_the_list():
    assert "- READ-001: " in (ROOT / ".notices" / "THIRD_PARTY.txt").read_text(encoding="utf-8")
    assert "READ-001" in text()


def test_the_link_check_runs_only_in_ci_on_the_reading_list():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"pull_request", "schedule", "workflow_dispatch"}
    assert "docs/research/reading-list.md" in triggers["pull_request"]["paths"]
    steps = workflow["jobs"]["reading-list"]["steps"]
    assert any("reading-list.md" in str(step.get("run", "")) for step in steps)
    assert workflow["permissions"] == {"contents": "read"}


def test_this_module_never_uses_the_network():
    source = Path(__file__).read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import|from)\s+(urllib\.request|socket|http\.client|requests|httpx)", source, re.M)
