"""Reading a source into passages that can be cited (SPEC-030, A1-4 and A1-5).

A source is anything the owner hands over to build a strategy from: a text, a question, a PDF or a document of the Library, a link, a video or a reel.
Each one is read into passages, each with a locator that says where it came from ("paragraph 3", "page 4", "Entry rules", "03:12-03:40"), and saved in
`strategy_sources` and `strategy_source_passages`. The passages stay in the local database of the owner; the strategy that comes out of them carries the
rule, the reference and the locator, never the text of a third party.

Rules kept here and not by the caller:
- A file is never named by a path: a PDF or a document enters by its `document_id` in the Library.
- A link goes through the rules of `hokma.docs.web` (public addresses only, 5 redirects, 20 seconds, 5 MB of HTML).
- Every passage goes through the injection detector before it is saved. Its findings are kept and the text is not dropped; only chat control tokens
  are neutralized.
- A video needs the `watch` plugin on the Claude Code engine. Hokma does not install a video tool: the agent that talks with the owner watches the
  video and hands over its transcript with times and its notes about the frames.
- The same content under the same kind is the same source (`duplicate`).
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from hokma.db import Database, get_database
from hokma.db.errors import NotFound
from hokma.docs import chunker, ingest as library, readers, web
from hokma.projects.errors import ProjectError
from hokma.sources import injection, transcript

MAX_TEXT_CHARS = 200_000
MAX_QUESTION_CHARS = 4_000
MAX_TRANSCRIPT_CHARS = 400_000
MAX_NOTES_CHARS = 40_000
MAX_PASSAGES = 2_000
MAX_LOCATOR = 120
MAX_REFERENCE = 300
MAX_URL = 2_000
PAGE_LIMIT = 60
KINDS = ("text", "question", "pdf", "document", "link", "video", "reel")
RIGHTS = ("own", "third_party", "unknown")
RESTRICTION = {"own": 0, "third_party": 1, "unknown": 2}
DEFAULT_RIGHTS = {"text": "own", "question": "own", "pdf": "unknown", "document": "unknown", "link": "third_party", "video": "third_party",
                  "reel": "third_party"}
CAPTIONS = ("native", "whisper", "none")
WATCH_NAMES = ("watch", "claude-video")
WATCH_REPOS = frozenset({"github:bradautomates/claude-video"})      # where the watch plugin is published; a plugin of that name from anywhere else does not count
INSTALL_HINT = "Install it from Settings, Extensions, Add by link, with bradautomates/claude-video, and turn it on."
LIBRARY_HINT = "Open the link in your browser and upload the file to the Library."
CODES = ("bad_input", "no_data", "unavailable", "blocked")


class SourceError(Exception):
    """A classified failure: `code` is one of the tool error codes (bad_input, no_data, unavailable, blocked)."""

    def __init__(self, code: str, message: str, suggestions: list[str] | None = None) -> None:
        if code not in CODES:
            raise ValueError(f"unknown source error code {code!r}")
        super().__init__(message)
        self.code, self.message, self.suggestions = code, message, list(suggestions or [])


@dataclass(frozen=True)
class PassageRow:
    passage_id: int
    ord: int
    locator: str
    body: str


@dataclass(frozen=True)
class SourceRow:
    id: int
    kind: str
    reference: str
    url: str | None
    document_id: int | None
    rights: str
    content_sha256: str
    passage_count: int
    truncated: bool
    injection: list[dict[str, str]]
    meta: dict[str, Any]
    created_by: str
    duplicate: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def neutralized(self) -> bool:
        return bool(self.meta.get("neutralized"))

    @property
    def notice(self) -> str:
        return injection.NOTICE_OWN if self.rights == "own" else injection.NOTICE_THIRD_PARTY


@dataclass(frozen=True)
class PassagePage:
    source: SourceRow
    passages: list[PassageRow] = field(default_factory=list)
    next_offset: int | None = None
    total: int = 0


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _db(db: Database | None) -> Database:
    return db or get_database()


def _check_kind(kind: str, allowed: tuple[str, ...]) -> None:
    if kind not in allowed:
        raise SourceError("bad_input", f"kind is one of {', '.join(allowed)} here, got {kind!r}")


def _rights(kind: str, rights: str | None) -> str:
    chosen = DEFAULT_RIGHTS[kind] if rights is None else rights
    if chosen not in RIGHTS:
        raise SourceError("bad_input", f"rights is one of {', '.join(RIGHTS)}")
    return chosen


def _reference(reference: str | None, fallback: str) -> str:
    chosen = " ".join((reference if reference is not None else fallback).split())
    if reference is not None and not chosen:
        raise SourceError("bad_input", "reference is text of 1 to 300 characters")
    if len(chosen) > MAX_REFERENCE:
        raise SourceError("bad_input", f"reference is at most {MAX_REFERENCE} characters")
    return chosen or "untitled source"


def _locator(text: str) -> str:
    one = " ".join(text.split()) or "document"
    return one if len(one) <= MAX_LOCATOR else one[:MAX_LOCATOR - 3] + "..."


def _row(r: Mapping[str, Any], *, duplicate: bool = False, warnings: tuple[str, ...] = ()) -> SourceRow:
    return SourceRow(int(r["id"]), r["kind"], r["reference"], r["url"], r["document_id"], r["rights"], r["content_sha256"], int(r["passage_count"]),
                     bool(r["truncated"]), json.loads(r["injection_json"]), json.loads(r["meta_json"]), r["created_by"], duplicate, warnings)


def _strictest(a: str, b: str) -> str:
    """The more restrictive of two rights: own is the only one that lets the text out, unknown is stricter than third_party."""
    return max(a, b, key=lambda r: RESTRICTION[r])


def _save(kind: str, *, reference: str, url: str | None, document_id: int | None, rights: str, sha: str, parts: list[tuple[str, str]],
          meta: Mapping[str, Any], caller: str, truncated: bool, db: Database | None) -> SourceRow:
    """Scan, neutralize and store the passages of a source in one transaction. The same kind and content is the source that already exists."""
    if not parts:
        raise SourceError("no_data", "the source has no text to read")
    if len(parts) > MAX_PASSAGES:
        parts, truncated = parts[:MAX_PASSAGES], True
    reference_before = reference
    cleaned: list[tuple[str, str, str]] = []
    findings: list[dict[str, str]] = []
    changed = False
    reference, found = injection.scan_and_neutralize(reference, field="reference")      # Ian reads the reference and the locator, too
    changed, findings = reference != reference_before, found
    for place, body in parts:
        clean, found = injection.scan_and_neutralize(body, field=place)
        place_clean, place_found = injection.scan_and_neutralize(_locator(place), field=f"locator of {_locator(place)}")
        changed = changed or clean is not body or place_clean != _locator(place)
        findings += found + place_found
        cleaned.append((place_clean, clean, _sha(clean)))
    info = {**meta, "neutralized": changed}
    with _db(db).write() as u:
        existing = u.session.one("SELECT * FROM strategy_sources WHERE kind = :k AND content_sha256 = :h", {"k": kind, "h": sha})
        if existing is not None:
            if existing["rights"] == rights:
                return _row(existing, duplicate=True)
            # The same content came again under other rights: the source stays one, and the stricter rights win so the text never gets out on the looser claim.
            strict = _strictest(existing["rights"], rights)
            seen = sorted({*json.loads(existing["meta_json"]).get("rights_seen", [existing["rights"]]), rights}, key=RESTRICTION.get)
            u.session.execute("UPDATE strategy_sources SET rights = :r, meta_json = :m WHERE id = :i",
                              {"r": strict, "m": json.dumps({**json.loads(existing["meta_json"]), "rights_seen": seen}, ensure_ascii=True, sort_keys=True), "i": existing["id"]})
            existing = u.session.one("SELECT * FROM strategy_sources WHERE id = :i", {"i": existing["id"]})
            note = (f"this content was already saved under other rights ({', '.join(seen)}); it is now {strict}, the stricter claim, "
                    "and its text is given out only when every ingest of it was own")
            return _row(existing, duplicate=True, warnings=(note,))
        source_id = u.session.insert("strategy_sources", {
            "kind": kind, "reference": reference, "url": url, "document_id": document_id, "rights": rights, "content_sha256": sha,
            "passage_count": len(cleaned), "truncated": 1 if truncated else 0, "injection_json": json.dumps(injection.merge(findings), ensure_ascii=True),
            "meta_json": json.dumps(info, ensure_ascii=True, sort_keys=True), "created_by": caller})
        u.session.execute_many("INSERT INTO strategy_source_passages (source_id, ord, locator, body, body_sha256) VALUES (:s, :o, :l, :b, :h)",
                               [{"s": source_id, "o": n, "l": place, "b": body, "h": digest} for n, (place, body, digest) in enumerate(cleaned)])
        row = u.session.one("SELECT * FROM strategy_sources WHERE id = :i", {"i": source_id})
    return _row(row)


# ---------------------------------------------------------------------------------------------------- text and question

def ingest_text(text: str, *, kind: Literal["text", "question"] = "text", reference: str | None = None, rights: str | None = None, caller: str,
                db: Database | None = None) -> SourceRow:
    """The idea or the question of the owner. A text is cut in passages "paragraph N"; a question is one passage, "question"."""
    _check_kind(kind, ("text", "question"))
    limit = MAX_TEXT_CHARS if kind == "text" else MAX_QUESTION_CHARS
    if not isinstance(text, str) or not text.strip():
        raise SourceError("bad_input", f"{kind} is text of 1 to {limit} characters")
    if len(text) > limit:
        raise SourceError("bad_input", f"{kind} is at most {limit} characters, got {len(text)}")
    clean = chunker.normalize_text(text)
    if not clean:
        raise SourceError("bad_input", f"{kind} has no text in it")
    if kind == "question":
        parts = [("question", clean)]
    else:
        parts = [(f"paragraph {n}", body) for n, (_s, body) in enumerate(chunker.split_section("paragraph", clean), start=1)]
    return _save(kind, reference=_reference(reference, clean.split("\n", 1)[0][:80]), url=None, document_id=None, rights=_rights(kind, rights), sha=_sha(clean),
                 parts=parts, meta={}, caller=caller, truncated=False, db=db)


# ---------------------------------------------------------------------------------------------------- pdf and document

def _read_stored(doc: Any) -> readers.ReadResult:
    try:
        result = readers.read_document(Path(doc.stored_path))
    except readers.UnsupportedFormat as exc:
        raise SourceError("no_data", f"the document cannot be read: {exc}") from None
    except readers.UnreadableDocument as exc:
        raise SourceError("no_data", f"the document cannot be read: {exc}") from None
    if result.notice == readers.NO_TEXT_NOTICE or not result.passages:
        raise SourceError("no_data", readers.NO_TEXT_NOTICE, ["Upload a PDF with selectable text, or paste the text of the strategy."])
    return result


def _document(document_id: int, db: Database | None) -> Any:
    if isinstance(document_id, bool) or not isinstance(document_id, int) or document_id < 1:
        raise SourceError("bad_input", "document_id is a whole number from the Library")
    try:
        with _db(db).read() as u:
            doc = u.documents.get_document(document_id)
    except NotFound:
        raise SourceError("no_data", f"the Library has no document {document_id}", ["List the documents of the Library to find its id."]) from None
    if doc.status != "ready":
        raise SourceError("no_data", f"the document {document_id} is {doc.status}, not ready to read")
    return doc


def _passages_of(result: readers.ReadResult) -> list[tuple[str, str]]:
    return [(_locator(section), body) for section, body in result.passages]


def ingest_document(document_id: int, *, kind: Literal["pdf", "document"] = "pdf", rights: str | None = None, reference: str | None = None,
                    caller: str, db: Database | None = None) -> SourceRow:
    """A PDF or another document of the Library, read from the file that was stored. A PDF has its passages by page ("page 4")."""
    _check_kind(kind, ("pdf", "document"))
    doc = _document(document_id, db)
    if kind == "pdf" and doc.format != "pdf":
        raise SourceError("bad_input", f"the document {document_id} is {doc.format}, not a pdf: use kind document")
    result = _read_stored(doc)
    return _save(kind, reference=_reference(reference, doc.name), url=doc.source_url, document_id=doc.id, rights=_rights(kind, rights), sha=doc.sha256,
                 parts=_passages_of(result), meta={"format": doc.format, "document_name": doc.name}, caller=caller, truncated=result.truncated is not None, db=db)


# ---------------------------------------------------------------------------------------------------- link

def _link_error(exc: web.LinkError) -> SourceError:
    if exc.reason == "blocked":
        return SourceError("blocked", "the site did not let Hokma read the page", [LIBRARY_HINT])
    if exc.code in ("url_private", "link_scheme"):
        return SourceError("blocked", "only public http and https addresses can be read", [LIBRARY_HINT])
    if exc.code == "bad_url":
        return SourceError("bad_input", "the link is not a valid address")
    if exc.reason == "not_a_document":
        return SourceError("no_data", "the link does not lead to a page or a PDF that can be read")
    if exc.reason == "too_large":
        return SourceError("bad_input", "the document behind the link is larger than 50 MB")
    return SourceError("unavailable", "the link did not answer", ["Try again later, or open it in your browser and upload it to the Library."])


def _check_link(url: str) -> None:
    if not isinstance(url, str) or not url.strip() or len(url) > MAX_URL:
        raise SourceError("bad_input", f"url is text of 1 to {MAX_URL} characters")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise SourceError("bad_input", "the link is not a valid address") from None
    if parts.username is not None or parts.password is not None:
        raise SourceError("blocked", "a link with a user name or a password in it is not read", [LIBRARY_HINT])


def _library_pdf(url: str, resolver, connector, db: Database | None) -> tuple[int, str]:
    """The Library document of a PDF behind a link: the one already there with the same bytes, or the new one. (document id, final address)."""
    import hashlib as _h

    try:
        name, data, final = web.download(url, resolver=resolver, connector=connector)
    except web.LinkError as exc:
        raise _link_error(exc) from None
    digest = _h.sha256(data).hexdigest()
    with _db(db).write() as u:
        found = u.documents.find_by_sha256(u.library.ensure_student_library().id, digest)
    if found is not None:
        return found.id, final
    try:
        made = library.add_library_document(name, data, source_url=final, db=db)
    except ProjectError as exc:
        raise SourceError("no_data", f"the document behind the link cannot be added to the Library ({exc.code})") from None
    return int(made["id"]), final


def ingest_link(url: str, *, rights: str | None = None, reference: str | None = None, caller: str, resolver=None, connector=None,
                db: Database | None = None) -> SourceRow:
    """A page or a PDF behind a link. A page is read with the HTML reader of the Library; a PDF is saved in the Library and read as a PDF, and the
    source keeps its `document_id`."""
    _check_link(url)
    try:
        page = web.fetch_page(url, resolver=resolver, connector=connector)
    except web.LinkError as exc:
        raise _link_error(exc) from None
    if page.kind == "pdf":
        document_id, final = _library_pdf(page.url, resolver, connector, db)
        doc = _document(document_id, db)
        result = _read_stored(doc)
        return _save("link", reference=_reference(reference, doc.name), url=final, document_id=doc.id, rights=_rights("link", rights), sha=doc.sha256,
                     parts=_passages_of(result), meta={"content_type": "application/pdf", "final_url": final}, caller=caller,
                     truncated=result.truncated is not None, db=db)
    with tempfile.TemporaryDirectory(prefix="hokma-page-") as folder:
        path = Path(folder) / "page.html"
        path.write_bytes(page.html)
        try:
            result = readers.read_document(path)
        except readers.UnreadableDocument as exc:
            raise SourceError("no_data", f"the page cannot be read: {exc}") from None
    if not result.passages:
        raise SourceError("no_data", "the page has no text to read")
    parts = [(f"paragraph {n}" if section.startswith(readers.DEFAULT_SECTION) else _locator(section), body)
             for n, (section, body) in enumerate(result.passages, start=1)]
    host = urlsplit(page.url).hostname or "page"
    return _save("link", reference=_reference(reference, page.title or host), url=page.url, document_id=None, rights=_rights("link", rights),
                 sha=_sha("\n".join(body for _p, body in parts)), parts=parts, meta={"content_type": "text/html", "final_url": page.url}, caller=caller,
                 truncated=result.truncated is not None, db=db)


# ---------------------------------------------------------------------------------------------------- video and reel

def _is_watch(plugin_id: str, data: Mapping[str, Any]) -> bool:
    """A plugin named watch or claude-video that comes from the repository of the watch plugin or from the official marketplace of Anthropic."""
    from hokma.kit import extensions

    name, _, market = plugin_id.partition("@")
    if name not in WATCH_NAMES or not market:
        return False
    if market in extensions.BUILTIN_MARKETPLACES:
        return True
    entry = data["extraKnownMarketplaces"].get(market)
    source = entry.get("source") if isinstance(entry, dict) else None
    return isinstance(source, dict) and extensions.source_key(source) in WATCH_REPOS


def plugin_status() -> tuple[str, str]:
    """(state, reason) of the `watch` plugin and of the engine that would run it, read from what the engine left on disk. The state is `active`,
    `other_engine`, `disabled` or `missing`."""
    from hokma.kit import extensions, plugin_state
    from hokma.status.extensions_service import default_engine

    data, _problems = extensions.load()
    enabled: Mapping[str, bool] = data["enabledPlugins"]
    installs = plugin_state.engine_installs()
    found = [p for p in dict.fromkeys([*enabled, *installs]) if _is_watch(p, data)]
    if not found:
        return "missing", "the watch plugin is not installed"
    state = plugin_state.state_of(found[0], bool(enabled.get(found[0], False)), data, installs, engine=default_engine())
    if state["state"] == "active":
        return "active", ""
    if state["state"] == "claude_only":
        return "other_engine", "the watch plugin only works with the Claude Code engine"
    if state["state"] == "disabled":
        return "disabled", "the watch plugin is turned off"
    return "missing", state["reason"] or "the watch plugin is not ready"


def _check_watch(status: Callable[[], tuple[str, str]] | None) -> None:
    state, reason = (status or plugin_status)()
    if state == "active":
        return
    if state == "other_engine":
        raise SourceError("unavailable", "the watch plugin only works with the Claude Code engine", ["Change the engine of this conversation to Claude Code."])
    raise SourceError("unavailable", f"video needs the watch plugin: {reason}", [INSTALL_HINT])


def _clean_meta(meta: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(meta or {})
    extra = sorted(set(raw) - {"duration_s", "captions", "frames_seen", "frames_only"})
    if extra:
        raise SourceError("bad_input", f"video_meta has duration_s, captions, frames_seen and frames_only, not {extra[0]!r}")
    out: dict[str, Any] = {}
    if "duration_s" in raw:
        if isinstance(raw["duration_s"], bool) or not isinstance(raw["duration_s"], (int, float)) or not 0 <= raw["duration_s"] <= 86_400 * 2:
            raise SourceError("bad_input", "duration_s is a number of seconds from 0 to 172800")
        out["duration_s"] = raw["duration_s"]
    if "captions" in raw:
        if raw["captions"] not in CAPTIONS:
            raise SourceError("bad_input", f"captions is one of {', '.join(CAPTIONS)}")
        out["captions"] = raw["captions"]
    if "frames_seen" in raw:
        if isinstance(raw["frames_seen"], bool) or not isinstance(raw["frames_seen"], int) or not 0 <= raw["frames_seen"] <= 100_000:
            raise SourceError("bad_input", "frames_seen is a whole number from 0 to 100000")
        out["frames_seen"] = raw["frames_seen"]
    if "frames_only" in raw:
        if not isinstance(raw["frames_only"], bool):
            raise SourceError("bad_input", "frames_only is true or false")
        out["frames_only"] = raw["frames_only"]
    return out


def ingest_video(url: str, transcript_text: str, *, kind: Literal["video", "reel"] = "video", visual_notes: str | None = None,
                 meta: Mapping[str, Any] | None = None, rights: str | None = None, reference: str | None = None, caller: str,
                 watch_status: Callable[[], tuple[str, str]] | None = None, db: Database | None = None) -> SourceRow:
    """A video or a reel that Hokma already watched: its transcript with times and its notes about the frames. The passages of the transcript are
    blocks of 30 to 60 seconds ("03:12-03:40"); each note about a frame is a passage ("frame 03:12"). An empty transcript is valid only when
    `meta.frames_only` is true (a video with no captions and no speech to text): then only the notes are saved."""
    _check_kind(kind, ("video", "reel"))
    _check_watch(watch_status)
    if not isinstance(url, str) or not url.strip() or len(url) > MAX_URL or urlsplit(url).scheme.lower() not in ("http", "https"):
        raise SourceError("bad_input", "url is the http or https address of the video")
    info = _clean_meta(meta)
    if not isinstance(transcript_text, str) or len(transcript_text) > MAX_TRANSCRIPT_CHARS:
        raise SourceError("bad_input", f"transcript is text of at most {MAX_TRANSCRIPT_CHARS} characters")
    if visual_notes is not None and (not isinstance(visual_notes, str) or len(visual_notes) > MAX_NOTES_CHARS):
        raise SourceError("bad_input", f"visual_notes is text of at most {MAX_NOTES_CHARS} characters")
    parts: list[tuple[str, str]] = []
    if transcript_text.strip():
        try:
            parts += transcript.blocks(transcript.parse(transcript_text))
        except transcript.TranscriptError as exc:
            raise SourceError("bad_input", str(exc)) from None
    elif not info.get("frames_only"):
        raise SourceError("bad_input", "the transcript is empty: send it, or set video_meta.frames_only to true when the video has no speech and no captions")
    if visual_notes and visual_notes.strip():
        parts += transcript.frames(visual_notes)
    if not parts:
        raise SourceError("no_data", "there is neither a transcript nor notes about the frames to read")
    host = urlsplit(url).hostname or "video"
    content = "\n".join(f"{place}\t{body}" for place, body in parts)
    return _save(kind, reference=_reference(reference, f"{kind} on {host}"), url=url.strip(), document_id=None, rights=_rights(kind, rights), sha=_sha(content),
                 parts=parts, meta=info, caller=caller, truncated=False, db=db)


# ---------------------------------------------------------------------------------------------------- reading what was saved

def get_source(source_id: int, *, db: Database | None = None) -> SourceRow:
    if isinstance(source_id, bool) or not isinstance(source_id, int) or source_id < 1:
        raise SourceError("bad_input", "source_id is a whole number")
    with _db(db).read() as u:
        row = u.session.one("SELECT * FROM strategy_sources WHERE id = :i", {"i": source_id})
    if row is None:
        raise SourceError("no_data", f"there is no source {source_id}")
    return _row(row)


def read_passages(source_id: int, *, offset: int = 0, limit: int = 40, for_agent: bool = True, db: Database | None = None) -> PassagePage:
    """A page of the passages of a saved source, in order. `for_agent` keeps the text (Ian reads the source); without it only the locators come back."""
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise SourceError("bad_input", "offset is a whole number from 0")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= PAGE_LIMIT:
        raise SourceError("bad_input", f"limit is a whole number from 1 to {PAGE_LIMIT}")
    source = get_source(source_id, db=db)
    with _db(db).read() as u:
        rows = u.session.query("SELECT id, ord, locator, body FROM strategy_source_passages WHERE source_id = :s ORDER BY ord LIMIT :l OFFSET :o",
                               {"s": source_id, "l": limit, "o": offset})
    passages = [PassageRow(int(r["id"]), int(r["ord"]), r["locator"], r["body"] if for_agent else "") for r in rows]
    more = offset + len(passages)
    return PassagePage(source, passages, more if more < source.passage_count else None, source.passage_count)
