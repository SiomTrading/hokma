-- State of the encrypted cloud backup of the student's database and memory (task 3215, SPEC-011 P10, Hokma 0.0.31).
-- Additive: one new table, one row per destination. Version 0.0.30 ignores it. Credentials never live here (keyring).
-- The numbers 0009 and 0010 belong to other pieces of the same release; versions only have to be unique and ordered.

-- @engine: all

CREATE TABLE cloud_backup_state (
  id               {PK},
  destination      TEXT NOT NULL UNIQUE CHECK (destination IN ('drive','github')),
  status           TEXT NOT NULL DEFAULT 'idle' CHECK (status IN ('idle','running','ok','failed')),
  status_detail    TEXT,
  last_backup_at   {TS},
  last_size_bytes  INTEGER CHECK (last_size_bytes IS NULL OR last_size_bytes >= 0),
  last_remote_name TEXT,
  created_at       {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at       {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);
