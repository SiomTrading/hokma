-- What the Today card of the fund shows (task 3294, class 7): one row, the student's choice as validated JSON
-- (hokma.today.service). No row means the defaults, so the card is never empty. Additive: one new table, no data touched.

-- @engine: all

CREATE TABLE today_config (
  id         INTEGER NOT NULL PRIMARY KEY CHECK (id = 1),
  config     TEXT NOT NULL,
  updated_at {TS} NOT NULL DEFAULT CURRENT_TIMESTAMP
);
