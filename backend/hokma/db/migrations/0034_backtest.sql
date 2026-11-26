-- Backtest runs, trial ledger, trades and strategy specs (SPEC-020, H4). Additive: new tables only, nothing is altered.
-- backtest_trials is append only: the DSR of any result counts every trial, failed ones included.

-- @engine: all

CREATE TABLE strategy_specs (
  id            {PK},
  strategy_id   TEXT NOT NULL CHECK (length(strategy_id) BETWEEN 1 AND 80),
  version       INTEGER NOT NULL CHECK (version >= 1),
  line          TEXT NOT NULL CHECK (line IN ('RF','DT','SWC','SWO','LP')),
  spec_yaml     TEXT NOT NULL,
  spec_hash     TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','proposed','accepted','rejected','retired')),
  proposed_by   TEXT,
  root_cause    TEXT CHECK (root_cause IS NULL OR length(trim(root_cause)) BETWEEN 20 AND 4000),
  window_run_id INTEGER NOT NULL DEFAULT 0 CHECK (window_run_id >= 0),
  created_at    {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (strategy_id, version)
);

CREATE TABLE backtest_runs (
  id               {PK},
  kind             TEXT NOT NULL CHECK (kind IN ('run','sweep','optimize','walkforward','montecarlo','replay','calibration')),
  parent_id        INTEGER REFERENCES backtest_runs(id) ON DELETE RESTRICT,
  strategy_id      TEXT,
  strategy_version INTEGER,
  desk_id          INTEGER REFERENCES desks(id) ON DELETE SET NULL,
  spec_json        TEXT NOT NULL,
  spec_hash        TEXT NOT NULL,
  data_hash        TEXT,
  engine_version   TEXT NOT NULL,
  seed             INTEGER NOT NULL,
  status           TEXT NOT NULL CHECK (status IN ('queued','running','degraded','failed','done','account_blown')),
  summary_json     TEXT,
  result_path      TEXT,
  result_hash      TEXT,
  error            TEXT,
  started_at       {TS},
  finished_at      {TS},
  created_at       {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_backtest_runs ON backtest_runs (strategy_id, kind, status);

CREATE TABLE backtest_trials (
  id            {PK},
  strategy_id   TEXT NOT NULL,
  hypothesis_id TEXT NOT NULL,
  run_id        INTEGER REFERENCES backtest_runs(id) ON DELETE RESTRICT,
  params_json   TEXT NOT NULL,
  status        TEXT NOT NULL CHECK (status IN ('done','failed')),
  objective     REAL,
  created_at    {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_backtest_trials ON backtest_trials (strategy_id, hypothesis_id);

CREATE TABLE backtest_trades (
  id            {PK},
  run_id        INTEGER NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
  contract_id   TEXT NOT NULL,
  side          TEXT NOT NULL CHECK (side IN ('long','short')),
  qty           TEXT NOT NULL,
  entry_at      {TS} NOT NULL,
  entry_price   TEXT NOT NULL,
  exit_at       {TS},
  exit_price    TEXT,
  pnl_usd       TEXT,
  costs_usd     TEXT NOT NULL,
  mae_usd       TEXT,
  mfe_usd       TEXT,
  entry_tag     TEXT,
  exit_tag      TEXT,
  context_json  TEXT
);
CREATE INDEX idx_backtest_trades ON backtest_trades (run_id);

-- @engine: sqlite

CREATE TRIGGER trg_trials_no_upd BEFORE UPDATE ON backtest_trials BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER trg_trials_no_del BEFORE DELETE ON backtest_trials BEGIN SELECT RAISE(ABORT, 'append-only table'); END;

-- @engine: postgres

CREATE FUNCTION hokma_trials_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'append-only table';
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_trials_no_upd BEFORE UPDATE ON backtest_trials
  FOR EACH ROW EXECUTE FUNCTION hokma_trials_append_only();
CREATE TRIGGER trg_trials_no_del BEFORE DELETE ON backtest_trials
  FOR EACH ROW EXECUTE FUNCTION hokma_trials_append_only();
