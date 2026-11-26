-- The layout of a desk (SPEC-025, delivery 3, task 3336): which panels its screen shows and in which order. A JSON text such as
-- {"panels": ["terminal", "company", "rules"]}. NULL means the default order (the five panels every desk had before), so old desks
-- look the same. Adding the column is enough in SQLite and in Postgres. The way back is hokma.db.downgrade.rollback_0026.

-- @engine: all

ALTER TABLE desks ADD COLUMN layout TEXT;
