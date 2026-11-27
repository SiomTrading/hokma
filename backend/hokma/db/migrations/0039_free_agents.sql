-- 1.2.9 (task 3431): the agents are free with the base, as in ARIA. The base no longer decides who may move a task or a bug, from which state to which, or who
-- may close what. Four rules stay, and they are the hard locks: an agent never lowers the approval of the owner or the real-money lock of a task, and a task that needs
-- the approval of the owner is closed only with it (the real-money rule). What goes: the table of allowed transitions, the open parents, the review path, the reviewer
-- different from the implementer, the counters that only fall when a task closes, the bug transitions and the bug verified by someone else. The table
-- status_transitions stays as it was (it is a reference table the transfer knows); nothing reads it any more.

-- @engine: sqlite

DROP TRIGGER trg_tasks_guard;
DROP TRIGGER trg_tasks_transition;

-- A bug can be fixed, verified or closed with no task behind it: the table is rebuilt without the check that asked for fixed_by_task_id. The rows, the index and the touch trigger go as they were.
CREATE TABLE bugs_new (
  id                {PK},
  project_id        INTEGER REFERENCES projects(id) ON DELETE SET NULL,
  title             TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
  description       TEXT CHECK (description IS NULL OR length(description) <= 8000),
  severity          TEXT NOT NULL DEFAULT 'medium' CHECK (severity IN ('low','medium','high','critical')),
  status            TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open','in_progress','fixed','verified','closed','wont_fix')),
  found_by_kind     TEXT NOT NULL CHECK (found_by_kind IN ('human','agent','system')),
  found_by          TEXT NOT NULL,
  fixed_by_task_id  INTEGER REFERENCES tasks(id),
  changed_by_kind   TEXT NOT NULL DEFAULT 'system' CHECK (changed_by_kind IN ('human','agent','system')),
  changed_by        TEXT NOT NULL DEFAULT 'hokma',
  created_at        {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at        {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  closed_at         {TS},
  CHECK (status NOT IN ('closed','wont_fix') OR closed_at IS NOT NULL)
);
INSERT INTO bugs_new (id, project_id, title, description, severity, status, found_by_kind, found_by, fixed_by_task_id, changed_by_kind, changed_by, created_at, updated_at, closed_at)
  SELECT id, project_id, title, description, severity, status, found_by_kind, found_by, fixed_by_task_id, changed_by_kind, changed_by, created_at, updated_at, closed_at FROM bugs;
DROP TABLE bugs;
ALTER TABLE bugs_new RENAME TO bugs;
CREATE INDEX idx_bugs_status ON bugs(status, severity);
CREATE TRIGGER trg_bugs_touch AFTER UPDATE OF status ON bugs
WHEN NEW.status <> OLD.status
BEGIN
  UPDATE bugs SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

CREATE TRIGGER trg_tasks_guard BEFORE UPDATE ON tasks
BEGIN
  SELECT RAISE(ABORT, 'an agent cannot lower approval or the real-money lock')
  WHERE NEW.changed_by_kind = 'agent' AND (
        NEW.requires_approval < OLD.requires_approval OR
        NEW.touches_real_money < OLD.touches_real_money);
END;

CREATE TRIGGER trg_tasks_transition BEFORE UPDATE OF status ON tasks
WHEN NEW.status <> OLD.status
BEGIN
  SELECT RAISE(ABORT, 'task requires the approval of the owner')
  WHERE NEW.status = 'done' AND NEW.requires_approval = 1 AND (
        NEW.changed_by_kind <> 'human' OR NOT EXISTS (
          SELECT 1 FROM approvals a
          WHERE a.subject_type = 'task' AND a.subject_id = NEW.id AND a.verdict = 'approved'
            AND a.created_at >= OLD.updated_at));
END;

-- @engine: postgres

CREATE OR REPLACE FUNCTION hokma_check_task() RETURNS trigger AS $$
BEGIN
  IF current_user = 'hokma_agent' AND NEW.changed_by_kind <> 'agent' THEN
    RAISE EXCEPTION 'the agent role only writes as an agent';
  END IF;
  IF NEW.changed_by_kind = 'agent' AND (
       NEW.requires_approval < OLD.requires_approval OR
       NEW.touches_real_money < OLD.touches_real_money) THEN
    RAISE EXCEPTION 'an agent cannot lower approval or the real-money lock';
  END IF;
  IF NEW.status = OLD.status THEN RETURN NEW; END IF;
  IF NEW.status = 'done' AND NEW.requires_approval = 1 AND (
       NEW.changed_by_kind <> 'human' OR NOT EXISTS (
         SELECT 1 FROM approvals a
         WHERE a.subject_type = 'task' AND a.subject_id = NEW.id AND a.verdict = 'approved'
           AND a.created_at >= date_trunc('second', OLD.updated_at))) THEN
    RAISE EXCEPTION 'task requires the approval of the owner';
  END IF;
  NEW.updated_at := CURRENT_TIMESTAMP;
  IF NEW.status = 'done' THEN NEW.block_count := 0; NEW.review_cycles := 0; END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

DO $
DECLARE r record;
BEGIN
  FOR r IN SELECT conname FROM pg_constraint
           WHERE conrelid = 'bugs'::regclass AND contype = 'c' AND pg_get_constraintdef(oid) LIKE '%fixed_by_task_id IS NOT NULL%' LOOP
    EXECUTE 'ALTER TABLE bugs DROP CONSTRAINT ' || quote_ident(r.conname);
  END LOOP;
END $;

CREATE OR REPLACE FUNCTION hokma_check_bug() RETURNS trigger AS $$
BEGIN
  IF NEW.status <> OLD.status THEN NEW.updated_at := CURRENT_TIMESTAMP; END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
