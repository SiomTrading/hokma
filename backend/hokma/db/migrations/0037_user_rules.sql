-- Strategy creator, A2 (SPEC-031): rules written by the strategy coder, their versions, events and the specs that use them.
-- Additive: new tables only. user_rule_events and strategy_spec_rules are append only; the code of a version never changes.

-- @engine: all

CREATE TABLE user_rules (
  id          {PK},
  slug        TEXT NOT NULL UNIQUE CHECK (length(slug) BETWEEN 2 AND 40),     -- ^[a-z][a-z0-9_]{1,39}$, checked by the code
  kind        TEXT NOT NULL CHECK (kind IN ('alpha','filter','exit','strategy')),
  title       TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 160),
  bank_id     INTEGER NOT NULL REFERENCES strategy_bank(id) ON DELETE RESTRICT,
  component   TEXT NOT NULL,                                                  -- one of the pending components of the extraction sheet
  created_by  TEXT NOT NULL,
  created_at  {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE user_rule_versions (
  id               {PK},
  rule_id          INTEGER NOT NULL REFERENCES user_rules(id) ON DELETE RESTRICT,
  version          INTEGER NOT NULL CHECK (version >= 1),
  registry_id      TEXT NOT NULL UNIQUE,                                      -- u_<slug>_v<version>
  request_version  INTEGER REFERENCES strategy_bank_versions(id) ON DELETE RESTRICT,   -- the bank version whose needs_new_rule asked for it
  code_sha256      TEXT NOT NULL CHECK (length(code_sha256) = 64),
  code_bytes       INTEGER NOT NULL CHECK (code_bytes BETWEEN 1 AND 32768),
  code_text        TEXT NOT NULL,                                             -- the source; the file in the data folder must have the same sha256
  cases_json       TEXT NOT NULL,                                             -- the test cases as data
  manifest_json    TEXT,                                                      -- what the isolated process read: schema, facts, lookback, requests
  check_json       TEXT NOT NULL DEFAULT '[]',                                -- findings of the static check
  test_json        TEXT,                                                      -- results of coder_test
  status           TEXT NOT NULL DEFAULT 'draft'
                   CHECK (status IN ('draft','failed','generated','owner_reviewed','rejected','retired')),
  reviewed_by      TEXT,
  reviewed_at      {TS},
  reviewed_sha256  TEXT,
  review_note      TEXT CHECK (review_note IS NULL OR length(review_note) <= 2000),
  created_by       TEXT NOT NULL,
  created_at       {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (rule_id, version),
  UNIQUE (rule_id, code_sha256),
  UNIQUE (id, code_sha256),                                                 -- the target of the composite key of strategy_spec_rules
  CHECK ((status = 'owner_reviewed') = (reviewed_at IS NOT NULL AND reviewed_sha256 IS NOT NULL AND reviewed_by IS NOT NULL)),
  CHECK (reviewed_sha256 IS NULL OR reviewed_sha256 = code_sha256),
  CHECK (status NOT IN ('generated','owner_reviewed') OR (manifest_json IS NOT NULL AND test_json IS NOT NULL))
);
CREATE INDEX idx_user_rule_versions_status ON user_rule_versions (status);

CREATE TABLE user_rule_events (
  id               {PK},
  rule_version_id  INTEGER NOT NULL REFERENCES user_rule_versions(id) ON DELETE RESTRICT,
  from_status      TEXT,
  to_status        TEXT NOT NULL,
  reason           TEXT,
  actor            TEXT NOT NULL,                                             -- agent slug, 'owner' or 'system', set by the code
  channel          TEXT NOT NULL CHECK (channel IN ('tool','app','terminal','system')),
  created_at       {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE strategy_spec_rules (
  strategy_id      TEXT NOT NULL,
  spec_version     INTEGER NOT NULL,
  rule_version_id  INTEGER NOT NULL,
  code_sha256      TEXT NOT NULL CHECK (length(code_sha256) = 64),
  created_at       {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (strategy_id, spec_version, rule_version_id),
  FOREIGN KEY (rule_version_id, code_sha256) REFERENCES user_rule_versions (id, code_sha256) ON DELETE RESTRICT   -- the hash a spec stores is the hash of that version
);

-- @engine: sqlite

CREATE TRIGGER trg_rule_events_no_upd BEFORE UPDATE ON user_rule_events BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER trg_rule_events_no_del BEFORE DELETE ON user_rule_events BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER trg_spec_rules_no_upd BEFORE UPDATE ON strategy_spec_rules BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER trg_rule_code_frozen BEFORE UPDATE OF rule_id, version, code_text, code_sha256, cases_json, registry_id ON user_rule_versions
  BEGIN SELECT RAISE(ABORT, 'the code of a rule version never changes: write a new version'); END;

-- @engine: postgres

CREATE FUNCTION hokma_rule_code_frozen() RETURNS trigger AS $$
BEGIN
  IF NEW.rule_id IS DISTINCT FROM OLD.rule_id OR NEW.version IS DISTINCT FROM OLD.version OR NEW.code_text IS DISTINCT FROM OLD.code_text OR NEW.code_sha256 IS DISTINCT FROM OLD.code_sha256
     OR NEW.cases_json IS DISTINCT FROM OLD.cases_json OR NEW.registry_id IS DISTINCT FROM OLD.registry_id THEN
    RAISE EXCEPTION 'the code of a rule version never changes: write a new version';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_rule_code_frozen BEFORE UPDATE ON user_rule_versions
  FOR EACH ROW EXECUTE FUNCTION hokma_rule_code_frozen();
CREATE TRIGGER trg_rule_events_no_upd BEFORE UPDATE ON user_rule_events
  FOR EACH ROW EXECUTE FUNCTION hokma_trials_append_only();
CREATE TRIGGER trg_rule_events_no_del BEFORE DELETE ON user_rule_events
  FOR EACH ROW EXECUTE FUNCTION hokma_trials_append_only();
CREATE TRIGGER trg_spec_rules_no_upd BEFORE UPDATE ON strategy_spec_rules
  FOR EACH ROW EXECUTE FUNCTION hokma_trials_append_only();
