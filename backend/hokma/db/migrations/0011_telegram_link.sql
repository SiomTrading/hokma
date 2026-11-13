-- The Telegram link of the student (task 3215, SPEC-011 P7, Hokma 0.0.31): one row at most. The bot token is NOT here;
-- it lives in the system credential store. Additive: version 0.0.30 ignores the table.

-- @engine: all

CREATE TABLE telegram_link (
  id              INTEGER PRIMARY KEY CHECK (id = 1),
  chat_id         TEXT NOT NULL,
  user_name       TEXT,
  conversation_id TEXT,
  linked_at       {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);
