-- Chat state in the database: the active project, archived conversations and the tool lines of each message.
-- Replaces the <data>/active-project file. Plain ALTER TABLE ADD COLUMN works the same on both engines.

-- @engine: all

ALTER TABLE profile ADD COLUMN active_project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL;

ALTER TABLE conversations ADD COLUMN archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0,1));
CREATE INDEX idx_conversations_archived ON conversations(archived, pinned, last_message_at);

ALTER TABLE messages ADD COLUMN meta TEXT CHECK (meta IS NULL OR length(meta) <= 64000);
