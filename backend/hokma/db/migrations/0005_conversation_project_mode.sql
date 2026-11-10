-- Conversation project mode (task 3083). A conversation created before 0.0.7 keeps following the active project
-- (project_id NULL means "the active project"); one created from 0.0.7 on is 'fixed': its project_id is its project,
-- and a fixed conversation without project_id is a general one (the working folder).
-- Additive: no row changes, DEFAULT 'follow' reproduces the 0.0.6 behaviour. Plain ALTER TABLE ADD COLUMN works the
-- same on both engines.
--
-- To pin the existing conversations to the project they are in today, a later migration only has to update the
-- data (set project_mode = 'fixed' and resolve each NULL project_id to the active project). This file does not.

-- @engine: all

ALTER TABLE conversations ADD COLUMN project_mode TEXT NOT NULL DEFAULT 'follow' CHECK (project_mode IN ('follow', 'fixed'));

-- "General" in the projects panel: the person chose no active project. 0 keeps the 0.0.6 rule (the last one picked,
-- else the oldest); 1 means the working folder, whatever projects exist.
ALTER TABLE profile ADD COLUMN no_active_project INTEGER NOT NULL DEFAULT 0 CHECK (no_active_project IN (0,1));
