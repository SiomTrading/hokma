-- Outgoing Telegram messages to the owner (task 3215, SPEC-011 P17, Hokma 0.0.31): every message enters here and a
-- sender retries each minute until Telegram accepts it (sent_at). Additive: version 0.0.30 ignores the table.

-- @engine: all

CREATE TABLE telegram_outbox (
  id         {PK},
  text       TEXT NOT NULL,
  created_at {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  sent_at    {TS},
  attempts   INTEGER NOT NULL DEFAULT 0,
  last_error TEXT
);
CREATE INDEX telegram_outbox_pending ON telegram_outbox (sent_at, id);
