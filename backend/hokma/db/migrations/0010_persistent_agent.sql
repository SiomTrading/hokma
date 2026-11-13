-- Persistent agent (task 3215, SPEC-011, Hokma 0.0.31): two device-local preferences, both off by default.
-- keep_awake: while a chat turn or a routine is running, the computer does not go to sleep.
-- wake_enabled: the computer is woken shortly before the next routine.
-- Additive: version 0.0.30 ignores the columns.

-- @engine: all

ALTER TABLE profile ADD COLUMN keep_awake INTEGER NOT NULL DEFAULT 0 CHECK (keep_awake IN (0,1));
ALTER TABLE profile ADD COLUMN wake_enabled INTEGER NOT NULL DEFAULT 0 CHECK (wake_enabled IN (0,1));
