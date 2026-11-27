-- 1.3.0 (task 3433): what a strategy creation costs. One row for each creation (the code of Hokma writes it, never an agent) and the baseline that the new flow is measured against:
-- the one of 1.2.8 (Ian, Sam and Ben, one creator in the lab) taken on the PC of the owner on 2026-11-26 with the same strategy. Additive: one new table.

-- @engine: all

CREATE TABLE creation_metrics (
  id               {PK},
  label            TEXT NOT NULL CHECK (length(trim(label)) BETWEEN 1 AND 120),
  kind             TEXT NOT NULL CHECK (kind IN ('baseline','creation')),
  agent            TEXT CHECK (agent IS NULL OR length(agent) BETWEEN 1 AND 80),
  hokma_version    TEXT CHECK (hokma_version IS NULL OR length(hokma_version) <= 40),
  conversation_id  INTEGER,                                     -- no foreign key on purpose: a conversation may be deleted and the measure stays
  bank_id          INTEGER,
  str_code         TEXT CHECK (str_code IS NULL OR length(str_code) <= 20),
  started_at       {TS},
  finished_at      {TS},
  seconds          INTEGER NOT NULL DEFAULT 0 CHECK (seconds >= 0),
  llm_calls        INTEGER NOT NULL DEFAULT 0 CHECK (llm_calls >= 0),     -- turns of the model: tool calls plus the closing answer (an upper bound when calls go in parallel)
  tool_calls       INTEGER NOT NULL DEFAULT 0 CHECK (tool_calls >= 0),    -- calls to the tools of Hokma made by the creator, the ones of the paperwork included
  dry_runs         INTEGER NOT NULL DEFAULT 0 CHECK (dry_runs >= 0),
  tokens_in        INTEGER NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
  tokens_out       INTEGER NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
  tokens_total     INTEGER NOT NULL DEFAULT 0 CHECK (tokens_total >= 0),
  note             TEXT CHECK (note IS NULL OR length(note) <= 2000),
  created_at       {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_creation_metrics_label ON creation_metrics (label, id);
CREATE INDEX idx_creation_metrics_conv ON creation_metrics (conversation_id, id);

INSERT INTO creation_metrics (label, kind, agent, hokma_version, seconds, llm_calls, tool_calls, tokens_total, note)
VALUES ('baseline-1.2.8', 'baseline', 'sam', '1.2.8', 903, 75, 0, 254880,
        'Measured by the owner on his PC on 2026-11-26 with Hokma 1.2.8 (Ian, then Sam, then Ben) and the strategy of the reel. Sam alone: 15 min 03 s, 75 calls, 254,880 tokens. The split in and out and the tool calls were not recorded.');
INSERT INTO creation_metrics (label, kind, agent, hokma_version, seconds, llm_calls, tool_calls, tokens_total, note)
VALUES ('baseline-1.2.8', 'baseline', 'joel-lab', '1.2.8', 275, 10, 0, 89934,
        'Measured by the owner on his PC on 2026-11-26 with Hokma 1.2.8: the same strategy written by one agent in the lab (Joel). 4 min 35 s, 10 calls, 89,934 tokens. The split in and out and the tool calls were not recorded.');
