-- Engine connections (task 3127, Hokma 0.0.9). A connection is "an engine + a way in": Claude Code, Codex, a direct
-- API key. Secrets are never stored here: they live in the system keyring (service hokma-engine), one entry per id.
-- Additive: a new table and a nullable column. Version 0.0.8 ignores both.

-- @engine: all

CREATE TABLE engine_connections (
  id            {PK},
  engine        TEXT NOT NULL CHECK (engine IN ('claude_code','codex','gemini_cli','kimi_code','api')),
  label         TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 60),
  auth_kind     TEXT NOT NULL CHECK (auth_kind IN ('subscription','oauth_token','api_key','gateway','imported','local')),
  provider      TEXT,
  wire          TEXT CHECK (wire IS NULL OR wire IN ('openai','anthropic')),
  base_url      TEXT,
  tier_models   TEXT,
  status        TEXT NOT NULL DEFAULT 'unknown'
                CHECK (status IN ('connected','no_session','no_cli','error','unknown','removed')),
  status_detail TEXT CHECK (status_detail IS NULL OR length(status_detail) <= 500),
  is_default    INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0,1)),
  settings      TEXT,
  checked_at    {TS},
  created_at    {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE conversations ADD COLUMN connection_id INTEGER REFERENCES engine_connections(id) ON DELETE SET NULL;

-- A student who already connected Claude Code keeps it as the default connection, and every Claude Code conversation
-- points at it. Their model names (haiku, sonnet, opus) and engine_session stay valid.
INSERT INTO engine_connections (engine, label, auth_kind, tier_models, status, is_default)
SELECT 'claude_code', 'Claude Code', 'subscription', '{"fast": "haiku", "balanced": "sonnet", "deep": "opus"}',
       'connected', 1
FROM profile WHERE id = 1 AND claude_status = 'connected';

UPDATE conversations
SET connection_id = (SELECT id FROM engine_connections WHERE engine = 'claude_code' ORDER BY id LIMIT 1)
WHERE engine = 'claude_code' AND connection_id IS NULL;
