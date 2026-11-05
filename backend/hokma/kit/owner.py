"""Owner and fund names: stored in the protected data folder, filled into the kit text by kit sync.

`<data>/owner.json` lives outside the working folder, so the agent can read the names (they are written into
HOKMA.md) but cannot change what Hokma stores. An empty name means "not given": the kit then says "the owner"
and "the fund" (instructions are read by the agents, for whom "you" would be themselves).
"""
import json
import re

from hokma.core.config import owner_path

MAX_LENGTH = 60
DEFAULT_OWNER = "the owner"
DEFAULT_FUND = "the fund"
# Braces would let a name inject a marker; angle brackets would let it fake the comment markers of HOKMA.md;
# the pipe breaks Markdown tables; brackets, parentheses and "!" build links and images.
_FORBIDDEN = re.compile(r"[{}<>|\[\]()!\x00-\x1f\x7f]")
# What can open a Markdown block when the name starts a line: headings, bullets, rules, quotes, fences,
# numbered lists. They are removed from the start of the name until it begins with a plain character.
_BLOCK_START = re.compile(r"^(?:[#*+`>~=_\-]+|\d+[.)])\s*")


def clean(value: object) -> str:
    """One line, no markers or control characters, bounded length. Anything that is not text becomes empty."""
    if not isinstance(value, str):
        return ""
    text = " ".join(_FORBIDDEN.sub(" ", value).split())
    while True:
        stripped = _BLOCK_START.sub("", text)
        if stripped == text:
            break
        text = stripped
    return text[:MAX_LENGTH].strip()


def exists() -> bool:
    return owner_path().is_file()


def load() -> dict[str, str]:
    """The stored names; empty strings for what is missing or unreadable."""
    try:
        data = json.loads(owner_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {"owner_name": clean(data.get("owner_name")), "fund_name": clean(data.get("fund_name"))}


def save(owner_name: str, fund_name: str) -> None:
    path = owner_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"owner_name": clean(owner_name), "fund_name": clean(fund_name)}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def marker_values() -> dict[str, str]:
    """Marker -> text for the kit: the stored name, or the default when none was given."""
    names = load()
    return {"owner_name": names["owner_name"] or DEFAULT_OWNER, "fund_name": names["fund_name"] or DEFAULT_FUND}
