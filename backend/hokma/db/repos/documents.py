"""Project libraries, their documents and passages, and keyword search over the passages."""
from __future__ import annotations

import re
import unicodedata
from typing import Sequence

from hokma.db.errors import DuplicateDocument, IntegrityViolation, NotFound, ValidationError
from hokma.db.models import DocHit, Document, Library
from hokma.db.repos.base import Repo, require_in
from hokma.db.timeutil import utcnow

STATUSES = ("indexing", "ready", "failed")
MAX_SIZE_BYTES = 52_428_800
DEFAULT_LIMIT = 8
MAX_LIMIT = 50
MAX_TERMS = 24
EXCERPT_CHARS = 300
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _fold_char(ch: str) -> str:
    """One character folded for search: lower case, accents removed, anything that is not a letter or digit a space."""
    out = []
    for c in unicodedata.normalize("NFKD", ch).lower():
        if unicodedata.combining(c):
            continue
        out.append(c if c.isalnum() else " ")
    return "".join(out)


def fold(text: str) -> str:
    """The search form of a text: lower case, no accents, words separated by single spaces."""
    return " ".join("".join(_fold_char(c) for c in (text or "")).split())


def query_terms(query: str) -> list[str]:
    """The distinct words of a query in search form, in order; at most MAX_TERMS."""
    terms: list[str] = []
    for word in fold(query).split():
        if word not in terms:
            terms.append(word)
    return terms[:MAX_TERMS]


