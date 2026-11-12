-- When the student last CHOSE an engine or a model (bug 1105, Hokma 0.0.30). Only the choice writes it: not a connection
-- test, not a status change (`updated_at` changes with those). The chat uses it to pick the replacement when the saved engine
-- cannot answer: the last valid choice of the student first.
-- Additive: one nullable column. A student who already updated keeps the engine saved as the default as their last choice
-- (it was the engine they had); the others stay NULL, which ranks last. The runner applies each file once.

-- @engine: all

ALTER TABLE engine_connections ADD COLUMN chosen_at {TS};

UPDATE engine_connections SET chosen_at = updated_at WHERE is_default = 1 AND chosen_at IS NULL;
