-- Money desks (task 3284, SPEC-014 P1): a desk is a project (kind 'strategy') plus its team, assets and connections.
-- Additive: only creates tables, never inserts and never touches an existing table, so 0.0.39 data is unchanged.
-- Plain CREATE TABLE (no IF NOT EXISTS) on purpose: hokma.db.transfer reads the tables from these statements, and
-- re-applying is already a no-op because schema_version records the file.
-- `desks.mode` is locked to 'simulation' by a CHECK: real accounts will live in their own table, not here.
-- Archiving is `status = 'archived'`: there is no delete route, and a project cannot be removed while it has a desk.

-- @engine: all

CREATE TABLE desks (
  id                 {PK},
  project_id         INTEGER NOT NULL UNIQUE REFERENCES projects(id) ON DELETE RESTRICT,
  name               TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  focus              TEXT CHECK (focus IS NULL OR length(focus) <= 120),
  template           TEXT NOT NULL DEFAULT 'standard' CHECK (template IN ('minimal','standard','debate','custom')),
  mode               TEXT NOT NULL DEFAULT 'simulation' CHECK (mode = 'simulation'),
  capital            INTEGER NOT NULL CHECK (capital BETWEEN 100 AND 1000000000),
  currency           TEXT NOT NULL DEFAULT 'USD' CHECK (currency IN ('USD')),
  objective_pct      REAL CHECK (objective_pct IS NULL OR objective_pct BETWEEN 0 AND 100),
  mandate            TEXT CHECK (mandate IS NULL OR length(mandate) <= 4000),
  token_budget       INTEGER CHECK (token_budget IS NULL OR token_budget > 0),
  budget_alert_week  TEXT,
  budget_alert_level INTEGER CHECK (budget_alert_level IS NULL OR budget_alert_level IN (80,100)),
  status             TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','archived')),
  sort_order         INTEGER NOT NULL DEFAULT 0,
  created_at         {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at         {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE desk_agents (
  desk_id     INTEGER NOT NULL REFERENCES desks(id) ON DELETE CASCADE,
  agent_slug  TEXT NOT NULL REFERENCES agents(slug),
  role        TEXT NOT NULL CHECK (role IN ('chief','data','risk','research','signals','execution','reviewer',
                                            'bull','bear','judge','strategy_dev','custom')),
  model       TEXT NOT NULL CHECK (model IN ('haiku','sonnet','opus')),
  active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
  added_at    {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (desk_id, agent_slug)
);

CREATE UNIQUE INDEX uq_desk_one_chief ON desk_agents (desk_id) WHERE role = 'chief';

CREATE TABLE desk_assets (
  desk_id    INTEGER NOT NULL REFERENCES desks(id) ON DELETE CASCADE,
  symbol     TEXT NOT NULL CHECK (length(symbol) BETWEEN 1 AND 40),
  note       TEXT CHECK (note IS NULL OR length(note) <= 200),
  sort_order INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (desk_id, symbol)
);

CREATE TABLE desk_connections (
  desk_id     INTEGER NOT NULL REFERENCES desks(id) ON DELETE CASCADE,
  provider_id TEXT NOT NULL,
  enabled     INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
  PRIMARY KEY (desk_id, provider_id)
);

CREATE INDEX idx_desks_status ON desks (status, sort_order);
