-- ─────────────────────────────────────────────────────────────────────────────
-- agent_config table — stores all AI Voice Agent settings as a single JSONB row
-- Run this ONCE in the Supabase SQL Editor for your project.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS agent_config (
    id          TEXT PRIMARY KEY DEFAULT 'default',
    data        JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Trigger: auto-update updated_at on every write
CREATE OR REPLACE FUNCTION update_agent_config_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_agent_config_updated_at ON agent_config;
CREATE TRIGGER trg_agent_config_updated_at
    BEFORE UPDATE ON agent_config
    FOR EACH ROW EXECUTE FUNCTION update_agent_config_timestamp();

-- Insert an empty default row so upserts always work
INSERT INTO agent_config (id, data)
VALUES ('default', '{}'::jsonb)
ON CONFLICT (id) DO NOTHING;

-- Confirm table was created
SELECT id, updated_at FROM agent_config;
