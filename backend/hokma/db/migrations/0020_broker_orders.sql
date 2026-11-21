-- Hokma 1.0.9 (task 3299, SPEC-015 18.2 and 18.6): orders with Schwab. Additive: three new tables, nothing is altered, so a
-- 1.0.8 database migrates without losing a row.
--   broker_orders           one row per attempt (preview, place, replace, cancel), also the refused ones and the ones that ask
--                           the owner. request_json and response_json arrive with the account numbers already masked
--                           (account_ref and mask); a hash or a plain number never reaches this table.
--   broker_external_accounts  the link of a local broker account with an account of an outside broker: a fingerprint (HMAC of the
--                           broker's own identifier with a local key kept in the system credential store) and the mask.
--                           The identifier itself is never stored.
--   desk_limits             the optional limits of a desk (18.5), a JSON object; a desk without a row has no limit.

-- @engine: all

CREATE TABLE broker_orders (
  id               {PK},
  desk_id          INTEGER REFERENCES desks(id) ON DELETE SET NULL,
  account_id       INTEGER REFERENCES broker_accounts(id) ON DELETE RESTRICT,
  broker_kind      TEXT NOT NULL CHECK (length(broker_kind) BETWEEN 1 AND 40),
  action           TEXT NOT NULL CHECK (action IN ('preview','place','replace','cancel')),
  agent_slug       TEXT CHECK (agent_slug IS NULL OR length(agent_slug) BETWEEN 1 AND 80),
  conversation_id  INTEGER,
  task_id          INTEGER,
  reason           TEXT CHECK (reason IS NULL OR length(reason) <= 2000),
  request_json     TEXT,
  rule_result      TEXT CHECK (rule_result IS NULL OR rule_result IN ('auto','needs_owner','refused')),
  rule_why         TEXT CHECK (rule_why IS NULL OR length(rule_why) <= 4000),
  decision_id      INTEGER REFERENCES decisions(id) ON DELETE SET NULL,
  approved_by      TEXT CHECK (approved_by IS NULL OR approved_by IN ('auto','owner')),
  http_status      INTEGER CHECK (http_status IS NULL OR http_status BETWEEN 100 AND 599),
  broker_order_id  TEXT CHECK (broker_order_id IS NULL OR length(broker_order_id) BETWEEN 1 AND 64),
  replaces_order_id TEXT CHECK (replaces_order_id IS NULL OR length(replaces_order_id) BETWEEN 1 AND 64),
  response_json    TEXT,
  correl_id        TEXT CHECK (correl_id IS NULL OR length(correl_id) <= 128),
  created_at       {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_broker_orders_desk    ON broker_orders (desk_id, id);
CREATE INDEX idx_broker_orders_account ON broker_orders (account_id, id);

CREATE TABLE broker_external_accounts (
  account_id  INTEGER NOT NULL PRIMARY KEY REFERENCES broker_accounts(id) ON DELETE RESTRICT,
  fingerprint TEXT NOT NULL UNIQUE CHECK (length(fingerprint) BETWEEN 16 AND 128),
  mask        TEXT NOT NULL CHECK (length(mask) BETWEEN 1 AND 16)
);

CREATE TABLE desk_limits (
  desk_id INTEGER NOT NULL PRIMARY KEY REFERENCES desks(id) ON DELETE CASCADE,
  config  TEXT NOT NULL DEFAULT '{}'
);
