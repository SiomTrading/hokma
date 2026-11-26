-- The highest equity the broker reported in the session of an account, kept in the database (SPEC-026 v2, piece 3390e2, task 3390). The drawdown of an account is
-- this peak minus the equity now, both as the broker reports them; until now the peak lived in memory and a restart of Hokma started it again from the next reading,
-- so a drawdown rule measured less than it should. Hokma values nothing: it only keeps the maximum of what the broker said. It is cleared when the session rolls to
-- the next one. This is version 32: 0031 is the one before it. The way back is hokma.db.downgrade.rollback_0032 (the column goes; the peak is read again from the
-- next readings).

-- @engine: all

ALTER TABLE account_live_session ADD COLUMN peak_equity TEXT;
