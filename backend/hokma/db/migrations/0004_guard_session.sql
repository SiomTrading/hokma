-- The remembered session (task 3062): guard_events accepts guard = 'session', the row the engine writes when it
-- revokes a remembered session because an old copy of its cookie was used.

-- @engine: sqlite

-- SQLite cannot change a CHECK in place: the table is rebuilt inside the migration transaction.
-- Renaming first keeps the triggers and the index with the old table, which is dropped afterwards.
ALTER TABLE guard_events RENAME TO guard_events_old;

CREATE TABLE guard_events (
  id          {PK},
  guard       TEXT NOT NULL CHECK (guard IN ('broker','secrets','permission','protect_kit','approval','transition',
              'delete','truncate','hokma_internal','force_push','outside_workdir','real_money','session')),
  decision    TEXT NOT NULL CHECK (decision IN ('blocked','asked','allowed_by_human','denied_by_human')),
  agent_slug  TEXT REFERENCES agents(slug),
  tool        TEXT,
  task_id     INTEGER REFERENCES tasks(id),
  reason      TEXT NOT NULL CHECK (length(reason) <= 1000),
  created_at  {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO guard_events (id, guard, decision, agent_slug, tool, task_id, reason, created_at)
  SELECT id, guard, decision, agent_slug, tool, task_id, reason, created_at FROM guard_events_old;

DROP TABLE guard_events_old;

CREATE INDEX idx_guard_events_created ON guard_events(created_at);
CREATE TRIGGER trg_guard_no_upd BEFORE UPDATE ON guard_events BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER trg_guard_no_del BEFORE DELETE ON guard_events BEGIN SELECT RAISE(ABORT, 'append-only table'); END;

-- @engine: postgres

ALTER TABLE guard_events DROP CONSTRAINT IF EXISTS guard_events_guard_check;
ALTER TABLE guard_events ADD CONSTRAINT guard_events_guard_check CHECK (guard IN ('broker','secrets','permission',
  'protect_kit','approval','transition','delete','truncate','hokma_internal','force_push','outside_workdir',
  'real_money','session'));
