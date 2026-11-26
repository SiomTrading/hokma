-- The owner declares the equity an account started its session with, when a position carried over leaves that start unmeasured (SPEC-026, piece 3390c2,
-- task 3390). One row per declaration, never changed: it keeps who declared, when and what, and the live measure reads the latest of the session of the
-- account (session_key). This is version 30: 0028 and 0029 are taken. The way back is hokma.db.downgrade.rollback_0030.

-- @engine: all

CREATE TABLE account_start_declarations (
  id          {PK},
  account_id  INTEGER NOT NULL REFERENCES broker_accounts(id) ON DELETE RESTRICT,
  session_key TEXT NOT NULL,
  equity      TEXT NOT NULL,
  declared_by TEXT NOT NULL,
  declared_at {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_start_declarations_account ON account_start_declarations (account_id, session_key);
