"""The guide: Markdown topics read from the working folder, with the copy shipped in the app as fallback.

Layout: `<root>/docs/learn/<language>/<NN>-<slug>.md`. `NN-` only sets the order. The working folder wins over the
app copy, and a topic missing in the chosen language falls back to English. Files are read with `safefs`, so a
link planted in the working folder is never followed. Everything returned is text: the screen renders it as
Markdown without raw HTML."""
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from hokma.core.config import hokma_home
from hokma.i18n import LANGUAGES
from hokma.kit import safefs

MAX_TOPIC_BYTES = 200 * 1024
FILE = re.compile(r"^(?:(\d{1,3})-)?([a-z0-9][a-z0-9-]{0,60})\.md$")
SNIPPET_RADIUS = 70
MAX_QUERY = 100
FALLBACK_LANGUAGE = "en"
LEARN_DIR = "docs/learn"


@dataclass(frozen=True)
class Topic:
    slug: str
    title: str
    order: int
    body: str


def app_template_root() -> Path:
    """Folder of the kit template that ships with the app (it has docs/learn inside)."""
    return Path(__file__).resolve().parents[3] / "kit" / "template"


def _roots() -> list[Path]:
    """Folders that hold docs/learn, highest priority first."""
    return [hokma_home(), app_template_root()]


def _title(body: str, slug: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()[:120] or slug
    return slug


def _read_folder(root: Path, language: str) -> dict[str, Topic]:
    found: dict[str, Topic] = {}
    try:
        names = sorted(os.listdir(root / LEARN_DIR / language))
    except OSError:
        return found
    for name in names:
        match = FILE.match(name)
        if not match:
            continue
        try:
            raw = safefs.read_regular(root, f"{LEARN_DIR}/{language}/{name}")
        except (safefs.UnsafePath, OSError):
            continue
        if raw is None or len(raw) > MAX_TOPIC_BYTES:
            continue
        try:
            body = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        slug = match.group(2)
        found[slug] = Topic(slug, _title(body, slug), int(match.group(1) or 999), body)
    return found


def topics(language: str) -> list[Topic]:
    chosen = language if language in LANGUAGES else FALLBACK_LANGUAGE
    merged: dict[str, Topic] = {}
    for lang in dict.fromkeys([FALLBACK_LANGUAGE, chosen]):          # English first, the chosen language overrides
        for root in reversed(_roots()):                               # app copy first, working folder overrides
            merged.update(_read_folder(root, lang))
    return sorted(merged.values(), key=lambda topic: (topic.order, topic.slug))


def get(language: str, slug: str) -> Topic | None:
    return next((topic for topic in topics(language) if topic.slug == slug), None)


def _fold(text: str) -> str:
    """Lowercase without accents, so an accented word matches its plain spelling."""
    return "".join(c for c in unicodedata.normalize("NFD", text.lower()) if not unicodedata.combining(c))


def search(language: str, query: str) -> list[dict]:
    """Topics that contain every word of the query, best first, each with a short snippet around the first match."""
    words = [w for w in _fold(query.strip()[:MAX_QUERY]).split() if w]
    if not words or sum(len(w) for w in words) < 2:
        return []
    results = []
    for topic in topics(language):
        title, body = _fold(topic.title), _fold(topic.body)
        if not all(w in title or w in body for w in words):
            continue
        score = sum(3 * title.count(w) + body.count(w) for w in words)
        plain = re.sub(r"[#*`>|_]+", " ", topic.body)
        folded = _fold(plain)
        first = min((i for i in (folded.find(w) for w in words) if i >= 0), default=0)
        start = max(0, first - SNIPPET_RADIUS)
        snippet = " ".join(plain[start:first + SNIPPET_RADIUS * 2].split())
        results.append({"slug": topic.slug, "title": topic.title, "snippet": snippet, "score": score})
    return sorted(results, key=lambda r: (-r["score"], r["title"]))
