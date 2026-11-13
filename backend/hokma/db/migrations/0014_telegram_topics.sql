-- Telegram group with topics (task 3218, SPEC-012 P3, Hokma 0.0.32): the linked group and its Avisos topic, the topic
-- of each queued message (NULL = Avisos) and one conversation per (chat, topic). Additive: 0.0.31 ignores all of it.

-- @engine: all

ALTER TABLE telegram_link ADD COLUMN group_id TEXT;
ALTER TABLE telegram_link ADD COLUMN avisos_thread_id INTEGER;
ALTER TABLE telegram_link ADD COLUMN avisos_group_id TEXT;
ALTER TABLE telegram_link ADD COLUMN avisos TEXT;
ALTER TABLE telegram_outbox ADD COLUMN thread_id INTEGER;

CREATE TABLE telegram_session (
  chat_id         TEXT NOT NULL,
  thread_id       INTEGER NOT NULL DEFAULT 0,
  conversation_id TEXT NOT NULL,
  topic_name      TEXT,
  updated_at      INTEGER NOT NULL,
  PRIMARY KEY (chat_id, thread_id)
);
