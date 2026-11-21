-- Hokma 1.0.8 (task 3301): Databento and Massive are no longer offered, so a desk cannot keep them in its connections.
-- Only removes rows of desk_connections whose provider_id is no longer in the catalog. Idempotent: a second run finds nothing.
-- No table or column changes.

-- @engine: all

DELETE FROM desk_connections WHERE provider_id IN ('databento', 'massive');
