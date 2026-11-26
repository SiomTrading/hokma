-- Verdict of the optimizer (SPEC-032): editable thresholds by line, owner answers, verdicts and library requests.
-- Additive: new tables only. audit_threshold_sets, audit_answers and strategy_verdicts are append only.

-- @engine: all

CREATE TABLE audit_threshold_sets (
  id           {PK},
  line         TEXT NOT NULL CHECK (line IN ('DT','SWC','SWO','LP','RF')),
  version      INTEGER NOT NULL CHECK (version >= 1),
  values_json  TEXT NOT NULL,                 -- the measures of section 6, by key
  values_hash  TEXT NOT NULL,
  changed_by   TEXT NOT NULL,                 -- 'owner' or 'system:seed', set by the code
  reason       TEXT CHECK (reason IS NULL OR length(reason) <= 500),
  restored_from INTEGER REFERENCES audit_threshold_sets(id) ON DELETE RESTRICT,
  created_at   {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (line, version)
);

CREATE TABLE audit_answers (
  id            {PK},
  scope         TEXT NOT NULL CHECK (scope IN ('strategy','data')),
  subject       TEXT NOT NULL CHECK (length(subject) BETWEEN 1 AND 200),   -- strategy: "{strategy_id}@{version}"; data: the contract id
  question_key  TEXT NOT NULL CHECK (length(question_key) BETWEEN 1 AND 40),
  answer        TEXT NOT NULL CHECK (answer IN ('yes','no','na')),
  note          TEXT CHECK (note IS NULL OR length(note) <= 1000),
  answered_by   TEXT NOT NULL,                -- 'owner', set by the code
  created_at    {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_audit_answers ON audit_answers (scope, subject, question_key, id);

CREATE TABLE strategy_verdicts (
  id                {PK},
  strategy_id       TEXT NOT NULL,
  strategy_version  INTEGER NOT NULL CHECK (strategy_version >= 1),
  line              TEXT NOT NULL,
  parent_id         INTEGER REFERENCES strategy_verdicts(id) ON DELETE RESTRICT,   -- the verdict it recomputes
  cause             TEXT NOT NULL CHECK (cause IN ('evaluate','exclude','include','thresholds','answers')),
  run_id            INTEGER NOT NULL REFERENCES backtest_runs(id) ON DELETE RESTRICT,
  walkforward_id    INTEGER REFERENCES backtest_runs(id) ON DELETE RESTRICT,
  optimize_id       INTEGER REFERENCES backtest_runs(id) ON DELETE RESTRICT,
  montecarlo_id     INTEGER REFERENCES backtest_runs(id) ON DELETE RESTRICT,
  thresholds_id     INTEGER NOT NULL REFERENCES audit_threshold_sets(id) ON DELETE RESTRICT,
  answer_ids_json   TEXT NOT NULL DEFAULT '[]',
  excluded_json     TEXT NOT NULL DEFAULT '[]',     -- check keys the owner took out, with the reason of each
  evidence_json     TEXT NOT NULL,                  -- the AuditEvidence used, so a recompute never reruns a backtest
  audit_verdict     TEXT NOT NULL CHECK (audit_verdict IN ('go','review','needs_evidence','no_go')),   -- before exclusions
  verdict           TEXT NOT NULL CHECK (verdict IN ('go','review','needs_evidence','no_go')),         -- after exclusions
  report_json       TEXT NOT NULL,                  -- AuditReport.to_dict()
  sentences_json    TEXT NOT NULL,                  -- three sentences as message keys and parameters
  suggestions_json  TEXT NOT NULL DEFAULT '[]',
  ideal_json        TEXT NOT NULL DEFAULT '{}',
  inputs_hash       TEXT NOT NULL,
  actor             TEXT NOT NULL,                  -- 'owner' or the agent slug, set by the code
  created_at        {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_strategy_verdicts ON strategy_verdicts (strategy_id, strategy_version, id);

CREATE TABLE library_requests (
  id               {PK},
  name             TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 48),
  description      TEXT NOT NULL CHECK (length(trim(description)) BETWEEN 20 AND 2000),
  fields_json      TEXT NOT NULL DEFAULT '[]',     -- bar fields it reads: open, high, low, close, volume
  params_json      TEXT NOT NULL DEFAULT '{}',
  bank_version_id  INTEGER,                         -- strategy_bank_versions.id of SPEC-030 when present; no foreign key
  status           TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','wired','built','user_rule','declined')),
  resolution       TEXT CHECK (resolution IS NULL OR length(resolution) <= 2000),
  requested_by     TEXT NOT NULL,
  created_at       {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at       {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK ((status = 'open') = (resolution IS NULL))
);
CREATE INDEX idx_library_requests ON library_requests (status, name);

-- Seed: version 1 of DT, SWC and SWO with the values of section 6 (one row per line, changed_by 'system:seed').
-- values_json is the canonical form (sorted keys, no spaces) and values_hash its sha256, as thresholds.save writes them.
INSERT INTO audit_threshold_sets (line, version, values_json, values_hash, changed_by)
VALUES ('DT', 1, '{"edge_warns":true,"max_cost_share":0.33,"max_neighbor_drop":0.3,"max_top_trade_share":0.25,"min_dsr":0.95,"min_oos_to_is":0.5,"min_regime_share":0.5,"min_trades":200,"min_years":1.0,"neighbor_spread":0.1,"pbo_reject":0.5,"pbo_warn":0.2,"red_flag_sharpe":3.0}', '481035708dccca6d3ef77916bc3775f147d4f77f9ca6dec0e87dd18fcea45c26', 'system:seed');
INSERT INTO audit_threshold_sets (line, version, values_json, values_hash, changed_by)
VALUES ('SWC', 1, '{"edge_warns":true,"max_cost_share":0.33,"max_neighbor_drop":0.3,"max_top_trade_share":0.25,"min_dsr":0.95,"min_oos_to_is":0.5,"min_regime_share":0.5,"min_trades":100,"min_years":3.0,"neighbor_spread":0.1,"pbo_reject":0.5,"pbo_warn":0.2,"red_flag_sharpe":3.0}', 'cc611ffa769ad63b02c68be1388187ce633811d0489ff6755a389c7790aa1bf2', 'system:seed');
INSERT INTO audit_threshold_sets (line, version, values_json, values_hash, changed_by)
VALUES ('SWO', 1, '{"edge_warns":true,"max_cost_share":0.33,"max_neighbor_drop":0.3,"max_top_trade_share":0.25,"min_dsr":0.95,"min_oos_to_is":0.5,"min_regime_share":0.5,"min_trades":50,"min_years":3.0,"neighbor_spread":0.1,"pbo_reject":0.5,"pbo_warn":0.2,"red_flag_sharpe":3.0}', 'ba82f024ccc31247245554f16c9f345627d592c2445875e9388611133097af37', 'system:seed');

-- @engine: sqlite

CREATE TRIGGER trg_thresholds_no_upd BEFORE UPDATE ON audit_threshold_sets BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER trg_thresholds_no_del BEFORE DELETE ON audit_threshold_sets BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER trg_answers_no_upd BEFORE UPDATE ON audit_answers BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER trg_answers_no_del BEFORE DELETE ON audit_answers BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER trg_verdicts_no_upd BEFORE UPDATE ON strategy_verdicts BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER trg_verdicts_no_del BEFORE DELETE ON strategy_verdicts BEGIN SELECT RAISE(ABORT, 'append-only table'); END;

-- @engine: postgres

CREATE TRIGGER trg_thresholds_no_upd BEFORE UPDATE ON audit_threshold_sets
  FOR EACH ROW EXECUTE FUNCTION hokma_trials_append_only();
CREATE TRIGGER trg_thresholds_no_del BEFORE DELETE ON audit_threshold_sets
  FOR EACH ROW EXECUTE FUNCTION hokma_trials_append_only();
CREATE TRIGGER trg_answers_no_upd BEFORE UPDATE ON audit_answers
  FOR EACH ROW EXECUTE FUNCTION hokma_trials_append_only();
CREATE TRIGGER trg_answers_no_del BEFORE DELETE ON audit_answers
  FOR EACH ROW EXECUTE FUNCTION hokma_trials_append_only();
CREATE TRIGGER trg_verdicts_no_upd BEFORE UPDATE ON strategy_verdicts
  FOR EACH ROW EXECUTE FUNCTION hokma_trials_append_only();
CREATE TRIGGER trg_verdicts_no_del BEFORE DELETE ON strategy_verdicts
  FOR EACH ROW EXECUTE FUNCTION hokma_trials_append_only();
