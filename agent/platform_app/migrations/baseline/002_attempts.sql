CREATE UNIQUE INDEX task_tenant_identity ON tasks(tenant_id,id);
CREATE TABLE task_attempts (
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    execution_version INTEGER NOT NULL CHECK (execution_version>0),
    state TEXT NOT NULL DEFAULT 'running' CHECK (state IN ('running','completed','needs_clarification','failed','retry','canceled','lease_expired')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    queue_ms DOUBLE PRECISION NOT NULL CHECK (queue_ms>=0),
    call_ms DOUBLE PRECISION CHECK (call_ms>=0),
    result_bytes INTEGER CHECK (result_bytes>=0),
    error_code TEXT NOT NULL DEFAULT '',
    telemetry JSONB,
    PRIMARY KEY(task_id,execution_version),
    FOREIGN KEY(tenant_id,task_id) REFERENCES tasks(tenant_id,id)
);
CREATE INDEX attempt_tenant_time ON task_attempts(tenant_id,started_at);
