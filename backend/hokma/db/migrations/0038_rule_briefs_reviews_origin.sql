-- Strategy creator, A2 (SPEC-031 rev 3): the requirement of a strategy (a brief), the verdicts of the code reviewer and where each strategy was created.
-- Additive: new tables and nullable columns only. user_rule_reviews is append only; a brief never changes once written (a change is a new version).

-- @engine: all

CREATE TABLE user_rule_briefs (
  id                {PK},
  rule_id           INTEGER NOT NULL REFERENCES user_rules(id) ON DELETE RESTRICT,
  version           INTEGER NOT NULL CHECK (version >= 1),
  request_version   INTEGER NOT NULL REFERENCES strategy_bank_versions(id) ON DELETE RESTRICT,   -- the bank version the requirement answers
  brief_json        TEXT NOT NULL,                                   -- the requirement of the strategy: input of the first gate, never shown as a document
  brief_sha256      TEXT NOT NULL CHECK (length(brief_sha256) = 64),
  status            TEXT NOT NULL DEFAULT 'current' CHECK (status IN ('current','superseded')),
  origin_desk_id    INTEGER,                                         -- NULL: written in Hokma (main chat); no foreign key on purpose
  origin_desk_name  TEXT CHECK (origin_desk_name IS NULL OR length(origin_desk_name) BETWEEN 1 AND 80),
  created_by        TEXT NOT NULL,
  created_at        {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (rule_id, version),
  UNIQUE (rule_id, brief_sha256),
  CHECK ((origin_desk_id IS NULL) = (origin_desk_name IS NULL))
);
CREATE UNIQUE INDEX uq_rule_brief_current ON user_rule_briefs (rule_id) WHERE status = 'current';

CREATE TABLE user_rule_reviews (
  id                {PK},
  rule_version_id   INTEGER NOT NULL REFERENCES user_rule_versions(id) ON DELETE RESTRICT,
  brief_id          INTEGER NOT NULL REFERENCES user_rule_briefs(id) ON DELETE RESTRICT,
  code_sha256       TEXT NOT NULL CHECK (length(code_sha256) = 64),
  test_json         TEXT NOT NULL,                                   -- the green run of the checks on this hash: tests, fixed tests, static findings
  verdict           TEXT NOT NULL CHECK (verdict IN ('approved','rejected')),
  findings_json     TEXT NOT NULL DEFAULT '[]',                      -- [{severity, line, message}]
  reviewer          TEXT NOT NULL,
  created_at        {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_rule_reviews ON user_rule_reviews (rule_version_id, id);

ALTER TABLE user_rule_versions     ADD COLUMN brief_id INTEGER REFERENCES user_rule_briefs(id);   -- the requirement the code was written against
ALTER TABLE strategy_bank          ADD COLUMN origin_desk_id INTEGER;
ALTER TABLE strategy_bank          ADD COLUMN origin_desk_name TEXT CHECK (origin_desk_name IS NULL OR length(origin_desk_name) BETWEEN 1 AND 80);
ALTER TABLE strategy_bank_versions ADD COLUMN origin_desk_id INTEGER;
ALTER TABLE strategy_bank_versions ADD COLUMN origin_desk_name TEXT CHECK (origin_desk_name IS NULL OR length(origin_desk_name) BETWEEN 1 AND 80);
ALTER TABLE user_rules             ADD COLUMN origin_desk_id INTEGER;
ALTER TABLE user_rules             ADD COLUMN origin_desk_name TEXT CHECK (origin_desk_name IS NULL OR length(origin_desk_name) BETWEEN 1 AND 80);
ALTER TABLE user_rule_versions     ADD COLUMN origin_desk_id INTEGER;
ALTER TABLE user_rule_versions     ADD COLUMN origin_desk_name TEXT CHECK (origin_desk_name IS NULL OR length(origin_desk_name) BETWEEN 1 AND 80);
CREATE INDEX idx_bank_origin ON strategy_bank (origin_desk_id);

-- @engine: sqlite

CREATE TRIGGER trg_rule_reviews_no_upd BEFORE UPDATE ON user_rule_reviews BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER trg_rule_reviews_no_del BEFORE DELETE ON user_rule_reviews BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER trg_brief_frozen BEFORE UPDATE OF brief_json, brief_sha256, rule_id, version, request_version ON user_rule_briefs
  BEGIN SELECT RAISE(ABORT, 'a brief never changes: write a new version'); END;
-- GATE 2 in the base: a version is generated or owner_reviewed only when its last verdict is an approval of the same code, and a strategy is linked to a
-- spec only through a version in that situation.
CREATE TRIGGER trg_rule_owner_needs_agent BEFORE UPDATE OF status ON user_rule_versions
  WHEN NEW.status IN ('generated','owner_reviewed') AND NOT EXISTS (
    SELECT 1 FROM user_rule_reviews r
    WHERE r.id = (SELECT max(id) FROM user_rule_reviews WHERE rule_version_id = NEW.id)
      AND r.verdict = 'approved' AND r.code_sha256 = NEW.code_sha256)
  BEGIN SELECT RAISE(ABORT, 'the code reviewer has not approved this code'); END;
-- The same on INSERT: a version cannot be born generated or owner_reviewed, it has no verdict yet.
CREATE TRIGGER trg_rule_owner_needs_agent_ins BEFORE INSERT ON user_rule_versions
  WHEN NEW.status IN ('generated','owner_reviewed') AND NOT EXISTS (
    SELECT 1 FROM user_rule_reviews r
    WHERE r.id = (SELECT max(id) FROM user_rule_reviews WHERE rule_version_id = NEW.id)
      AND r.verdict = 'approved' AND r.code_sha256 = NEW.code_sha256)
  BEGIN SELECT RAISE(ABORT, 'the code reviewer has not approved this code'); END;
CREATE TRIGGER trg_spec_rules_need_review BEFORE INSERT ON strategy_spec_rules
  WHEN NOT EXISTS (
    SELECT 1 FROM user_rule_versions v
    WHERE v.id = NEW.rule_version_id AND v.status IN ('generated','owner_reviewed')
      AND EXISTS (SELECT 1 FROM user_rule_reviews r
                  WHERE r.id = (SELECT max(id) FROM user_rule_reviews WHERE rule_version_id = v.id)
                    AND r.verdict = 'approved' AND r.code_sha256 = v.code_sha256))
  BEGIN SELECT RAISE(ABORT, 'the code reviewer has not approved this code'); END;

-- @engine: postgres

CREATE FUNCTION hokma_rule_brief_frozen() RETURNS trigger AS $$
BEGIN
  IF NEW.brief_json IS DISTINCT FROM OLD.brief_json OR NEW.brief_sha256 IS DISTINCT FROM OLD.brief_sha256 OR NEW.rule_id IS DISTINCT FROM OLD.rule_id
     OR NEW.version IS DISTINCT FROM OLD.version OR NEW.request_version IS DISTINCT FROM OLD.request_version THEN
    RAISE EXCEPTION 'a brief never changes: write a new version';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE FUNCTION hokma_rule_owner_needs_agent() RETURNS trigger AS $$
BEGIN
  IF NEW.status IN ('generated','owner_reviewed') AND NOT EXISTS (
       SELECT 1 FROM user_rule_reviews r
       WHERE r.id = (SELECT max(id) FROM user_rule_reviews WHERE rule_version_id = NEW.id)
         AND r.verdict = 'approved' AND r.code_sha256 = NEW.code_sha256) THEN
    RAISE EXCEPTION 'the code reviewer has not approved this code';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE FUNCTION hokma_spec_rules_need_review() RETURNS trigger AS $$
BEGIN
  IF NOT EXISTS (
       SELECT 1 FROM user_rule_versions v
       WHERE v.id = NEW.rule_version_id AND v.status IN ('generated','owner_reviewed')
         AND EXISTS (SELECT 1 FROM user_rule_reviews r
                     WHERE r.id = (SELECT max(id) FROM user_rule_reviews WHERE rule_version_id = v.id)
                       AND r.verdict = 'approved' AND r.code_sha256 = v.code_sha256)) THEN
    RAISE EXCEPTION 'the code reviewer has not approved this code';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_rule_reviews_no_upd BEFORE UPDATE ON user_rule_reviews
  FOR EACH ROW EXECUTE FUNCTION hokma_trials_append_only();
CREATE TRIGGER trg_rule_reviews_no_del BEFORE DELETE ON user_rule_reviews
  FOR EACH ROW EXECUTE FUNCTION hokma_trials_append_only();
CREATE TRIGGER trg_brief_frozen BEFORE UPDATE ON user_rule_briefs
  FOR EACH ROW EXECUTE FUNCTION hokma_rule_brief_frozen();
CREATE TRIGGER trg_rule_owner_needs_agent BEFORE UPDATE OF status ON user_rule_versions
  FOR EACH ROW WHEN (NEW.status IN ('generated','owner_reviewed')) EXECUTE FUNCTION hokma_rule_owner_needs_agent();
CREATE TRIGGER trg_rule_owner_needs_agent_ins BEFORE INSERT ON user_rule_versions
  FOR EACH ROW WHEN (NEW.status IN ('generated','owner_reviewed')) EXECUTE FUNCTION hokma_rule_owner_needs_agent();
CREATE TRIGGER trg_spec_rules_need_review BEFORE INSERT ON strategy_spec_rules
  FOR EACH ROW EXECUTE FUNCTION hokma_spec_rules_need_review();