def make_excerpt(body: str, terms: Sequence[str], size: int = EXCERPT_CHARS) -> str:
    """About `size` characters of `body` around the first word of the query that appears in it."""
    body = body or ""
    if len(body) <= size:
        return body.strip()
    folded, origin = [], []
    for index, ch in enumerate(body):
        piece = _fold_char(ch)
        folded.append(piece)
        origin.extend([index] * len(piece))
    flat = "".join(folded)
    at = -1
    for term in terms:
        found = re.search(r"(?<![^\W_])" + re.escape(term), flat)
        if found and (at < 0 or found.start() < at):
            at = found.start()
    center = origin[at] if 0 <= at < len(origin) else 0
    start = max(0, min(center - size // 3, len(body) - size))
    end = min(len(body), start + size)
    if start > 0:
        start = _word_edge(body, start, forward=True, limit=end)
    if end < len(body):
        end = _word_edge(body, end, forward=False, limit=start)
    text = body[start:end].strip()
    return ("..." if start > 0 else "") + text + ("..." if end < len(body) else "")


def _word_edge(body: str, pos: int, *, forward: bool, limit: int) -> int:
    """Move `pos` to a word boundary without crossing `limit`."""
    if forward:
        while pos < limit and not body[pos - 1].isspace():
            pos += 1
    else:
        while pos > limit and not body[pos].isspace():
            pos -= 1
    return pos


class DocumentsRepo(Repo):
    # ---------------------------------------------------------------------------------------- libraries
    def find_project_library(self, project_id: int) -> Library | None:
        row = self.s.one("SELECT * FROM libraries WHERE scope = 'project' AND project_id = :p", {"p": project_id})
        return Library.from_row(row) if row else None

    def create_project_library(self, project_id: int, *, name: str | None = None) -> Library:
        """The library of a project; created on first use, the same one afterwards."""
        existing = self.find_project_library(project_id)
        if existing:
            return existing
        project = self.s.one("SELECT name FROM projects WHERE id = :p", {"p": project_id})
        if project is None:
            raise NotFound(f"projects {project_id!r} not found")
        new_id = self.s.insert("libraries", {
            "scope": "project", "project_id": project_id, "name": name or project["name"], "created_at": utcnow()})
        return self.get_library(new_id)

    def get_library(self, id: int) -> Library:
        return self._get("libraries", "id", id, Library)

    # ---------------------------------------------------------------------------------------- documents
    def add_document(self, library_id: int, *, name: str, stored_path: str, mime_type: str, format: str,
                     size_bytes: int, sha256: str, chunks: Sequence[tuple[str, str]] = (),
                     source_url: str | None = None, description: str | None = None, status: str = "ready",
                     status_detail: str | None = None) -> Document:
        """Insert a document and its passages (`chunks` is a list of `(section, text)`). Raises DuplicateDocument when
        the library already has that sha256."""
        if not (name or "").strip() or not (stored_path or "").strip():
            raise ValidationError("name and stored_path are required")
        if not (1 <= size_bytes <= MAX_SIZE_BYTES):
            raise ValidationError(f"size_bytes must be between 1 and {MAX_SIZE_BYTES}")
        sha256 = (sha256 or "").lower()
        if not _SHA256.match(sha256):
            raise ValidationError("sha256 must be 64 hexadecimal characters")
        require_in("status", status, STATUSES)
        self.get_library(library_id)
        same = self.find_by_sha256(library_id, sha256)
        if same:
            raise DuplicateDocument(same.id)
        try:
            new_id = self.s.insert("documents", {
                "library_id": library_id, "name": name, "stored_path": stored_path, "mime_type": mime_type,
                "format": format, "size_bytes": size_bytes, "sha256": sha256, "source_url": source_url,
                "description": description, "status": status, "status_detail": status_detail, "chunk_count": 0,
                "created_at": utcnow(), "indexed_at": utcnow() if status == "ready" else None})
        except IntegrityViolation as exc:
            if "stored_path" in str(exc):
                raise ValidationError("stored_path is already used by another document") from exc
            raise
        if chunks:
            self.add_chunks(new_id, chunks)
        return self.get_document(new_id)

    def add_chunks(self, document_id: int, chunks: Sequence[tuple[str, str]]) -> int:
        """Append passages after the ones the document already has; returns how many were added."""
        self.get_document(document_id)
        start = int(self.s.scalar("SELECT COALESCE(MAX(ord), -1) + 1 FROM doc_chunks WHERE document_id = :d",
                                  {"d": document_id}))
        rows = []
        for section, text in chunks:
            search_text = fold(text)
            if not search_text:
                continue
            rows.append({"d": document_id, "o": start + len(rows), "s": section or "", "b": text, "t": search_text})
        if rows:
            self.s.execute_many("INSERT INTO doc_chunks (document_id, ord, section, body, search_text) "
                                "VALUES (:d, :o, :s, :b, :t)", rows)
            self.s.execute("UPDATE documents SET chunk_count = (SELECT count(*) FROM doc_chunks WHERE document_id = :d) "
                           "WHERE id = :d", {"d": document_id})
        return len(rows)

    def set_status(self, document_id: int, status: str, detail: str | None = None) -> Document:
        require_in("status", status, STATUSES)
        self.get_document(document_id)
        self.s.update("documents", {"id": document_id}, {
            "status": status, "status_detail": detail, "indexed_at": utcnow() if status == "ready" else None})
        return self.get_document(document_id)

    def set_stored_path(self, document_id: int, stored_path: str) -> Document:
        """Point a document at its final file once the id that names the file is known."""
        if not (stored_path or "").strip():
            raise ValidationError("stored_path is required")
        self.get_document(document_id)
        try:
            self.s.update("documents", {"id": document_id}, {"stored_path": stored_path})
        except IntegrityViolation as exc:
            raise ValidationError("stored_path is already used by another document") from exc
        return self.get_document(document_id)

    def get_document(self, id: int) -> Document:
        return self._get("documents", "id", id, Document)

    def find_by_sha256(self, library_id: int, sha256: str) -> Document | None:
        row = self.s.one("SELECT * FROM documents WHERE library_id = :l AND sha256 = :h",
                         {"l": library_id, "h": (sha256 or "").lower()})
        return Document.from_row(row) if row else None

    def list_documents(self, library_id: int) -> list[Document]:
        rows = self.s.query("SELECT * FROM documents WHERE library_id = :l ORDER BY id", {"l": library_id})
        return [Document.from_row(r) for r in rows]

    def remove_document(self, document_id: int) -> Document:
        """Delete a document and its passages (and their search entries). Returns the removed row so the caller can
        delete the stored file."""
        doc = self.get_document(document_id)
        self.s.execute("DELETE FROM documents WHERE id = :d", {"d": document_id})
        return doc

    # ---------------------------------------------------------------------------------------- search
    def search(self, library_ids: Sequence[int], query: str, limit: int = DEFAULT_LIMIT) -> list[DocHit]:
        """Passages of ready documents that match the words of `query`, best first. All the words are tried first;
        when nothing has them all, any of the words. An empty list means no match."""
        ids = [int(i) for i in library_ids]
        terms = query_terms(query)
        if not ids or not terms:
            return []
        limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
        for joiner in ("AND", "OR") if len(terms) > 1 else ("AND",):
            rows = self._match(ids, terms, joiner, limit)
            if rows:
                return [DocHit(chunk_id=r["chunk_id"], document_id=r["document_id"], document=r["document"],
                               section=r["section"], excerpt=make_excerpt(r["body"], terms), path=r["path"],
                               format=r["format"], source_url=r["source_url"], score=float(r["score"]))
                        for r in rows]
        return []

    def _match(self, ids: list[int], terms: list[str], joiner: str, limit: int) -> list[dict]:
        marks = ", ".join(f":l{i}" for i in range(len(ids)))
        params: dict = {f"l{i}": value for i, value in enumerate(ids)} | {"n": limit}
        if self.s.engine == "sqlite":
            params["q"] = f" {joiner} ".join(f'"{t}"' for t in terms)          # terms hold only letters and digits
            sql = ("SELECT c.id AS chunk_id, c.document_id, d.name AS document, c.section, c.body, "
                   "d.stored_path AS path, d.format, d.source_url, -bm25(doc_chunks_fts) AS score "
                   "FROM doc_chunks_fts JOIN doc_chunks c ON c.id = doc_chunks_fts.rowid "
                   "JOIN documents d ON d.id = c.document_id "
                   f"WHERE doc_chunks_fts MATCH :q AND d.status = 'ready' AND d.library_id IN ({marks}) "
                   "ORDER BY bm25(doc_chunks_fts), c.id LIMIT :n")
        else:
            params["q"] = (" & " if joiner == "AND" else " | ").join(f"'{t}'" for t in terms)
            sql = ("SELECT c.id AS chunk_id, c.document_id, d.name AS document, c.section, c.body, "
                   "d.stored_path AS path, d.format, d.source_url, "
                   "ts_rank(to_tsvector('simple', c.search_text), to_tsquery('simple', :q)) AS score "
                   "FROM doc_chunks c JOIN documents d ON d.id = c.document_id "
                   "WHERE to_tsvector('simple', c.search_text) @@ to_tsquery('simple', :q) AND d.status = 'ready' "
                   f"AND d.library_id IN ({marks}) ORDER BY score DESC, c.id LIMIT :n")
        return self.s.query(sql, params)
