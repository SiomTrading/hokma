-- The student's single Library: disk folders per project, folders inside the Library and the control table for
-- passage vectors (task 3204, SPEC-010, Hokma 0.0.27).
-- Additive: new tables, new nullable columns and indexes. Version 0.0.26 ignores them. The Library itself (the
-- 'student' library) is created on first use by the code, not here. The vectors live in an engine specific store that
-- the code creates after the extension is loaded; doc_chunk_vectors only records which passage has a vector and from
-- which model.
--
-- ALTER statements use the plain `ADD COLUMN` form so the SQLite <-> Postgres copy can read them.

-- @engine: all

-- one student library per installation (NULL project_id never collides in a plain UNIQUE, so a partial index)
CREATE UNIQUE INDEX ux_libraries_student ON libraries(scope) WHERE scope = 'student';

CREATE TABLE project_folders (
  id            {PK},
  project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  path          TEXT NOT NULL,
  label         TEXT,
  status        TEXT NOT NULL DEFAULT 'scanning' CHECK (status IN ('scanning','ready','missing','failed')),
  status_detail TEXT,
  file_count    INTEGER NOT NULL DEFAULT 0 CHECK (file_count >= 0),
  last_scan_at  {TS},
  created_at    {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (project_id, path)
);

CREATE INDEX idx_project_folders_project ON project_folders(project_id);

-- folders inside the Library (organisation only; a tree through parent_id, NULL parent = top level)
CREATE TABLE library_folders (
  id         {PK},
  library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  parent_id  INTEGER REFERENCES library_folders(id) ON DELETE CASCADE,
  name       TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  created_at {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- sibling names are unique without regard to case, also at the top level
CREATE UNIQUE INDEX ux_libfolders_child ON library_folders(library_id, parent_id, lower(name)) WHERE parent_id IS NOT NULL;
CREATE UNIQUE INDEX ux_libfolders_root ON library_folders(library_id, lower(name)) WHERE parent_id IS NULL;
CREATE INDEX idx_libfolders_parent ON library_folders(parent_id);

ALTER TABLE documents ADD COLUMN origin_project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL;
ALTER TABLE documents ADD COLUMN folder_id INTEGER REFERENCES project_folders(id) ON DELETE CASCADE;
ALTER TABLE documents ADD COLUMN rel_path TEXT;
ALTER TABLE documents ADD COLUMN mtime_ns BIGINT;
ALTER TABLE documents ADD COLUMN library_folder_id INTEGER REFERENCES library_folders(id) ON DELETE CASCADE;

CREATE INDEX idx_documents_folder ON documents(folder_id);
CREATE INDEX idx_documents_library_folder ON documents(library_folder_id);

-- which passage has a vector and from which model (the vector itself is stored by the engine specific store)
CREATE TABLE doc_chunk_vectors (
  chunk_id INTEGER PRIMARY KEY REFERENCES doc_chunks(id) ON DELETE CASCADE,
  model_id TEXT NOT NULL,
  dim      INTEGER NOT NULL CHECK (dim > 0)
);
