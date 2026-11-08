-- Permission mode, the always-on protections and the Hokma trash.
-- profile gets the permission mode, the one-time notice flag and the last update check time.
-- guard_events accepts the rules of the always-on protections.
-- trash_items records what the agent moved to the trash instead of deleting it (and the folder it came from).
-- checkpoint_points records the commit of every restore point that Hokma took.
-- A database that already existed keeps 'ask' (the default of the new column); a new one gets 'auto' when the
-- first-run "Ready" step completes.

-- @engine: all

ALTER TABLE profile ADD COLUMN permission_mode TEXT NOT NULL DEFAULT 'ask' CHECK (permission_mode IN ('ask','auto'));
ALTER TABLE profile ADD COLUMN permission_mode_intro_seen INTEGER NOT NULL DEFAULT 0 CHECK (permission_mode_intro_seen IN (0,1));
ALTER TABLE profile ADD COLUMN update_checked_at {TS};

CREATE TABLE trash_items (
  id               {PK},
  original_path    TEXT NOT NULL,
  stored_path      TEXT NOT NULL,
  source_root      TEXT,
  size_bytes       BIGINT,
  project_id       INTEGER REFERENCES projects(id) ON DELETE SET NULL,
  conversation_id  INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
  reason           TEXT CHECK (reason IS NULL OR length(reason) <= 500),
  moved_at         {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  restored_at      {TS},
  purged_at        {TS}
);
CREATE INDEX idx_trash_items_moved ON trash_items(moved_at);

-- The restore points Hokma itself took: a ref in the project's Git that is not recorded here is not trusted.
CREATE TABLE checkpoint_points (
  id            {PK},
  project_path  TEXT NOT NULL,
  point_id      TEXT NOT NULL,
  commit_sha    TEXT NOT NULL,
  created_at    {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (project_path, point_id)
);

-- @engine: sqlite

-- SQLite cannot change a CHECK in place: the table is rebuilt inside the migration transaction.
-- Renaming first keeps the triggers and the index with the old table, which is dropped afterwards.
ALTER TABLE guard_events RENAME TO guard_events_old;

CREATE TABLE guard_events (
  id          {PK},
  guard       TEXT NOT NULL CHECK (guard IN ('broker','secrets','permission','protect_kit','approval','transition',
              'delete','truncate','hokma_internal','force_push','outside_workdir','real_money')),
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
  'real_money'));
