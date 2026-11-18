-- Delete without losing the history (task 3289, Hokma 1.0.4): the journal is append-only and points at its project, so a
-- project (or a desk's project) with journal entries cannot be removed. It is archived instead: the row stays, hidden
-- from every list, with its name and the date; the entries keep showing the name of the project they came from.
-- Additive: one nullable column, no data touched.

-- @engine: all

ALTER TABLE projects ADD COLUMN archived_at {TS};
