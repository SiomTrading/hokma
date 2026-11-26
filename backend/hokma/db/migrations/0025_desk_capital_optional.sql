-- desks.capital becomes optional (SPEC-027, piece 027c, task 3335): a desk is born with its agents and no money; its accounts carry
-- the money and the owner may set a capital later. NULL means "no capital set"; the CHECK of the range stays and a NULL passes it.
-- Old desks keep their number. The way back is hokma.db.downgrade.rollback_0025 (it refuses while a desk has no capital).
-- Postgres drops the NOT NULL here. SQLite cannot: dropping and rebuilding desks fails with foreign_keys=ON because other tables
-- reference it with ON DELETE RESTRICT (that check is not deferrable), so the data step of version 25 (hokma.db.datasteps) takes the
-- NOT NULL out of the stored definition of the table, the way the SQLite manual allows for this one kind of change.

-- @engine: postgres

ALTER TABLE desks ALTER COLUMN capital DROP NOT NULL;
