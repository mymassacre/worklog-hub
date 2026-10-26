CREATE TABLE sources (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    base_url TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE sync_runs (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    date_from TEXT NOT NULL,
    date_to TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed')),
    fetched_count INTEGER NOT NULL DEFAULT 0,
    upserted_count INTEGER NOT NULL DEFAULT 0,
    deleted_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT
);

CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    external_id TEXT NOT NULL,
    username TEXT NOT NULL,
    display_name TEXT,
    web_url TEXT,
    loaded_at TEXT NOT NULL,
    UNIQUE (source_id, external_id)
);

CREATE TABLE projects (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    external_id TEXT NOT NULL,
    name TEXT NOT NULL,
    full_path TEXT,
    web_url TEXT,
    loaded_at TEXT NOT NULL,
    UNIQUE (source_id, external_id)
);

CREATE TABLE work_items (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    project_id INTEGER REFERENCES projects(id),
    external_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    iid TEXT,
    title TEXT NOT NULL,
    web_url TEXT,
    branch_name TEXT,
    loaded_at TEXT NOT NULL,
    UNIQUE (source_id, external_id)
);

CREATE TABLE time_entries (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    external_id TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    project_id INTEGER NOT NULL REFERENCES projects(id),
    work_item_id INTEGER REFERENCES work_items(id),
    spent_at TEXT NOT NULL,
    spent_date TEXT NOT NULL,
    seconds INTEGER NOT NULL,
    summary TEXT,
    loaded_at TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    last_seen_run_id INTEGER NOT NULL REFERENCES sync_runs(id),
    UNIQUE (source_id, external_id)
);

CREATE INDEX idx_time_entries_source_date ON time_entries(source_id, spent_date);

CREATE TABLE entity_links (
    id INTEGER PRIMARY KEY,
    left_work_item_id INTEGER NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
    right_work_item_id INTEGER REFERENCES work_items(id) ON DELETE CASCADE,
    right_source_id INTEGER NOT NULL REFERENCES sources(id),
    right_external_key TEXT NOT NULL,
    method TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    evidence TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (left_work_item_id, right_source_id, right_external_key, method)
);

