-- Lessons of the desks (SPEC-017, H1). Additive: two new tables, nothing is altered.
-- A lesson only comes from an executed decision: order_id is required and the code checks that the order executed.
-- Lessons are never deleted: retiring one is status = 'retired'.

-- @engine: all

CREATE TABLE lessons (
  id            {PK},
  desk_id       INTEGER REFERENCES desks(id) ON DELETE RESTRICT,
  scope         TEXT NOT NULL CHECK (scope IN ('desk','general')),
  decision_id   INTEGER NOT NULL REFERENCES decisions(id) ON DELETE RESTRICT,
  order_id      INTEGER NOT NULL REFERENCES broker_orders(id) ON DELETE RESTRICT,
  text          TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 4000),
  outcome_json  TEXT,
  measured_from {TS},
  status        TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','settled','promoted','retired')),
  proposed_by   TEXT NOT NULL CHECK (length(proposed_by) BETWEEN 1 AND 80),
  approved_by   TEXT CHECK (approved_by IS NULL OR length(approved_by) BETWEEN 1 AND 80),
  created_at    {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  settled_at    {TS},
  promoted_at   {TS},
  CHECK (scope <> 'desk' OR desk_id IS NOT NULL),
  CHECK (status <> 'promoted' OR (approved_by IS NOT NULL AND promoted_at IS NOT NULL))
);
CREATE INDEX idx_lessons_desk ON lessons (desk_id, status, created_at);

CREATE TABLE thesis_events (
  id          {PK},
  desk_id     INTEGER NOT NULL REFERENCES desks(id) ON DELETE RESTRICT,
  symbol      TEXT NOT NULL CHECK (length(symbol) BETWEEN 1 AND 40),
  state       TEXT NOT NULL CHECK (state IN ('open','confirmed','invalidated','closed')),
  note        TEXT CHECK (note IS NULL OR length(note) <= 4000),
  created_at  {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_thesis_desk ON thesis_events (desk_id, symbol, created_at);
