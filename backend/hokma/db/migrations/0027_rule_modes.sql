-- Rules with a mode and a scope (SPEC-026, piece 3390a, task 3390). A control of a desk is a restriction or a notice (kept in
-- desk_limits.config.rule_modes, no column needed). A control of an ACCOUNT, or of one desk in one account, lives here:
-- account_rules (desk_id NULL = every desk that uses the account). account_day_state says whether an account is closed for the day
-- and account_day_events keeps who closed and who lifted it, when and with what loss. rule_mode_migrations is the record of the data
-- step of this version: each desk whose list of assets stopped blocking (or stayed blocking because the owner set it by hand).
-- This is version 27 because 3336 takes 26 (the layout of a desk): this migration must reach a database after 0026, never before.
-- The way back is hokma.db.downgrade.rollback_0027 (it refuses while an account rule exists, so no rule is lost without notice).

-- @engine: all

CREATE TABLE account_rules (
  id          {PK},
  account_id  INTEGER NOT NULL REFERENCES broker_accounts(id) ON DELETE RESTRICT,
  desk_id     INTEGER REFERENCES desks(id) ON DELETE CASCADE,
  control_id  TEXT NOT NULL CHECK (length(control_id) BETWEEN 1 AND 40),
  value       TEXT NOT NULL CHECK (length(value) BETWEEN 1 AND 40),
  mode        TEXT NOT NULL CHECK (mode IN ('restriction','notice')),
  on_trip     TEXT CHECK (on_trip IS NULL OR on_trip IN ('block_entries','block_and_flatten','block_and_flatten_all')),
  timeout_min INTEGER CHECK (timeout_min IS NULL OR timeout_min BETWEEN 1 AND 1440),
  source      TEXT NOT NULL DEFAULT 'hand' CHECK (source IN ('hand','chat','suggested')),
  created_at  {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX uq_account_rules_scope ON account_rules (account_id, COALESCE(desk_id, 0), control_id);

CREATE TABLE account_day_state (
  account_id INTEGER NOT NULL PRIMARY KEY REFERENCES broker_accounts(id) ON DELETE RESTRICT,
  state      TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open','day_closed')),
  closed_at  {TS},
  rule_id    TEXT,
  loss       TEXT,
  on_trip    TEXT,
  updated_at {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE account_day_events (
  id         {PK},
  account_id INTEGER NOT NULL REFERENCES broker_accounts(id) ON DELETE RESTRICT,
  kind       TEXT NOT NULL CHECK (kind IN ('closed','lifted')),
  at         {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  who        TEXT NOT NULL CHECK (length(who) BETWEEN 1 AND 80),
  rule_id    TEXT,
  loss       TEXT,
  note       TEXT
);
CREATE INDEX idx_account_day_events_account ON account_day_events (account_id, id);

CREATE TABLE rule_mode_migrations (
  id          {PK},
  desk_id     INTEGER NOT NULL,
  rule_id     TEXT NOT NULL,
  before_mode TEXT NOT NULL,
  after_mode  TEXT NOT NULL,
  reason      TEXT NOT NULL,
  migrated_at {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);
