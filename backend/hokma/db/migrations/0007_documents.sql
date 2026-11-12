-- Project documents with local keyword search (task 3187, SPEC-009, Hokma 0.0.26).
-- A library holds documents; a document is split into passages (doc_chunks); the passages are indexed for search
-- (FTS5 on SQLite, a GIN index over to_tsvector on Postgres). Today only scope 'project' is used: 'student' and
-- 'course' are accepted by the CHECK so a later library kind does not need another migration.
-- Additive: new tables only. Version 0.0.25 ignores them.
--
-- doc_chunks.search_text is the passage text folded in Python (lower case, no accents, words separated by single
-- spaces); queries are folded the same way, so both engines match alike without unaccent. The Postgres index is over
-- an expression and not a generated column, so the table has the same columns on both engines and the SQLite <->
-- Postgres copy keeps working with `SELECT *`.

-- @engine: all

CREATE TABLE libraries (
  id         {PK},
  scope      TEXT NOT NULL CHECK (scope IN ('project','student','course')),
  project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
  name       TEXT,
  source     TEXT,
  version    TEXT,
  created_at {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK ((scope = 'project') = (project_id IS NOT NULL)),
  UNIQUE (scope, project_id)
);

CREATE TABLE documents (
  id            {PK},
  library_id    INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  name          TEXT NOT NULL,
  stored_path   TEXT NOT NULL UNIQUE,
  mime_type     TEXT NOT NULL,
  format        TEXT NOT NULL,
  size_bytes    INTEGER NOT NULL CHECK (size_bytes > 0 AND size_bytes <= 52428800),
  sha256        TEXT NOT NULL CHECK (length(sha256) = 64),
  source_url    TEXT,
  description   TEXT,
  status        TEXT NOT NULL CHECK (status IN ('indexing','ready','failed')),
  status_detail TEXT,
  chunk_count   INTEGER NOT NULL DEFAULT 0,
  created_at    {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  indexed_at    {TS},
  UNIQUE (library_id, sha256)
);

CREATE TABLE doc_chunks (
  id          {PK},
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ord         INTEGER NOT NULL,
  section     TEXT NOT NULL,
  body        TEXT NOT NULL,
  search_text TEXT NOT NULL,
  UNIQUE (document_id, ord)
);

CREATE INDEX idx_documents_library ON documents(library_id);

-- @engine: sqlite

CREATE VIRTUAL TABLE doc_chunks_fts USING fts5(
  search_text, content='doc_chunks', content_rowid='id', tokenize='unicode61 remove_diacritics 2');

CREATE TRIGGER trg_doc_chunks_fts_ai AFTER INSERT ON doc_chunks
BEGIN
  INSERT INTO doc_chunks_fts(rowid, search_text) VALUES (NEW.id, NEW.search_text);
END;

CREATE TRIGGER trg_doc_chunks_fts_ad AFTER DELETE ON doc_chunks
BEGIN
  INSERT INTO doc_chunks_fts(doc_chunks_fts, rowid, search_text) VALUES ('delete', OLD.id, OLD.search_text);
END;

CREATE TRIGGER trg_doc_chunks_fts_au AFTER UPDATE ON doc_chunks
BEGIN
  INSERT INTO doc_chunks_fts(doc_chunks_fts, rowid, search_text) VALUES ('delete', OLD.id, OLD.search_text);
  INSERT INTO doc_chunks_fts(rowid, search_text) VALUES (NEW.id, NEW.search_text);
END;

-- @engine: postgres

CREATE INDEX idx_doc_chunks_tsv ON doc_chunks USING GIN (to_tsvector('simple', search_text));
