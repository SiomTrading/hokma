-- Execution, contracts, calendar and back office (SPEC-018, H2). Additive: new tables only, nothing is altered.
-- An order leaves Hokma only through hokma.exec; every attempt is still a row of broker_orders (0020).
-- Nothing here is ever deleted: a row is closed by its status.

-- @engine: all

CREATE TABLE desk_profile (
  desk_id     INTEGER NOT NULL PRIMARY KEY REFERENCES desks(id) ON DELETE CASCADE,
  line        TEXT NOT NULL CHECK (line IN ('RF','DT','SWC','SWO','LP')),
  variant     TEXT CHECK (variant IS NULL OR length(variant) BETWEEN 1 AND 40),
  benchmark   TEXT CHECK (benchmark IS NULL OR length(benchmark) BETWEEN 1 AND 40),
  updated_at  {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE desk_autonomy (
  desk_id     INTEGER NOT NULL PRIMARY KEY REFERENCES desks(id) ON DELETE CASCADE,
  level       INTEGER NOT NULL DEFAULT 1 CHECK (level IN (1,2,3)),
  rules_json  TEXT,
  approved_by TEXT CHECK (approved_by IS NULL OR approved_by = 'owner'),
  approved_at {TS},
  updated_at  {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (level < 3 OR (rules_json IS NOT NULL AND approved_by = 'owner'))
);

CREATE TABLE exec_proposals (
  id               {PK},
  desk_id          INTEGER NOT NULL REFERENCES desks(id) ON DELETE RESTRICT,
  line             TEXT NOT NULL CHECK (line IN ('RF','DT','SWC','SWO','LP')),
  origin           TEXT NOT NULL CHECK (origin IN ('desk','chat','rule')),
  level            INTEGER NOT NULL CHECK (level IN (1,2,3)),
  proposal_json    TEXT NOT NULL,
  fingerprint      TEXT NOT NULL CHECK (length(fingerprint) = 64),
  status           TEXT NOT NULL CHECK (status IN ('analysis_only','refused','awaiting_owner','sent','filled',
                                                    'partially_filled','rejected_by_broker','expired','canceled')),
  code             TEXT CHECK (code IS NULL OR length(code) <= 64),
  prefilter_json   TEXT,
  clamps_json      TEXT,
  decision_id      INTEGER REFERENCES decisions(id) ON DELETE SET NULL,
  broker_order_record_id INTEGER REFERENCES broker_orders(id) ON DELETE RESTRICT,
  approved_fingerprint TEXT CHECK (approved_fingerprint IS NULL OR length(approved_fingerprint) = 64),
  approved_by      TEXT CHECK (approved_by IS NULL OR approved_by = 'owner' OR approved_by LIKE 'rule:%'),
  created_by       TEXT NOT NULL CHECK (length(created_by) BETWEEN 1 AND 80),
  created_at       {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at       {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (status NOT IN ('sent','filled','partially_filled') OR approved_fingerprint = fingerprint OR origin = 'chat')
);
CREATE INDEX idx_exec_proposals_desk ON exec_proposals (desk_id, id);

CREATE TABLE exec_guard_state (
  desk_id     INTEGER NOT NULL REFERENCES desks(id) ON DELETE CASCADE,
  guard       TEXT NOT NULL CHECK (length(guard) BETWEEN 1 AND 40),
  key         TEXT NOT NULL CHECK (length(key) BETWEEN 1 AND 80),
  state_json  TEXT NOT NULL,
  updated_at  {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (desk_id, guard, key)
);

CREATE TABLE exec_alerts (
  id           {PK},
  desk_id      INTEGER NOT NULL REFERENCES desks(id) ON DELETE CASCADE,
  rule_json    TEXT NOT NULL,
  armed        INTEGER NOT NULL DEFAULT 1 CHECK (armed IN (0,1)),
  last_fired_at {TS},
  created_at   {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE sim_orders (
  id           {PK},
  account_id   INTEGER NOT NULL REFERENCES broker_accounts(id) ON DELETE RESTRICT,
  order_ref    TEXT NOT NULL UNIQUE CHECK (length(order_ref) BETWEEN 1 AND 64),
  request_json TEXT NOT NULL,
  status       TEXT NOT NULL CHECK (status IN ('working','filled','partially_filled','canceled','rejected','expired')),
  filled_qty   TEXT NOT NULL DEFAULT '0',
  avg_price    TEXT,
  reason       TEXT CHECK (reason IS NULL OR length(reason) <= 200),
  created_at   {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE sim_fills (
  id           {PK},
  sim_order_id INTEGER NOT NULL REFERENCES sim_orders(id) ON DELETE RESTRICT,
  contract_id  TEXT NOT NULL,
  side         TEXT NOT NULL CHECK (side IN ('buy','sell')),
  qty          TEXT NOT NULL,
  price        TEXT NOT NULL,
  commission   TEXT NOT NULL,
  multiplier   TEXT NOT NULL,
  quote_json   TEXT NOT NULL,
  filled_at    {TS} NOT NULL
);

CREATE TABLE book_entries (
  id           {PK},
  account_id   INTEGER NOT NULL REFERENCES broker_accounts(id) ON DELETE RESTRICT,
  desk_id      INTEGER REFERENCES desks(id) ON DELETE SET NULL,
  kind         TEXT NOT NULL CHECK (kind IN ('fill','commission','fee','deposit','withdrawal','dividend','interest',
                                             'accrual','adjustment')),
  contract_id  TEXT,
  qty          TEXT,
  price        TEXT,
  multiplier   TEXT,
  amount       TEXT NOT NULL,
  currency     TEXT NOT NULL DEFAULT 'USD' CHECK (currency IN ('USD')),
  source       TEXT NOT NULL CHECK (source IN ('sim','schwab','alpaca','ibkr','crypto','import','owner')),
  source_ref   TEXT NOT NULL CHECK (length(source_ref) BETWEEN 1 AND 128),
  occurred_at  {TS} NOT NULL,
  recorded_at  {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  note         TEXT CHECK (note IS NULL OR length(note) <= 400),
  UNIQUE (source, source_ref, kind)
);
CREATE INDEX idx_book_account ON book_entries (account_id, occurred_at);

CREATE TABLE contracts (
  contract_id  TEXT NOT NULL PRIMARY KEY CHECK (length(contract_id) BETWEEN 3 AND 80),
  asset_type   TEXT NOT NULL CHECK (asset_type IN ('equity','etf','future','option','future_option','cfd','crypto','bond','bill')),
  root         TEXT, underlying TEXT,
  multiplier   TEXT NOT NULL,
  tick_size    TEXT, tick_value TEXT,
  expiration   TEXT, strike TEXT,
  put_call     TEXT CHECK (put_call IS NULL OR put_call IN ('C','P')),
  currency     TEXT NOT NULL DEFAULT 'USD' CHECK (currency IN ('USD')),
  source       TEXT NOT NULL,
  as_of        {TS} NOT NULL
);

CREATE TABLE contract_aliases (
  provider     TEXT NOT NULL CHECK (length(provider) BETWEEN 1 AND 40),
  alias        TEXT NOT NULL CHECK (length(alias) BETWEEN 1 AND 80),
  contract_id  TEXT NOT NULL REFERENCES contracts(contract_id) ON DELETE RESTRICT,
  PRIMARY KEY (provider, alias)
);

CREATE TABLE econ_events (
  event_id     TEXT NOT NULL PRIMARY KEY,
  source       TEXT NOT NULL CHECK (source IN ('forexfactory','finnhub')),
  name         TEXT NOT NULL, country TEXT NOT NULL, impact TEXT,
  scheduled_at {TS} NOT NULL,
  actual REAL, consensus REAL, previous REAL, revised REAL,
  received_at  {TS} NOT NULL
);
CREATE INDEX idx_econ_events_time ON econ_events (scheduled_at);

CREATE TABLE run_records (
  id           {PK},
  desk_id      INTEGER REFERENCES desks(id) ON DELETE SET NULL,
  kind         TEXT NOT NULL CHECK (length(kind) BETWEEN 1 AND 40),
  signature    TEXT NOT NULL,
  status       TEXT NOT NULL CHECK (status IN ('running','done','failed','interrupted')),
  record_json  TEXT NOT NULL,
  started_at   {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  ended_at     {TS}
);

CREATE TABLE run_checkpoints (
  run_id       INTEGER NOT NULL REFERENCES run_records(id) ON DELETE RESTRICT,
  node         TEXT NOT NULL,
  signature    TEXT NOT NULL,
  state_json   TEXT NOT NULL,
  created_at   {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (run_id, node)
);

CREATE TABLE reconciliations (
  id           {PK},
  account_id   INTEGER NOT NULL REFERENCES broker_accounts(id) ON DELETE RESTRICT,
  as_of        {TS} NOT NULL,
  status       TEXT NOT NULL CHECK (status IN ('clean','breaks','failed')),
  summary_json TEXT NOT NULL,
  created_at   {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE recon_breaks (
  id           {PK},
  recon_id     INTEGER NOT NULL REFERENCES reconciliations(id) ON DELETE RESTRICT,
  bucket       TEXT NOT NULL CHECK (bucket IN ('quantity','amount','timing','one_side')),
  key          TEXT NOT NULL,
  local_json   TEXT, broker_json TEXT,
  diff         TEXT,
  cause        TEXT CHECK (cause IS NULL OR cause IN ('timing','currency','mapping','duplicate_or_missing','commission','data_quality')),
  explanation  TEXT, owner TEXT, due_at {TS},
  action       TEXT CHECK (action IS NULL OR action IN ('watch','adjust','ticket','suppress')),
  task_id      INTEGER REFERENCES tasks(id) ON DELETE SET NULL
);
