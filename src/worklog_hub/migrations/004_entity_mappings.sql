-- Подтверждённые соответствия и отдельно проверяемые кандидаты.
CREATE TABLE entity_mappings (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('project', 'task', 'user')),
    gitlab_project_id INTEGER REFERENCES projects(id),
    jira_project_id INTEGER REFERENCES projects(id),
    gitlab_work_item_id INTEGER REFERENCES work_items(id),
    jira_work_item_id INTEGER REFERENCES work_items(id),
    gitlab_user_id INTEGER REFERENCES users(id),
    jira_user_id INTEGER REFERENCES users(id),
    status TEXT NOT NULL CHECK (status IN ('proposed', 'confirmed', 'rejected')),
    method TEXT NOT NULL CHECK (method IN ('manual', 'jira_key', 'username')),
    evidence TEXT NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reviewed_at TEXT,
    CHECK (
        (kind='project' AND gitlab_project_id IS NOT NULL AND jira_project_id IS NOT NULL
         AND gitlab_work_item_id IS NULL AND jira_work_item_id IS NULL
         AND gitlab_user_id IS NULL AND jira_user_id IS NULL)
        OR
        (kind='task' AND gitlab_work_item_id IS NOT NULL AND jira_work_item_id IS NOT NULL
         AND gitlab_project_id IS NULL AND jira_project_id IS NULL
         AND gitlab_user_id IS NULL AND jira_user_id IS NULL)
        OR
        (kind='user' AND gitlab_user_id IS NOT NULL AND jira_user_id IS NOT NULL
         AND gitlab_project_id IS NULL AND jira_project_id IS NULL
         AND gitlab_work_item_id IS NULL AND jira_work_item_id IS NULL)
    ),
    UNIQUE (gitlab_project_id, jira_project_id),
    UNIQUE (gitlab_work_item_id, jira_work_item_id),
    UNIQUE (gitlab_user_id, jira_user_id)
);

-- Один аккаунт в каждой системе соответствует одному аккаунту другой системы.
-- Для проектов и задач допустимы связи многие-ко-многим.
CREATE UNIQUE INDEX mapping_confirmed_gitlab_user
ON entity_mappings(gitlab_user_id) WHERE kind='user' AND status='confirmed';
CREATE UNIQUE INDEX mapping_confirmed_jira_user
ON entity_mappings(jira_user_id) WHERE kind='user' AND status='confirmed';

CREATE INDEX mapping_status ON entity_mappings(kind, status);

CREATE TABLE mapping_reviews (
    id INTEGER PRIMARY KEY,
    mapping_id INTEGER NOT NULL REFERENCES entity_mappings(id),
    previous_status TEXT,
    new_status TEXT NOT NULL CHECK (new_status IN ('proposed', 'confirmed', 'rejected')),
    action TEXT NOT NULL,
    created_at TEXT NOT NULL
);
