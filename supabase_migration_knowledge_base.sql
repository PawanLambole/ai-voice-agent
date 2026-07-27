-- ─────────────────────────────────────────────────────────────────────────────
-- knowledge_base table — stores free-form text knowledge blocks for the AI agent
-- Run this in the Supabase SQL Editor.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS knowledge_base (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title       TEXT NOT NULL,
    content     TEXT NOT NULL,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Trigger: auto-update updated_at on every write
CREATE OR REPLACE FUNCTION update_knowledge_base_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_kb_updated_at ON knowledge_base;
CREATE TRIGGER trg_kb_updated_at
    BEFORE UPDATE ON knowledge_base
    FOR EACH ROW EXECUTE FUNCTION update_knowledge_base_timestamp();

-- Insert initial default knowledge entries if empty
INSERT INTO knowledge_base (title, content, is_active)
SELECT 'General Guidelines & Scope',
'* Do not discuss technical implementation or internal company details.
* Do not answer questions unrelated to the application or business services.
* If you do not know the answer, politely inform the caller that a representative will assist them.
* Never promise features or information that are not explicitly mentioned in this knowledge base.',
TRUE
WHERE NOT EXISTS (SELECT 1 FROM knowledge_base);

INSERT INTO knowledge_base (title, content, is_active)
SELECT 'Company & Business Services',
'We assist callers with inquiries, service details, pricing information, and booking appointments. Always maintain a polite, helpful, and professional tone.',
TRUE
WHERE NOT EXISTS (SELECT 1 FROM knowledge_base WHERE title = 'Company & Business Services');

-- Confirm table contents
SELECT id, title, is_active, created_at FROM knowledge_base;
