-- A desk can have many accounts and an account can serve many desks (SPEC-027, piece 027a, task 3335).
-- desk_account_links replaces desk_accounts (one account per desk, one desk per account). desk_strategy_accounts says
-- which account each strategy of a desk trades through; every desk keeps the strategy 'principal'. exec_proposals and
-- broker_orders learn which account and which strategy an order came from.
-- Existing rows are copied: each desk keeps its account and its 'principal' strategy points at it, so old desks behave as before.
-- The way back is hokma.db.downgrade (it refuses when an account is shared or a desk has more than one account).
-- desks.capital is not touched here: it becomes optional in piece 027c.
-- exec_proposals.account_id carries no REFERENCES on purpose: an account is archived, never deleted, and a column with a
-- foreign key cannot be dropped by the way back in SQLite.
-- desk_strategy_accounts uses the default NO ACTION on its link, not RESTRICT: both refuse to unlink an account that a strategy
-- uses, but NO ACTION is checked after the cascades finish, so deleting a desk (which removes its links and its strategies) works.

-- @engine: all

CREATE TABLE desk_account_links (
  desk_id    INTEGER NOT NULL REFERENCES desks(id) ON DELETE CASCADE,
  account_id INTEGER NOT NULL REFERENCES broker_accounts(id) ON DELETE RESTRICT,
  added_at   {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  added_by   TEXT NOT NULL DEFAULT 'owner' CHECK (added_by IN ('owner','chat')),
  PRIMARY KEY (desk_id, account_id)
);
CREATE INDEX idx_desk_account_links_account ON desk_account_links (account_id, desk_id);

CREATE TABLE desk_strategy_accounts (
  desk_id      INTEGER NOT NULL REFERENCES desks(id) ON DELETE CASCADE,
  strategy_key TEXT NOT NULL CHECK (length(strategy_key) BETWEEN 1 AND 80),
  account_id   INTEGER NOT NULL,
  PRIMARY KEY (desk_id, strategy_key),
  FOREIGN KEY (desk_id, account_id) REFERENCES desk_account_links (desk_id, account_id)
);

INSERT INTO desk_account_links (desk_id, account_id, added_by) SELECT desk_id, account_id, 'owner' FROM desk_accounts;
INSERT INTO desk_strategy_accounts (desk_id, strategy_key, account_id) SELECT desk_id, 'principal', account_id FROM desk_accounts;
DROP TABLE desk_accounts;

ALTER TABLE exec_proposals ADD COLUMN account_id INTEGER;
ALTER TABLE exec_proposals ADD COLUMN strategy_key TEXT CHECK (strategy_key IS NULL OR length(strategy_key) BETWEEN 1 AND 80);
ALTER TABLE broker_orders ADD COLUMN strategy_key TEXT CHECK (strategy_key IS NULL OR length(strategy_key) BETWEEN 1 AND 80);
