"""Library search (SPEC-010 P12, section 4.6): always by meaning, with the words of the question as an inner complement.

1. Meaning: the question is embedded and the vector store returns its nearest passages (the main signal).
2. Words: a keyword search over the same passages catches exact names, acronyms and tickers that meaning dilutes.
   It is never shown: the student sees one list of passages and no "match" kind.
3. Both ranked lists are fused with Reciprocal Rank Fusion, `score = sum(1 / (RRF_K + rank))`.
4. "Nothing found": a passage counts only when its similarity reaches MIN_SEMANTIC_SIM, or when it carries every
   word of the question (an exact match). Everything else is dropped, so an unrelated question returns nothing.
5. A Library folder filter includes the subfolders; without it the whole Library is searched.

The same code runs on SQLite and Postgres: only the two stores underneath differ.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from hokma.db.repos.documents import DEFAULT_LIMIT, EXCERPT_CHARS, MAX_LIMIT, make_excerpt, query_terms
from hokma.db.vectors import DEFAULT_CANDIDATES, VectorStore, eligible_where, open_store

RRF_K = 60
# Lowest cosine similarity (multilingual-e5-small, `query: ` against `passage: `) at which a passage counts as an
# answer. e5 compresses similarities into a narrow band, so this was calibrated with the real model on the questions
# of tests/test_hybrid_search.py: Spanish questions with an answer in English passages scored 0.78 to 0.86, questions
# with no relation 0.69 to 0.73.
MIN_SEMANTIC_SIM = 0.76

EmbedQuery = Callable[[str], Sequence[float]]


@dataclass(frozen=True)
class LibraryHit:
    """One passage found for a question. `origin` is `"library"` or `"folder:{label}"`; `library_path` is the Library
    folder (`Papers/2026`), empty at the top level or when the passage was read from a disk folder."""

    chunk_id: int
    document_id: int
    document: str
    section: str
    excerpt: str
    path: str
    format: str
    source_url: str | None
    origin: str
    library_path: str
    score: float

    def public(self) -> dict:
        return {"document": self.document, "section": self.section, "excerpt": self.excerpt, "path": self.path,
                "format": self.format, "source_url": self.source_url, "origin": self.origin,
                "library_path": self.library_path}


def fuse(*rankings: Sequence[int]) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion of ranked id lists: `(id, score)` best first, ties broken by the smaller id."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))


def search(u, embed_query: EmbedQuery, query: str, *, model_id: str, library_ids: Sequence[int] = (),
           folder_ids: Sequence[int] = (), library_folder_id: int | None = None, limit: int = DEFAULT_LIMIT,
           min_similarity: float = MIN_SEMANTIC_SIM, store: VectorStore | None = None) -> list[LibraryHit]:
    """Passages that answer `query`, best first; an empty list means there is none.

    `u` is an open unit of work. `library_ids` are the Libraries to search and `folder_ids` the disk folders (of a
    project); with `library_folder_id` only that Library folder and its subfolders are searched (disk folders are
    left out, they are not inside any Library folder). `embed_query` turns the question into a vector. `store` is
    the vector store to use; by default the one the engine opens."""
    if not (query or "").strip():
        return []
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    folder_tree = None
    if library_folder_id is not None:
        folder_tree = u.library.descendant_ids(library_folder_id)       # NotFound when the folder does not exist
        folder_ids = ()
    where = eligible_where(library_ids, folder_ids, folder_tree)
    if where is None:
        return []

    store = store or open_store(u.session)
    meaning = store.search(embed_query(query), library_ids=library_ids, folder_ids=folder_ids,
                           library_folder_ids=folder_tree, limit=DEFAULT_CANDIDATES, model_id=model_id)
    similarity = {hit.chunk_id: hit.similarity for hit in meaning}
    words, every_word = u.documents.keyword_candidates(query, where, DEFAULT_CANDIDATES)

    # Only words that carry every term of the question may stand alone; a partial overlap needs the meaning too.
    exact = set(words) if every_word else set()
    ranked = [(cid, score) for cid, score in fuse([h.chunk_id for h in meaning], words)
              if similarity.get(cid, 0.0) >= min_similarity or cid in exact][:limit]
    return _describe(u, ranked, query)


def _describe(u, ranked: list[tuple[int, float]], query: str) -> list[LibraryHit]:
    if not ranked:
        return []
    ids = ",".join(str(int(cid)) for cid, _ in ranked)
    rows = {int(r["chunk_id"]): r for r in u.session.query(
        "SELECT c.id AS chunk_id, c.document_id, c.section, c.body, d.name AS document, d.stored_path AS path, "
        "d.format, d.source_url, d.library_folder_id, d.folder_id, pf.label AS folder_label, pf.path AS folder_path "
        "FROM doc_chunks c JOIN documents d ON d.id = c.document_id "
        f"LEFT JOIN project_folders pf ON pf.id = d.folder_id WHERE c.id IN ({ids})")}
    terms = query_terms(query)
    paths: dict[int, str] = {}
    hits = []
    for cid, score in ranked:
        row = rows.get(cid)
        if row is None:
            continue
        if row["folder_id"] is not None:
            origin, library_path = f"folder:{row['folder_label'] or row['folder_path']}", ""
        else:
            origin = "library"
            lf = row["library_folder_id"]
            library_path = "" if lf is None else paths.setdefault(int(lf), u.library.folder_path(int(lf)))
        hits.append(LibraryHit(
            chunk_id=cid, document_id=int(row["document_id"]), document=row["document"], section=row["section"] or "",
            excerpt=make_excerpt(row["body"], terms, EXCERPT_CHARS), path=row["path"], format=row["format"],
            source_url=row["source_url"], origin=origin, library_path=library_path, score=score))
    return hits
