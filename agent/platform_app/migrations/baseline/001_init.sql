CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY, name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
    username TEXT NOT NULL, password_hash BYTEA NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin','data','ops')),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    allowed_metrics TEXT[] NOT NULL,
    UNIQUE (tenant_id, username), UNIQUE (tenant_id, id)
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    FOREIGN KEY (tenant_id, user_id) REFERENCES users(tenant_id, id)
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
    input JSONB NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','querying','completed','needs_clarification','failed','canceled')),
    result JSONB, error TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0, execution_version INTEGER NOT NULL DEFAULT 0,
    lease_until TIMESTAMPTZ, started_at TIMESTAMPTZ, next_seq INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (tenant_id, user_id) REFERENCES users(tenant_id, id)
);
CREATE TABLE IF NOT EXISTS task_events (
    task_id TEXT NOT NULL REFERENCES tasks(id), seq INTEGER NOT NULL,
    kind TEXT NOT NULL, data JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (task_id, seq)
);
CREATE TABLE IF NOT EXISTS task_outbox (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id), delivered_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS task_tenant_time ON tasks(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS task_schedule ON tasks(status, lease_until, created_at);
CREATE INDEX IF NOT EXISTS session_expiration ON sessions(expires_at);
