CREATE TABLE groups (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    external_id TEXT NOT NULL,
    parent_id INTEGER REFERENCES groups(id),
    name TEXT NOT NULL,
    full_path TEXT NOT NULL,
    web_url TEXT,
    loaded_at TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    last_seen_catalog_run_id INTEGER REFERENCES sync_runs(id),
    is_active INTEGER NOT NULL DEFAULT 1,
    UNIQUE (source_id, external_id)
);

ALTER TABLE users ADD COLUMN raw_json TEXT;
ALTER TABLE users ADD COLUMN last_seen_catalog_run_id INTEGER REFERENCES sync_runs(id);
ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1;

ALTER TABLE projects ADD COLUMN group_id INTEGER REFERENCES groups(id);
ALTER TABLE projects ADD COLUMN default_branch TEXT;
ALTER TABLE projects ADD COLUMN archived INTEGER NOT NULL DEFAULT 0;
ALTER TABLE projects ADD COLUMN raw_json TEXT;
ALTER TABLE projects ADD COLUMN last_seen_catalog_run_id INTEGER REFERENCES sync_runs(id);
ALTER TABLE projects ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1;

ALTER TABLE work_items ADD COLUMN group_id INTEGER REFERENCES groups(id);
ALTER TABLE work_items ADD COLUMN state TEXT;
ALTER TABLE work_items ADD COLUMN description TEXT;
ALTER TABLE work_items ADD COLUMN created_at TEXT;
ALTER TABLE work_items ADD COLUMN updated_at TEXT;
ALTER TABLE work_items ADD COLUMN raw_json TEXT;
ALTER TABLE work_items ADD COLUMN last_seen_catalog_run_id INTEGER REFERENCES sync_runs(id);
ALTER TABLE work_items ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1;

CREATE TABLE memberships (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    external_id TEXT NOT NULL,
    scope_kind TEXT NOT NULL CHECK (scope_kind IN ('group', 'project')),
    group_id INTEGER REFERENCES groups(id),
    project_id INTEGER REFERENCES projects(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    access_level TEXT,
    access_level_value INTEGER,
    expires_at TEXT,
    loaded_at TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    last_seen_catalog_run_id INTEGER NOT NULL REFERENCES sync_runs(id),
    is_active INTEGER NOT NULL DEFAULT 1,
    UNIQUE (source_id, external_id)
);

CREATE TABLE branches (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    project_id INTEGER NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    loaded_at TEXT NOT NULL,
    last_seen_catalog_run_id INTEGER NOT NULL REFERENCES sync_runs(id),
    is_active INTEGER NOT NULL DEFAULT 1,
    UNIQUE (source_id, project_id, name)
);

CREATE TABLE iterations (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    external_id TEXT NOT NULL,
    group_id INTEGER NOT NULL REFERENCES groups(id),
    iid TEXT NOT NULL,
    title TEXT,
    description TEXT,
    state TEXT NOT NULL,
    start_date TEXT,
    due_date TEXT,
    web_url TEXT,
    loaded_at TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    last_seen_catalog_run_id INTEGER NOT NULL REFERENCES sync_runs(id),
    is_active INTEGER NOT NULL DEFAULT 1,
    UNIQUE (source_id, external_id)
);

ALTER TABLE sync_runs ADD COLUMN catalog_counts_json TEXT;

CREATE INDEX idx_groups_source_active ON groups(source_id, is_active);
CREATE INDEX idx_projects_source_active ON projects(source_id, is_active);
CREATE INDEX idx_work_items_source_active ON work_items(source_id, is_active);
