-- The notice mode with buttons (SPEC-026, piece 3390b, task 3390). An order that waits because a rule in NOTICE mode tripped now carries what the owner
-- needs to answer: which rules tripped (notice_json), a one-use token for the buttons of Telegram (notice_nonce, rotated at every tap) and its own deadline
-- (expires_at: the wait of the rule, 30 minutes by default; before this a pending order always waited 30). notice_grants is the record of "Follow for the
-- rest of the day": a temporary permission per account, desk and rule that ends with the session of the account. It never edits the rule.
-- This is version 29 because 3390c may take 28: apply 28 before 29 in any database. The way back is hokma.db.downgrade.rollback_0029.

-- @engine: all

ALTER TABLE exec_proposals ADD COLUMN notice_json TEXT;
ALTER TABLE exec_proposals ADD COLUMN notice_nonce TEXT CHECK (notice_nonce IS NULL OR length(notice_nonce) = 12);
ALTER TABLE exec_proposals ADD COLUMN expires_at TEXT;

CREATE TABLE notice_grants (
  id          {PK},
  account_id  INTEGER NOT NULL REFERENCES broker_accounts(id) ON DELETE RESTRICT,
  desk_id     INTEGER NOT NULL REFERENCES desks(id) ON DELETE CASCADE,
  rule_id     TEXT NOT NULL CHECK (length(rule_id) BETWEEN 1 AND 60),
  proposal_id INTEGER REFERENCES exec_proposals(id) ON DELETE SET NULL,
  granted_by  TEXT NOT NULL CHECK (granted_by = 'owner'),
  granted_at  {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at  {TS} NOT NULL,
  ended_at    {TS},
  ended_by    TEXT
);
CREATE INDEX idx_notice_grants_account ON notice_grants (account_id, desk_id, rule_id);
