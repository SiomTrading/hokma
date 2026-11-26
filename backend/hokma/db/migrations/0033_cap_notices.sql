-- The notice with buttons when a rule in NOTICE mode reaches its cap with no order in the way (SPEC-026 v2, piece 3390e4, task 3390). Until now the owner got a text with
-- no answer to give; now the monitor opens one row here, the chat shows its card and Telegram its three buttons (Keep trading today, Block entries today, Close positions),
-- and the row keeps what was measured, the one-use token of the Telegram buttons (rotated at every step), the deadline of the rule and what the owner answered.
-- A notice nobody answers expires and changes nothing: it only stays written here. "Keep trading today" also writes the day permission in notice_grants (0029),
-- so the notice of an order of the same rule does not ask again that session. The way back is hokma.db.downgrade.rollback_0033.
-- This is version 33: 0032 is the one before it.

-- @engine: all

CREATE TABLE cap_notices (
  id              {PK},
  account_id      INTEGER NOT NULL REFERENCES broker_accounts(id) ON DELETE CASCADE,
  rule_id         TEXT NOT NULL CHECK (length(rule_id) BETWEEN 1 AND 60),
  control         TEXT NOT NULL,
  metric          TEXT NOT NULL CHECK (metric IN ('daily_loss', 'drawdown')),
  scope           TEXT NOT NULL CHECK (scope IN ('account', 'desk_account', 'desk')),
  desk_id         INTEGER REFERENCES desks(id) ON DELETE SET NULL,
  on_trip         TEXT,
  session_key     TEXT NOT NULL,
  session_ends_at TEXT NOT NULL,
  value           TEXT NOT NULL,
  cap             TEXT NOT NULL,
  read_at         TEXT,
  timeout_min     INTEGER NOT NULL,
  nonce           TEXT CHECK (nonce IS NULL OR length(nonce) = 12),
  stage           TEXT NOT NULL DEFAULT 'ask' CHECK (stage IN ('ask', 'close')),
  close_plan      TEXT,
  status          TEXT NOT NULL DEFAULT 'waiting' CHECK (status IN ('waiting', 'followed', 'blocked', 'closed', 'expired')),
  outcome         TEXT,
  created_at      {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at      {TS} NOT NULL,
  answered_at     {TS},
  answered_by     TEXT CHECK (answered_by IS NULL OR answered_by IN ('owner', 'hokma'))
);
CREATE INDEX idx_cap_notices_session ON cap_notices (account_id, rule_id, session_key);
CREATE INDEX idx_cap_notices_waiting ON cap_notices (status, expires_at);
