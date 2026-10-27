-- SynaptiMesh BCI PostgreSQL Schema
-- ============================================================
-- Table: aiml
-- Purpose: Store registered mobile/BCI devices and their status
-- ============================================================

CREATE TABLE IF NOT EXISTS aiml (
    device_id   TEXT PRIMARY KEY,
    username    TEXT UNIQUE NOT NULL,
    device_type TEXT
);

-- Index for fast username lookups and conflict resolution
CREATE INDEX IF NOT EXISTS idx_aiml_username ON aiml (username);