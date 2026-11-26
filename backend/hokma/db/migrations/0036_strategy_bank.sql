-- Strategy creator, A1 (SPEC-030): sources, their passages, the bank of strategies and its versions.
-- Additive: new tables only. strategy_bank_events is append only.

-- @engine: all

CREATE TABLE strategy_sources (
  id                 {PK},
  kind               TEXT NOT NULL CHECK (kind IN ('text','question','pdf','document','link','video','reel')),
  reference          TEXT NOT NULL CHECK (length(trim(reference)) BETWEEN 1 AND 300),   -- title, author, channel, book and edition
  url                TEXT CHECK (url IS NULL OR length(url) <= 2000),
  document_id        INTEGER REFERENCES documents(id) ON DELETE SET NULL,
  rights             TEXT NOT NULL CHECK (rights IN ('own','third_party','unknown')),
  content_sha256     TEXT NOT NULL CHECK (length(content_sha256) = 64),
  passage_count      INTEGER NOT NULL DEFAULT 0 CHECK (passage_count >= 0),
  truncated          INTEGER NOT NULL DEFAULT 0 CHECK (truncated IN (0,1)),
  injection_json     TEXT NOT NULL DEFAULT '[]',     -- findings of the scanner; the text is never dropped
  meta_json          TEXT NOT NULL DEFAULT '{}',     -- video: duration, captions or whisper, frames seen; link: final url, content type
  created_by         TEXT NOT NULL,                  -- agent slug of the caller, set by the code
  created_at         {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (kind, content_sha256)
);

CREATE TABLE strategy_source_passages (
  id          {PK},
  source_id   INTEGER NOT NULL REFERENCES strategy_sources(id) ON DELETE CASCADE,
  ord         INTEGER NOT NULL CHECK (ord >= 0),
  locator     TEXT NOT NULL CHECK (length(locator) BETWEEN 1 AND 120),   -- "page 4", "Entry rules", "03:12-03:40", "paragraph 7"
  body        TEXT NOT NULL,
  body_sha256 TEXT NOT NULL CHECK (length(body_sha256) = 64),
  UNIQUE (source_id, ord)
);

CREATE TABLE strategy_bank (
  id            {PK},
  slug          TEXT NOT NULL UNIQUE CHECK (length(slug) BETWEEN 1 AND 80),
  title         TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 160),
  line          TEXT CHECK (line IS NULL OR line IN ('RF','DT','SWC','SWO','LP')),
  status        TEXT NOT NULL DEFAULT 'idea'
                CHECK (status IN ('idea','draft','testing','ready','not_working','live','off')),
  close_reason  TEXT CHECK (close_reason IS NULL OR length(trim(close_reason)) BETWEEN 20 AND 4000),
  pivots_used   INTEGER NOT NULL DEFAULT 0 CHECK (pivots_used >= 0),      -- D4, enforced in B (limit 5)
  created_at    {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK ((status = 'not_working') = (close_reason IS NOT NULL))
);

CREATE TABLE strategy_bank_versions (
  id               {PK},
  bank_id          INTEGER NOT NULL REFERENCES strategy_bank(id) ON DELETE RESTRICT,
  version          INTEGER NOT NULL CHECK (version >= 1),
  source_id        INTEGER NOT NULL REFERENCES strategy_sources(id) ON DELETE RESTRICT,
  derived_from     INTEGER REFERENCES strategy_bank_versions(id) ON DELETE RESTRICT,
  extraction_json  TEXT NOT NULL,                     -- the five components (SPEC-030 section 5.3)
  extraction_hash  TEXT NOT NULL,
  needs_rule_json  TEXT NOT NULL DEFAULT '[]',        -- rules the factory does not have: "needs a new rule (A2)"
  strategy_id      TEXT,                              -- strategy_specs.strategy_id when the ficha maps entirely
  spec_version     INTEGER,
  evidence_stage   TEXT NOT NULL DEFAULT 'hypothesis'
                   CHECK (evidence_stage IN ('hypothesis','backtest','walkforward','paper','live','retired')),
  created_by       TEXT NOT NULL,
  created_at       {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (bank_id, version),
  CHECK ((strategy_id IS NULL) = (spec_version IS NULL)),
  CHECK (needs_rule_json = '[]' OR strategy_id IS NULL)   -- a version that needs a new rule has no spec: nothing is approximated
);
CREATE INDEX idx_bank_versions ON strategy_bank_versions (bank_id, version);
CREATE INDEX idx_bank_versions_spec ON strategy_bank_versions (strategy_id, spec_version);

CREATE TABLE strategy_bank_events (
  id          {PK},
  bank_id     INTEGER NOT NULL REFERENCES strategy_bank(id) ON DELETE RESTRICT,
  from_status TEXT,
  to_status   TEXT NOT NULL,
  reason      TEXT,
  actor       TEXT NOT NULL,                          -- agent slug or 'owner', set by the code
  created_at  {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- @engine: sqlite

CREATE TRIGGER trg_bank_events_no_upd BEFORE UPDATE ON strategy_bank_events BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER trg_bank_events_no_del BEFORE DELETE ON strategy_bank_events BEGIN SELECT RAISE(ABORT, 'append-only table'); END;

-- @engine: postgres

CREATE TRIGGER trg_bank_events_no_upd BEFORE UPDATE ON strategy_bank_events
  FOR EACH ROW EXECUTE FUNCTION hokma_trials_append_only();
CREATE TRIGGER trg_bank_events_no_del BEFORE DELETE ON strategy_bank_events
  FOR EACH ROW EXECUTE FUNCTION hokma_trials_append_only();
