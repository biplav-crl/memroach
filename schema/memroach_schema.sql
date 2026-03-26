-- MemRoach Schema for CockroachDB
-- Unkillable memory for AI agents

-- Content-addressable blob store (deduplicated across machines/users)
CREATE TABLE IF NOT EXISTS memroach_blobs (
    content_hash STRING(64) PRIMARY KEY,
    content_bytes BYTES NOT NULL,
    original_size INT8 NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- File metadata per user+machine, with visibility, type, and versioning
CREATE TABLE IF NOT EXISTS memroach_files (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_name STRING NOT NULL,
    machine_id STRING NOT NULL,
    file_path STRING NOT NULL,
    file_type STRING NOT NULL DEFAULT 'file',
    content_hash STRING(64) NOT NULL REFERENCES memroach_blobs(content_hash),
    file_size INT8 NOT NULL,
    file_mtime TIMESTAMPTZ NOT NULL,
    is_deleted BOOL NOT NULL DEFAULT false,
    visibility STRING NOT NULL DEFAULT 'private',
    version INT8 NOT NULL DEFAULT 1,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_name, machine_id, file_path)
);

CREATE INDEX IF NOT EXISTS idx_memroach_files_user ON memroach_files (user_name, synced_at DESC);
CREATE INDEX IF NOT EXISTS idx_memroach_files_user_machine ON memroach_files (user_name, machine_id);
CREATE INDEX IF NOT EXISTS idx_memroach_files_hash ON memroach_files (content_hash);
CREATE INDEX IF NOT EXISTS idx_memroach_files_visibility ON memroach_files (visibility, user_name);
CREATE INDEX IF NOT EXISTS idx_memroach_files_type ON memroach_files (user_name, file_type);

-- Vector embeddings for semantic search
CREATE TABLE IF NOT EXISTS memroach_embeddings (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_name STRING NOT NULL,
    file_path STRING NOT NULL,
    content_hash STRING(64) NOT NULL,
    embedding VECTOR(1024) NOT NULL,
    chunk_index INT4 NOT NULL DEFAULT 0,
    chunk_text STRING NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_name, file_path, content_hash, chunk_index)
);

-- Version history for file changelog/timeline
CREATE TABLE IF NOT EXISTS memroach_history (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_name STRING NOT NULL,
    machine_id STRING NOT NULL,
    file_path STRING NOT NULL,
    content_hash STRING(64) NOT NULL REFERENCES memroach_blobs(content_hash),
    file_size INT8 NOT NULL,
    version INT8 NOT NULL,
    operation STRING NOT NULL DEFAULT 'update',  -- 'create', 'update', 'delete'
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memroach_history_user_path ON memroach_history (user_name, file_path, created_at DESC);

-- Graph links between memories (relates_to, duplicates, supersedes, caused_by)
CREATE TABLE IF NOT EXISTS memroach_links (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_name STRING NOT NULL,
    from_path STRING NOT NULL,
    to_path STRING NOT NULL,
    link_type STRING NOT NULL DEFAULT 'relates_to',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_name, from_path, to_path, link_type)
);

CREATE INDEX IF NOT EXISTS idx_memroach_links_from ON memroach_links (user_name, from_path);
CREATE INDEX IF NOT EXISTS idx_memroach_links_to ON memroach_links (user_name, to_path);

-- Access tracking for memory decay / compaction
CREATE TABLE IF NOT EXISTS memroach_access (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_name STRING NOT NULL,
    file_path STRING NOT NULL,
    access_type STRING NOT NULL DEFAULT 'read',
    accessed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memroach_access_user_path ON memroach_access (user_name, file_path, accessed_at DESC);

-- Audit log
CREATE TABLE IF NOT EXISTS memroach_log (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_name STRING NOT NULL,
    machine_id STRING NOT NULL,
    operation STRING NOT NULL,
    files_changed INT4 NOT NULL DEFAULT 0,
    bytes_transferred INT8 NOT NULL DEFAULT 0,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memroach_log_user ON memroach_log (user_name, completed_at DESC);
