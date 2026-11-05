"""Messages the student sees (CLI, installer helpers, hooks). Code, comments, docs and logs are English;
text shown to people lives in the catalogs under `hokma/catalog/`, Spanish by default, English available.

Language: HOKMA_LANG, then the file `<data>/language`, then Spanish. Standard library only, so the isolated
scripts (guard hook, MCP launcher) can use it too.
"""
import json
import os
from functools import lru_cache
from pathlib import Path

DEFAULT_LANGUAGE = "es"
LANGUAGES = ("es", "en")
CATALOG_DIR = Path(__file__).resolve().parent / "catalog"


def _data_dir() -> Path | None:
    value = os.environ.get("HOKMA_DATA_DIR")
    return Path(value) if value else None


def language() -> str:
    chosen = os.environ.get("HOKMA_LANG", "").strip().lower()
    if chosen not in LANGUAGES:
        chosen = ""
        data = _data_dir()
        if data is not None:
            try:
                chosen = (data / "language").read_text(encoding="utf-8").strip().lower()
            except OSError:
                chosen = ""
    return chosen if chosen in LANGUAGES else DEFAULT_LANGUAGE


@lru_cache(maxsize=None)
def catalog(lang: str) -> dict[str, str]:
    with open(CATALOG_DIR / f"{lang}.json", encoding="utf-8") as f:
        return json.load(f)


def t(key: str, /, **params: object) -> str:
    """Text for `key` in the active language (English, then the key itself, as fallbacks)."""
    text = catalog(language()).get(key)
    if text is None:
        text = catalog("en").get(key, key)
    try:
        return text.format(**params)
    except (KeyError, IndexError, ValueError):
        return text
