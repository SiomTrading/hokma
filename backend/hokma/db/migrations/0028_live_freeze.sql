-- The live freeze of the loss of the day (SPEC-026, piece 3390c, task 3390). account_live_session keeps, for each account, the session it is in
-- (its key and its start, by the calendar of the account and not by the UTC date), the equity it started with and the part of each desk at
-- that moment, so the loss of the day is the equity of the start minus the equity now. desk_day_stops is the stop of ONE desk, in one account
-- or in all of them, when a rule of a desk (not of the account) is reached: only that desk stops, until expires_at (the next session).
-- pnl_start, equity_start and desk_start hold only what was TAKEN LIVE at the open of the session (a position carried over has no start
-- price anywhere else); an account that was flat at the start needs none of them, its start is worked out from the book.
-- This is version 28: 0026 and 0027 are taken. The way back is hokma.db.downgrade.rollback_0028.

-- @engine: all

CREATE TABLE account_live_session (
  account_id         INTEGER NOT NULL PRIMARY KEY REFERENCES broker_accounts(id) ON DELETE RESTRICT,
  calendar           TEXT CHECK (calendar IS NULL OR calendar IN ('cme','us_equity')),
  session_key        TEXT NOT NULL,
  session_started_at {TS} NOT NULL,
  session_ends_at    {TS} NOT NULL,
  pnl_start          TEXT,
  equity_start       TEXT,
  desk_start         TEXT NOT NULL DEFAULT '{}',
  updated_at         {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE desk_day_stops (
  id         {PK},
  desk_id    INTEGER NOT NULL REFERENCES desks(id) ON DELETE CASCADE,
  account_id INTEGER NOT NULL DEFAULT 0,
  rule_id    TEXT NOT NULL,
  loss       TEXT,
  on_trip    TEXT,
  closed_at  {TS} NOT NULL,
  expires_at {TS} NOT NULL,
  lifted_at  {TS}
);
CREATE INDEX idx_desk_day_stops_desk ON desk_day_stops (desk_id, account_id);
