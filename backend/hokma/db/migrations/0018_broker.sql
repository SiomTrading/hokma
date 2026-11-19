-- Brokers and their accounts (task 3294, class 7, C7-5a): the contract of hokma.broker lives on these tables.
-- Additive: only new tables; the seed creates the simulated broker (hokma.db.seeds), so 1.0.4 data is unchanged.
-- `account_ledger` only knows 'open' and 'reset' in class 7; orders and fills widen the CHECK in class 9.
-- A desk has at most one account (desk_accounts.desk_id is the key) and an account serves at most one desk.

-- @engine: all

CREATE TABLE brokers (
  id         {PK},
  kind       TEXT NOT NULL CHECK (length(kind) BETWEEN 1 AND 40),
  name       TEXT NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 60),
  config     TEXT NOT NULL DEFAULT '{}',
  created_at {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE broker_accounts (
  id              {PK},
  broker_id       INTEGER NOT NULL REFERENCES brokers(id) ON DELETE RESTRICT,
  name            TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 60),
  initial_capital INTEGER NOT NULL CHECK (initial_capital BETWEEN 100 AND 1000000000),
  currency        TEXT NOT NULL DEFAULT 'USD' CHECK (currency IN ('USD')),
  status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
  created_at      {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (broker_id, name)
);

CREATE TABLE account_ledger (
  id         {PK},
  account_id INTEGER NOT NULL REFERENCES broker_accounts(id) ON DELETE CASCADE,
  kind       TEXT NOT NULL CHECK (kind IN ('open','reset')),
  amount     REAL NOT NULL,
  created_at {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE desk_accounts (
  desk_id    INTEGER NOT NULL PRIMARY KEY REFERENCES desks(id) ON DELETE CASCADE,
  account_id INTEGER NOT NULL UNIQUE REFERENCES broker_accounts(id) ON DELETE RESTRICT
);

CREATE INDEX idx_account_ledger_account ON account_ledger (account_id, id);
