-- Desk plantel, investor signals, typed mandates and the premium selling switch (SPEC-019, H3). Additive: new tables only.
-- premium_thresholds stays empty in H3: only the backtest service of H4 writes it (record_calibration).

-- @engine: all

CREATE TABLE desk_seats (
  desk_id     INTEGER NOT NULL REFERENCES desks(id) ON DELETE CASCADE,
  seat_code   TEXT NOT NULL CHECK (seat_code IN ('A01','A02','A03','A04','A05','A06','A07','A08','A09','A10','A11','A12',
                                                  'A13','A14','A15','A16','A17','A18','A19','A20','A21','A22','A23')),
  agent_slug  TEXT NOT NULL,
  line        TEXT NOT NULL CHECK (line IN ('RF','DT','SWC','SWO','LP')),
  variant     TEXT CHECK (variant IS NULL OR length(variant) BETWEEN 1 AND 40),
  active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
  added_at    {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (desk_id, seat_code),
  FOREIGN KEY (desk_id, agent_slug) REFERENCES desk_agents(desk_id, agent_slug) ON DELETE CASCADE
);

CREATE TABLE desk_signals (
  id            {PK},
  desk_id       INTEGER NOT NULL REFERENCES desks(id) ON DELETE RESTRICT,
  run_id        INTEGER REFERENCES run_records(id) ON DELETE SET NULL,
  seat_code     TEXT NOT NULL,
  symbol        TEXT NOT NULL CHECK (length(symbol) BETWEEN 1 AND 40),
  as_of         {TS} NOT NULL,
  value         REAL CHECK (value IS NULL OR value BETWEEN -1 AND 1),
  confidence    INTEGER NOT NULL CHECK (confidence BETWEEN 0 AND 100),
  abstained     INTEGER NOT NULL CHECK (abstained IN (0,1)),
  signal_json   TEXT NOT NULL,
  snapshot_hash TEXT NOT NULL,
  created_at    {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK ((abstained = 1) = (value IS NULL))
);
CREATE INDEX idx_desk_signals ON desk_signals (desk_id, seat_code, as_of);

CREATE TABLE desk_mandates (
  desk_id     INTEGER NOT NULL PRIMARY KEY REFERENCES desks(id) ON DELETE CASCADE,
  spec_json   TEXT NOT NULL,
  version     INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
  approved_by TEXT CHECK (approved_by IS NULL OR approved_by = 'owner'),
  updated_at  {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE premium_thresholds (
  id               {PK},
  desk_id          INTEGER NOT NULL REFERENCES desks(id) ON DELETE RESTRICT,
  underlying       TEXT NOT NULL CHECK (length(underlying) BETWEEN 1 AND 40),
  metric           TEXT NOT NULL CHECK (length(metric) BETWEEN 1 AND 40),
  threshold        TEXT NOT NULL,
  evidence_json    TEXT NOT NULL,
  backtest_run_ref TEXT NOT NULL CHECK (length(backtest_run_ref) BETWEEN 1 AND 128),
  status           TEXT NOT NULL DEFAULT 'calibrated' CHECK (status IN ('calibrated','retired')),
  calibrated_at    {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  retired_at       {TS}
);
CREATE INDEX idx_premium_thresholds ON premium_thresholds (desk_id, underlying, status);
