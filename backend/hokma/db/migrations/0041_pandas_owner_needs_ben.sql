-- 1.3.0 (task 3433): a strategy of the new flow (written in pandas, manifest "flow":"pandas") is available for backtest and optimizer as soon as the checks of Hokma pass (they leave an approved
-- review signed by hokma-checks), but it cannot become owner_reviewed (the only state that paper and real money accept) unless the LAST review is an approval of the same code by a reviewer
-- that is not hokma-checks: Ben. The base refuses it, whoever asks. Additive: triggers only.

-- @engine: sqlite

CREATE TRIGGER trg_pandas_owner_needs_ben BEFORE UPDATE OF status ON user_rule_versions
  WHEN NEW.status = 'owner_reviewed' AND NEW.manifest_json LIKE '%"flow":"pandas"%' AND NOT EXISTS (
    SELECT 1 FROM user_rule_reviews r
    WHERE r.id = (SELECT max(id) FROM user_rule_reviews WHERE rule_version_id = NEW.id)
      AND r.verdict = 'approved' AND r.code_sha256 = NEW.code_sha256 AND r.reviewer <> 'hokma-checks')
  BEGIN SELECT RAISE(ABORT, 'the reviewer has not approved this strategy: only the checks of Hokma did'); END;

-- @engine: postgres

CREATE FUNCTION hokma_pandas_owner_needs_ben() RETURNS trigger AS $$
BEGIN
  IF NEW.status = 'owner_reviewed' AND NEW.manifest_json LIKE '%"flow":"pandas"%' AND NOT EXISTS (
       SELECT 1 FROM user_rule_reviews r
       WHERE r.id = (SELECT max(id) FROM user_rule_reviews WHERE rule_version_id = NEW.id)
         AND r.verdict = 'approved' AND r.code_sha256 = NEW.code_sha256 AND r.reviewer <> 'hokma-checks') THEN
    RAISE EXCEPTION 'the reviewer has not approved this strategy: only the checks of Hokma did';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_pandas_owner_needs_ben BEFORE UPDATE OF status ON user_rule_versions
  FOR EACH ROW WHEN (NEW.status = 'owner_reviewed') EXECUTE FUNCTION hokma_pandas_owner_needs_ben();
