-- Hokma base schema: the 22 tables of the data model plus onboarding_progress.
-- Sections: "-- @engine: all" runs on every engine, "-- @engine: sqlite" / "-- @engine: postgres" only there.
-- {PK} and {TS} are replaced per engine by the migration runner.

-- @engine: all

CREATE TABLE schema_version (
  version     INTEGER NOT NULL PRIMARY KEY,
  applied_at  {TS}    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE profile (
  id            INTEGER NOT NULL PRIMARY KEY CHECK (id = 1),
  owner_name    TEXT    NOT NULL CHECK (length(owner_name) BETWEEN 1 AND 60),
  fund_name     TEXT             CHECK (fund_name IS NULL OR length(fund_name) <= 60),
  timezone      TEXT    NOT NULL DEFAULT 'America/New_York',
  language      TEXT    NOT NULL DEFAULT 'es' CHECK (language IN ('es','en')),
  engine        TEXT    NOT NULL DEFAULT 'claude_code'
                        CHECK (engine IN ('claude_code','codex','gemini_cli','kimi_code','api')),
  telemetry_enabled INTEGER NOT NULL DEFAULT 0 CHECK (telemetry_enabled IN (0,1)),
  max_parallel_agents INTEGER CHECK (max_parallel_agents IS NULL OR max_parallel_agents >= 1),
  workdir       TEXT,
  database_engine TEXT  NOT NULL DEFAULT 'sqlite' CHECK (database_engine IN ('sqlite','postgres')),
  claude_status TEXT    NOT NULL DEFAULT 'unknown'
                        CHECK (claude_status IN ('connected','no_session','no_cli','unknown')),
  claude_plan   TEXT,
  claude_checked_at {TS},
  weekly_token_cap INTEGER CHECK (weekly_token_cap IS NULL OR weekly_token_cap > 0),
  theme         TEXT    NOT NULL DEFAULT 'dark' CHECK (theme IN ('dark','light')),
  setup_completed_at {TS},
  created_at    {TS}    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    {TS}    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE license_checks (
  id              {PK},
  checked_at      {TS}  NOT NULL DEFAULT CURRENT_TIMESTAMP,
  result          TEXT  NOT NULL CHECK (result IN ('valid','revoked','expired','offline','error')),
  latest_version  TEXT,
  detail          TEXT  CHECK (detail IS NULL OR length(detail) <= 500)
);

CREATE TABLE agents (
  slug            TEXT    NOT NULL PRIMARY KEY CHECK (slug = lower(slug) AND length(slug) BETWEEN 2 AND 40),
  display_name    TEXT    NOT NULL,
  description     TEXT,
  model_tier      TEXT    NOT NULL DEFAULT 'balanced' CHECK (model_tier IN ('fast','balanced','deep')),
  status          TEXT    NOT NULL DEFAULT 'off' CHECK (status IN ('active','off')),
  enabled_session INTEGER CHECK (enabled_session IS NULL OR enabled_session BETWEEN 0 AND 20),
  is_custom       INTEGER NOT NULL DEFAULT 0 CHECK (is_custom IN (0,1)),
  file_path       TEXT,
  created_at      {TS}    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      {TS}    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE projects (
  id          {PK},
  slug        TEXT NOT NULL UNIQUE,
  name        TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
  path        TEXT NOT NULL,
  kind        TEXT NOT NULL DEFAULT 'other'
              CHECK (kind IN ('ips','wealth','strategy','research','robot','other')),
  status      TEXT NOT NULL DEFAULT 'active'
              CHECK (status IN ('idea','active','paused','done','cancelled')),
  description TEXT,
  default_requires_review   INTEGER NOT NULL DEFAULT 0 CHECK (default_requires_review IN (0,1)),
  default_reviewer_agent    TEXT    REFERENCES agents(slug),
  default_requires_approval INTEGER NOT NULL DEFAULT 0 CHECK (default_requires_approval IN (0,1)),
  created_at  {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE tasks (
  id                {PK},
  project_id        INTEGER REFERENCES projects(id) ON DELETE SET NULL,
  title             TEXT    NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
  body              TEXT    CHECK (body IS NULL OR length(body) <= 8000),
  status            TEXT    NOT NULL DEFAULT 'ready'
                    CHECK (status IN ('triage','todo','ready','running','blocked','review',
                                      'awaiting_approval','done','failed','archived')),
  priority          INTEGER NOT NULL DEFAULT 0 CHECK (priority BETWEEN -2 AND 2),
  assigned_agent    TEXT    REFERENCES agents(slug),
  implementer_agent TEXT    REFERENCES agents(slug),
  requires_review   INTEGER NOT NULL DEFAULT 0 CHECK (requires_review IN (0,1)),
  reviewer_agent    TEXT    REFERENCES agents(slug),
  requires_approval INTEGER NOT NULL DEFAULT 0 CHECK (requires_approval IN (0,1)),
  touches_real_money INTEGER NOT NULL DEFAULT 0 CHECK (touches_real_money IN (0,1)),
  created_by_kind   TEXT    NOT NULL CHECK (created_by_kind IN ('human','agent','scheduler')),
  created_by        TEXT    NOT NULL,
  changed_by_kind   TEXT    NOT NULL DEFAULT 'system' CHECK (changed_by_kind IN ('human','agent','system')),
  changed_by        TEXT    NOT NULL DEFAULT 'hokma',
  change_reason     TEXT    CHECK (change_reason IS NULL OR length(change_reason) <= 500),
  claim_token       TEXT,
  claimed_by        TEXT,
  claimed_at        {TS},
  claim_expires_at  {TS},
  heartbeat_at      {TS},
  max_runtime_seconds INTEGER NOT NULL DEFAULT 1800 CHECK (max_runtime_seconds BETWEEN 60 AND 14400),
  attempts          INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  max_attempts      INTEGER NOT NULL DEFAULT 2 CHECK (max_attempts BETWEEN 1 AND 5),
  block_kind        TEXT    CHECK (block_kind IS NULL OR block_kind IN ('dependency','needs_input','capability','transient')),
  block_reason      TEXT    CHECK (block_reason IS NULL OR length(block_reason) <= 500),
  block_count       INTEGER NOT NULL DEFAULT 0 CHECK (block_count >= 0),
  block_limit       INTEGER NOT NULL DEFAULT 2 CHECK (block_limit BETWEEN 1 AND 5),
  review_cycles     INTEGER NOT NULL DEFAULT 0 CHECK (review_cycles >= 0),
  review_limit      INTEGER NOT NULL DEFAULT 2 CHECK (review_limit BETWEEN 1 AND 5),
  result_summary    TEXT    CHECK (result_summary IS NULL OR length(result_summary) <= 4000),
  result_metadata   TEXT    CHECK (result_metadata IS NULL OR length(result_metadata) <= 16000),
  run_at            {TS},
  created_at        {TS}    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at        {TS}    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at      {TS},
  CHECK (status <> 'blocked' OR (block_kind IS NOT NULL AND block_reason IS NOT NULL)),
  CHECK (status <> 'running' OR (claim_token IS NOT NULL AND claimed_at IS NOT NULL)),
  CHECK (status NOT IN ('done','failed','archived') OR completed_at IS NOT NULL),
  CHECK (status <> 'done' OR result_summary IS NOT NULL),
  CHECK (requires_review = 0 OR reviewer_agent IS NOT NULL),
  CHECK (reviewer_agent IS NULL OR implementer_agent IS NULL OR reviewer_agent <> implementer_agent),
  CHECK (block_count <= block_limit),
  CHECK (review_cycles <= review_limit),
  CHECK (touches_real_money = 0 OR requires_approval = 1)
);
CREATE INDEX idx_tasks_status        ON tasks(status);
CREATE INDEX idx_tasks_lane          ON tasks(status, priority DESC, created_at) WHERE claim_token IS NULL;
CREATE INDEX idx_tasks_agent_status  ON tasks(assigned_agent, status);
CREATE INDEX idx_tasks_project       ON tasks(project_id);
CREATE INDEX idx_tasks_run_at        ON tasks(run_at) WHERE run_at IS NOT NULL;
CREATE INDEX idx_tasks_running_hb    ON tasks(heartbeat_at) WHERE status = 'running';

CREATE TABLE task_links (
  parent_id  INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  child_id   INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  PRIMARY KEY (parent_id, child_id),
  CHECK (parent_id <> child_id)
);
CREATE INDEX idx_task_links_child ON task_links(child_id);

CREATE TABLE task_runs (
  id             {PK},
  task_id        INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  agent_slug     TEXT    NOT NULL REFERENCES agents(slug),
  role           TEXT    NOT NULL DEFAULT 'implement' CHECK (role IN ('implement','review')),
  model          TEXT    NOT NULL,
  pid            INTEGER,
  started_at     {TS}    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  heartbeat_at   {TS},
  ended_at       {TS},
  outcome        TEXT    NOT NULL DEFAULT 'running'
                 CHECK (outcome IN ('running','completed','review_requested','approved_by_reviewer',
                                    'changes_requested','blocked','failure','timed_out','crashed','released')),
  summary        TEXT    CHECK (summary IS NULL OR length(summary) <= 4000),
  metadata       TEXT    CHECK (metadata IS NULL OR length(metadata) <= 16000),
  error          TEXT    CHECK (error IS NULL OR length(error) <= 2000),
  tokens_in      INTEGER NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
  tokens_out     INTEGER NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
  CHECK (outcome = 'running' OR ended_at IS NOT NULL),
  CHECK (outcome NOT IN ('completed','review_requested','approved_by_reviewer','changes_requested','blocked')
         OR summary IS NOT NULL)
);
CREATE INDEX idx_task_runs_task ON task_runs(task_id, started_at);
CREATE UNIQUE INDEX uq_task_runs_one_running ON task_runs(task_id) WHERE outcome = 'running';

CREATE TABLE task_events (
  id           {PK},
  task_id      INTEGER NOT NULL REFERENCES tasks(id),
  from_status  TEXT,
  to_status    TEXT    NOT NULL,
  actor_kind   TEXT    NOT NULL CHECK (actor_kind IN ('human','agent','system')),
  actor        TEXT    NOT NULL,
  reason       TEXT,
  created_at   {TS}    NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_task_events_task ON task_events(task_id, created_at);

CREATE TABLE task_comments (
  id          {PK},
  task_id     INTEGER NOT NULL REFERENCES tasks(id),
  author_kind TEXT    NOT NULL CHECK (author_kind IN ('human','agent','system')),
  author      TEXT    NOT NULL,
  body        TEXT    NOT NULL CHECK (length(body) BETWEEN 1 AND 2000),
  created_at  {TS}    NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_task_comments_task ON task_comments(task_id, created_at);

CREATE TABLE status_transitions (
  entity       TEXT NOT NULL CHECK (entity IN ('task','bug')),
  from_status  TEXT NOT NULL,
  to_status    TEXT NOT NULL,
  actor_kinds  TEXT NOT NULL,
  PRIMARY KEY (entity, from_status, to_status)
);

CREATE TABLE approvals (
  id              {PK},
  subject_type    TEXT NOT NULL CHECK (subject_type IN
                  ('task','decision','strategy','robot','ips_change','extension','scheduled_job','bug','permission')),
  subject_id      INTEGER NOT NULL,
  verdict         TEXT NOT NULL CHECK (verdict IN ('approved','rejected','changes_requested')),
  decided_by_kind TEXT NOT NULL DEFAULT 'human' CHECK (decided_by_kind = 'human'),
  decided_by      TEXT NOT NULL,
  channel         TEXT NOT NULL CHECK (channel IN ('dashboard','telegram','cli')),
  note            TEXT CHECK (note IS NULL OR length(note) <= 1000),
  created_at      {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_approvals_subject ON approvals(subject_type, subject_id, created_at);

CREATE TABLE decisions (
  id               {PK},
  project_id       INTEGER REFERENCES projects(id) ON DELETE SET NULL,
  topic            TEXT NOT NULL CHECK (length(topic) BETWEEN 1 AND 200),
  context          TEXT CHECK (context IS NULL OR length(context) <= 8000),
  options          TEXT CHECK (options IS NULL OR length(options) <= 8000),
  proposal         TEXT NOT NULL,
  proposed_by      TEXT NOT NULL REFERENCES agents(slug),
  status           TEXT NOT NULL DEFAULT 'proposed'
                   CHECK (status IN ('proposed','approved','rejected','superseded')),
  decided_by_kind  TEXT CHECK (decided_by_kind IS NULL OR decided_by_kind IN ('human','agent')),
  decided_by       TEXT,
  approval_id      INTEGER REFERENCES approvals(id),
  superseded_by    INTEGER REFERENCES decisions(id),
  created_at       {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  decided_at       {TS},
  CHECK (status NOT IN ('approved','rejected') OR (decided_by_kind IS NOT NULL AND decided_at IS NOT NULL)),
  CHECK (decided_by_kind IS NULL OR decided_by_kind <> 'human' OR approval_id IS NOT NULL),
  CHECK (status <> 'superseded' OR superseded_by IS NOT NULL)
);
CREATE INDEX idx_decisions_status ON decisions(status, created_at);

CREATE TABLE journal_entries (
  id          {PK},
  kind        TEXT NOT NULL CHECK (kind IN ('fact','decision','action','block','alert','note')),
  outcome     TEXT NOT NULL DEFAULT 'info' CHECK (outcome IN ('success','failure','partial','info')),
  agent_slug  TEXT REFERENCES agents(slug),
  actor_kind  TEXT NOT NULL CHECK (actor_kind IN ('human','agent','system')),
  task_id     INTEGER REFERENCES tasks(id),
  project_id  INTEGER REFERENCES projects(id),
  title       TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
  body        TEXT CHECK (body IS NULL OR length(body) <= 8000),
  refs        TEXT CHECK (refs IS NULL OR length(refs) <= 4000),
  created_at  {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_journal_created ON journal_entries(created_at);
CREATE INDEX idx_journal_agent   ON journal_entries(agent_slug, created_at);

CREATE TABLE guard_events (
  id          {PK},
  guard       TEXT NOT NULL CHECK (guard IN ('broker','secrets','permission','protect_kit','approval','transition')),
  decision    TEXT NOT NULL CHECK (decision IN ('blocked','asked','allowed_by_human','denied_by_human')),
  agent_slug  TEXT REFERENCES agents(slug),
  tool        TEXT,
  task_id     INTEGER REFERENCES tasks(id),
  reason      TEXT NOT NULL CHECK (length(reason) <= 1000),
  created_at  {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_guard_events_created ON guard_events(created_at);

CREATE TABLE bugs (
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
  CHECK (status NOT IN ('fixed','verified') OR fixed_by_task_id IS NOT NULL),
  CHECK (status NOT IN ('closed','wont_fix') OR closed_at IS NOT NULL)
);
CREATE INDEX idx_bugs_status ON bugs(status, severity);

CREATE TABLE conversations (
  id               {PK},
  engine           TEXT NOT NULL,
  engine_session   TEXT,
  title            TEXT,
  pinned           INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0,1)),
  project_id       INTEGER REFERENCES projects(id) ON DELETE SET NULL,
  model            TEXT,
  agent_slug       TEXT REFERENCES agents(slug),
  started_at       {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_message_at  {TS}
);
CREATE INDEX idx_conversations_project ON conversations(project_id);
CREATE INDEX idx_conversations_recent  ON conversations(pinned, last_message_at);

CREATE TABLE messages (
  id               {PK},
  conversation_id  INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role             TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
  agent_slug       TEXT REFERENCES agents(slug),
  model            TEXT,
  content          TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'complete' CHECK (status IN ('complete','stopped','error')),
  tokens_in        INTEGER NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
  tokens_out       INTEGER NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
  created_at       {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_messages_conv    ON messages(conversation_id, created_at);
CREATE INDEX idx_messages_created ON messages(created_at);

CREATE TABLE attachments (
  id               {PK},
  message_id       INTEGER REFERENCES messages(id) ON DELETE SET NULL,
  project_id       INTEGER REFERENCES projects(id) ON DELETE SET NULL,
  kind             TEXT NOT NULL CHECK (kind IN ('image','audio','file')),
  name             TEXT CHECK (name IS NULL OR length(name) <= 255),
  stored_path      TEXT NOT NULL UNIQUE,
  mime_type        TEXT NOT NULL,
  size_bytes       INTEGER NOT NULL CHECK (size_bytes > 0 AND size_bytes <= 26214400),
  sha256           TEXT NOT NULL CHECK (length(sha256) = 64),
  transcript       TEXT,
  created_at       {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (kind = 'audio' OR transcript IS NULL)
);
CREATE INDEX idx_attachments_kind ON attachments(kind, created_at);

CREATE TABLE extensions (
  id            {PK},
  name          TEXT NOT NULL,
  marketplace   TEXT NOT NULL,
  kind          TEXT NOT NULL CHECK (kind IN ('plugin','skill','mcp')),
  version       TEXT,
  license       TEXT,
  enabled       INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,1)),
  is_preinstalled INTEGER NOT NULL DEFAULT 0 CHECK (is_preinstalled IN (0,1)),
  approval_id   INTEGER REFERENCES approvals(id),
  installed_at  {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (marketplace, name)
);

CREATE TABLE extension_requests (
  id              {PK},
  action          TEXT NOT NULL CHECK (action IN ('install','enable','disable','uninstall')),
  kind            TEXT NOT NULL CHECK (kind IN ('plugin','skill','mcp')),
  name            TEXT NOT NULL,
  marketplace     TEXT NOT NULL,
  version         TEXT,
  license         TEXT,
  reason          TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 1000),
  requested_by_kind TEXT NOT NULL CHECK (requested_by_kind IN ('human','agent')),
  requested_by    TEXT NOT NULL,
  task_id         INTEGER REFERENCES tasks(id),
  status          TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','approved','rejected','applied','failed','expired')),
  approval_id     INTEGER REFERENCES approvals(id),
  extension_id    INTEGER REFERENCES extensions(id),
  error           TEXT CHECK (error IS NULL OR length(error) <= 1000),
  requested_at    {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  decided_at      {TS},
  applied_at      {TS},
  CHECK (status = 'pending' OR status = 'expired' OR decided_at IS NOT NULL),
  CHECK (status NOT IN ('approved','rejected','applied','failed')
         OR requested_by_kind = 'human' OR approval_id IS NOT NULL),
  CHECK (status <> 'applied' OR (applied_at IS NOT NULL AND extension_id IS NOT NULL))
);
CREATE INDEX idx_extension_requests_status ON extension_requests(status, requested_at);

CREATE TABLE scheduled_jobs (
  id             {PK},
  name           TEXT NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 80),
  agent_slug     TEXT NOT NULL REFERENCES agents(slug),
  prompt         TEXT NOT NULL CHECK (length(prompt) BETWEEN 1 AND 4000),
  schedule_kind  TEXT NOT NULL CHECK (schedule_kind IN ('interval','daily','weekly','cron','once')),
  schedule_expr  TEXT NOT NULL,
  timezone       TEXT NOT NULL,
  is_active      INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0,1)),
  approval_id    INTEGER REFERENCES approvals(id),
  last_run_at    {TS},
  next_run_at    {TS},
  last_task_id   INTEGER REFERENCES tasks(id),
  created_at     {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_scheduled_next ON scheduled_jobs(next_run_at) WHERE is_active = 1;

CREATE TABLE onboarding_progress (
  step     TEXT NOT NULL PRIMARY KEY CHECK (length(step) BETWEEN 1 AND 80),
  done_at  {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Task transitions. The three "to triage" rows from todo and ready are added so that an archived parent can
-- send its unfinished children to triage (a rule of the data model that needs them).
INSERT INTO status_transitions (entity, from_status, to_status, actor_kinds) VALUES
  ('task','triage','todo','agent,human'),
  ('task','triage','ready','agent,human'),
  ('task','triage','archived','agent,human'),
  ('task','todo','ready','system,agent,human'),
  ('task','todo','archived','agent,human'),
  ('task','todo','triage','system'),
  ('task','ready','running','system,agent'),
  ('task','ready','todo','system'),
  ('task','ready','blocked','agent,system'),
  ('task','ready','archived','agent,human'),
  ('task','ready','triage','system'),
  ('task','running','done','agent,system'),
  ('task','running','review','agent'),
  ('task','running','awaiting_approval','agent,system'),
  ('task','running','blocked','agent,system'),
  ('task','running','ready','system'),
  ('task','running','failed','system'),
  ('task','running','triage','system'),
  ('task','blocked','triage','system'),
  ('task','blocked','ready','agent,human,system'),
  ('task','blocked','todo','system'),
  ('task','blocked','archived','agent,human'),
  ('task','review','done','agent,human'),
  ('task','review','awaiting_approval','agent'),
  ('task','review','ready','agent,human'),
  ('task','review','triage','system'),
  ('task','awaiting_approval','done','human'),
  ('task','awaiting_approval','ready','human'),
  ('task','awaiting_approval','archived','human'),
  ('task','failed','ready','agent,human'),
  ('task','failed','archived','agent,human'),
  ('task','done','ready','agent,human'),
  ('task','done','archived','agent,human,system'),
  ('bug','open','in_progress','human,agent,system'),
  ('bug','open','wont_fix','human,agent,system'),
  ('bug','in_progress','fixed','human,agent,system'),
  ('bug','in_progress','open','human,agent,system'),
  ('bug','in_progress','wont_fix','human,agent,system'),
  ('bug','fixed','verified','human,agent,system'),
  ('bug','fixed','in_progress','human,agent,system'),
  ('bug','verified','closed','human,agent,system'),
  ('bug','closed','open','human,agent,system');

-- @engine: sqlite

CREATE TRIGGER trg_tasks_guard BEFORE UPDATE ON tasks
BEGIN
  SELECT RAISE(ABORT, 'an agent cannot lower review, approval or the real-money lock')
  WHERE NEW.changed_by_kind = 'agent' AND (
        NEW.requires_review < OLD.requires_review OR
        NEW.requires_approval < OLD.requires_approval OR
        NEW.touches_real_money < OLD.touches_real_money);
  SELECT RAISE(ABORT, 'block and review counters only return to 0 when the task is closed')
  WHERE NEW.status <> 'done' AND (NEW.block_count < OLD.block_count OR NEW.review_cycles < OLD.review_cycles);
END;

CREATE TRIGGER trg_tasks_transition BEFORE UPDATE OF status ON tasks
WHEN NEW.status <> OLD.status
BEGIN
  SELECT RAISE(ABORT, 'task transition not allowed')
  WHERE NOT EXISTS (
    SELECT 1 FROM status_transitions
    WHERE entity = 'task' AND from_status = OLD.status AND to_status = NEW.status
      AND (',' || actor_kinds || ',') LIKE ('%,' || NEW.changed_by_kind || ',%'));
  SELECT RAISE(ABORT, 'task has unfinished parents')
  WHERE NEW.status IN ('ready','running') AND EXISTS (
    SELECT 1 FROM task_links l JOIN tasks p ON p.id = l.parent_id
    WHERE l.child_id = NEW.id AND p.status <> 'done');
  SELECT RAISE(ABORT, 'task must go through review or approval')
  WHERE OLD.status = 'running' AND NEW.status = 'done'
    AND (NEW.requires_review = 1 OR NEW.requires_approval = 1);
  SELECT RAISE(ABORT, 'task has no review configured')
  WHERE OLD.status = 'running' AND NEW.status = 'review' AND NEW.requires_review = 0;
  SELECT RAISE(ABORT, 'only the reviewer, other than the implementer, closes a review')
  WHERE OLD.status = 'review' AND NEW.changed_by_kind = 'agent'
    AND (NEW.changed_by IS NOT NEW.reviewer_agent OR NEW.changed_by = NEW.implementer_agent);
  SELECT RAISE(ABORT, 'task requires the approval of the owner')
  WHERE NEW.status = 'done' AND NEW.requires_approval = 1 AND (
        NEW.changed_by_kind <> 'human' OR NOT EXISTS (
          SELECT 1 FROM approvals a
          WHERE a.subject_type = 'task' AND a.subject_id = NEW.id AND a.verdict = 'approved'
            AND a.created_at >= OLD.updated_at));
END;

CREATE TRIGGER trg_tasks_touch AFTER UPDATE OF status ON tasks
WHEN NEW.status <> OLD.status
BEGIN
  UPDATE tasks SET updated_at = CURRENT_TIMESTAMP,
                   block_count   = CASE WHEN NEW.status = 'done' THEN 0 ELSE block_count END,
                   review_cycles = CASE WHEN NEW.status = 'done' THEN 0 ELSE review_cycles END
   WHERE id = NEW.id;
  INSERT INTO task_events (task_id, from_status, to_status, actor_kind, actor, reason)
  VALUES (NEW.id, OLD.status, NEW.status, NEW.changed_by_kind, NEW.changed_by, NEW.change_reason);
END;

CREATE TRIGGER trg_tasks_event_ins AFTER INSERT ON tasks
BEGIN
  INSERT INTO task_events (task_id, from_status, to_status, actor_kind, actor, reason)
  VALUES (NEW.id, NULL, NEW.status, NEW.changed_by_kind, NEW.changed_by, NEW.change_reason);
END;

CREATE TRIGGER trg_bugs_transition BEFORE UPDATE OF status ON bugs
WHEN NEW.status <> OLD.status
BEGIN
  SELECT RAISE(ABORT, 'bug transition not allowed')
  WHERE NOT EXISTS (
    SELECT 1 FROM status_transitions
    WHERE entity = 'bug' AND from_status = OLD.status AND to_status = NEW.status
      AND (',' || actor_kinds || ',') LIKE ('%,' || NEW.changed_by_kind || ',%'));
  SELECT RAISE(ABORT, 'a bug is verified by someone other than who fixed it')
  WHERE NEW.status = 'verified' AND NEW.changed_by = OLD.changed_by;
END;

CREATE TRIGGER trg_bugs_touch AFTER UPDATE OF status ON bugs
WHEN NEW.status <> OLD.status
BEGIN
  UPDATE bugs SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

CREATE TRIGGER trg_approvals_no_upd BEFORE UPDATE ON approvals       BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER trg_approvals_no_del BEFORE DELETE ON approvals       BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER trg_journal_no_upd   BEFORE UPDATE ON journal_entries BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER trg_journal_no_del   BEFORE DELETE ON journal_entries BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER trg_events_no_upd    BEFORE UPDATE ON task_events     BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER trg_events_no_del    BEFORE DELETE ON task_events     BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER trg_guard_no_upd     BEFORE UPDATE ON guard_events    BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER trg_guard_no_del     BEFORE DELETE ON guard_events    BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER trg_comments_no_upd  BEFORE UPDATE ON task_comments   BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER trg_comments_no_del  BEFORE DELETE ON task_comments   BEGIN SELECT RAISE(ABORT, 'append-only table'); END;

-- @engine: postgres
-- Same rules as the SQLite triggers above, with the same messages (SPEC-003 section 2.4). The role check only
-- matters when the role `hokma_agent` exists (see hokma.db.transfer.provision_roles).

CREATE FUNCTION hokma_check_task() RETURNS trigger AS $$
BEGIN
  IF current_user = 'hokma_agent' AND NEW.changed_by_kind <> 'agent' THEN
    RAISE EXCEPTION 'the agent role only writes as an agent';
  END IF;
  IF NEW.changed_by_kind = 'agent' AND (
       NEW.requires_review < OLD.requires_review OR
       NEW.requires_approval < OLD.requires_approval OR
       NEW.touches_real_money < OLD.touches_real_money) THEN
    RAISE EXCEPTION 'an agent cannot lower review, approval or the real-money lock';
  END IF;
  IF NEW.status <> 'done' AND (NEW.block_count < OLD.block_count OR NEW.review_cycles < OLD.review_cycles) THEN
    RAISE EXCEPTION 'block and review counters only return to 0 when the task is closed';
  END IF;
  IF NEW.status = OLD.status THEN RETURN NEW; END IF;
  IF NOT EXISTS (
    SELECT 1 FROM status_transitions
    WHERE entity = 'task' AND from_status = OLD.status AND to_status = NEW.status
      AND (',' || actor_kinds || ',') LIKE ('%,' || NEW.changed_by_kind || ',%')
  ) THEN
    RAISE EXCEPTION 'task transition not allowed';
  END IF;
  IF NEW.status IN ('ready','running') AND EXISTS (
    SELECT 1 FROM task_links l JOIN tasks p ON p.id = l.parent_id
    WHERE l.child_id = NEW.id AND p.status <> 'done'
  ) THEN
    RAISE EXCEPTION 'task has unfinished parents';
  END IF;
  IF OLD.status = 'running' AND NEW.status = 'done'
     AND (NEW.requires_review = 1 OR NEW.requires_approval = 1) THEN
    RAISE EXCEPTION 'task must go through review or approval';
  END IF;
  IF OLD.status = 'running' AND NEW.status = 'review' AND NEW.requires_review = 0 THEN
    RAISE EXCEPTION 'task has no review configured';
  END IF;
  IF OLD.status = 'review' AND NEW.changed_by_kind = 'agent'
     AND (NEW.changed_by IS DISTINCT FROM NEW.reviewer_agent OR NEW.changed_by = NEW.implementer_agent) THEN
    RAISE EXCEPTION 'only the reviewer, other than the implementer, closes a review';
  END IF;
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

CREATE TRIGGER trg_tasks_check BEFORE UPDATE ON tasks
  FOR EACH ROW EXECUTE FUNCTION hokma_check_task();

CREATE FUNCTION hokma_check_bug() RETURNS trigger AS $$
BEGIN
  IF NEW.status = OLD.status THEN RETURN NEW; END IF;
  IF NOT EXISTS (
    SELECT 1 FROM status_transitions
    WHERE entity = 'bug' AND from_status = OLD.status AND to_status = NEW.status
      AND (',' || actor_kinds || ',') LIKE ('%,' || NEW.changed_by_kind || ',%')
  ) THEN
    RAISE EXCEPTION 'bug transition not allowed';
  END IF;
  IF NEW.status = 'verified' AND NEW.changed_by = OLD.changed_by THEN
    RAISE EXCEPTION 'a bug is verified by someone other than who fixed it';
  END IF;
  NEW.updated_at := CURRENT_TIMESTAMP;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_bugs_check BEFORE UPDATE ON bugs
  FOR EACH ROW EXECUTE FUNCTION hokma_check_bug();

CREATE FUNCTION hokma_log_task_event() RETURNS trigger AS $$
BEGIN
  INSERT INTO task_events (task_id, from_status, to_status, actor_kind, actor, reason)
  VALUES (NEW.id, CASE WHEN TG_OP = 'UPDATE' THEN OLD.status END, NEW.status,
          NEW.changed_by_kind, NEW.changed_by, NEW.change_reason);
  RETURN NULL;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_tasks_event_ins AFTER INSERT ON tasks
  FOR EACH ROW EXECUTE FUNCTION hokma_log_task_event();
CREATE TRIGGER trg_tasks_event_upd AFTER UPDATE OF status ON tasks
  FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status) EXECUTE FUNCTION hokma_log_task_event();

CREATE FUNCTION hokma_append_only() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'append-only table'; END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_approvals_ro  BEFORE UPDATE OR DELETE ON approvals       FOR EACH ROW EXECUTE FUNCTION hokma_append_only();
CREATE TRIGGER trg_journal_ro    BEFORE UPDATE OR DELETE ON journal_entries FOR EACH ROW EXECUTE FUNCTION hokma_append_only();
CREATE TRIGGER trg_events_ro     BEFORE UPDATE OR DELETE ON task_events     FOR EACH ROW EXECUTE FUNCTION hokma_append_only();
CREATE TRIGGER trg_guard_ro      BEFORE UPDATE OR DELETE ON guard_events    FOR EACH ROW EXECUTE FUNCTION hokma_append_only();
CREATE TRIGGER trg_comments_ro   BEFORE UPDATE OR DELETE ON task_comments   FOR EACH ROW EXECUTE FUNCTION hokma_append_only();
