-- Hokma stops measuring the loss of the day with its own prices (SPEC-026 v2, piece 3390e3, task 3390). The two columns of account_live_session that held the
-- P&L that Hokma worked out from its book and its quotes at the start of the session (pnl_start, and desk_start for each desk) have no reader any more: the loss
-- of the day is what the broker reports (its equity of the start minus its equity now). equity_start stays: the simulator writes it when it opens a session.
-- This is version 31: the table of risk reservations that was first numbered 31 was withdrawn before it reached main, and no database drops anything for it.
-- 0028 to 0030 are the ones before it. The way back is hokma.db.downgrade.rollback_0031 (the columns return, empty).

-- @engine: all

ALTER TABLE account_live_session DROP COLUMN pnl_start;
ALTER TABLE account_live_session DROP COLUMN desk_start;
