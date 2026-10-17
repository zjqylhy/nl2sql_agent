ALTER TABLE tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'query' CHECK (kind IN ('query','analysis'));
CREATE UNIQUE INDEX task_owner_identity ON tasks(tenant_id,user_id,id);
CREATE TABLE analysis_runs (
    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
    role TEXT NOT NULL, binding TEXT NOT NULL CHECK(length(binding)=64),
    metadata JSONB NOT NULL, policy JSONB NOT NULL, state JSONB NOT NULL,
    version INTEGER NOT NULL DEFAULT 0, required_metrics TEXT[] NOT NULL DEFAULT '{}',
    request_id TEXT NOT NULL, request_hash TEXT NOT NULL,
    ready_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY(tenant_id,user_id,id) REFERENCES tasks(tenant_id,user_id,id),
    UNIQUE(tenant_id,user_id,request_id), UNIQUE(tenant_id,id)
);
CREATE TABLE analysis_steps (
    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL, action_key TEXT NOT NULL, phase TEXT NOT NULL,
    execution_version INTEGER NOT NULL, input JSONB NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','completed','failed','unknown','canceled')),
    output JSONB, started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), ended_at TIMESTAMPTZ,
    FOREIGN KEY(tenant_id,run_id) REFERENCES analysis_runs(tenant_id,id), UNIQUE(run_id,sequence)
);
CREATE INDEX analysis_step_cache ON analysis_steps(run_id,action_key) WHERE status='completed';
CREATE TABLE analysis_step_attempts (
    step_id TEXT PRIMARY KEY REFERENCES analysis_steps(id), execution_version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'running', telemetry JSONB, cached BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE TABLE analysis_artifacts (
    tenant_id TEXT NOT NULL, run_id TEXT NOT NULL, result_ref TEXT NOT NULL CHECK(length(result_ref)=64),
    body JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(run_id,result_ref), FOREIGN KEY(tenant_id,run_id) REFERENCES analysis_runs(tenant_id,id)
);
CREATE TABLE analysis_budget_ledger (
    step_id TEXT PRIMARY KEY REFERENCES analysis_steps(id), run_id TEXT NOT NULL REFERENCES analysis_runs(id),
    reserved_tokens BIGINT NOT NULL CHECK(reserved_tokens>=0), reserved_cost BIGINT NOT NULL CHECK(reserved_cost>=0),
    actual_tokens BIGINT, actual_cost BIGINT, status TEXT NOT NULL DEFAULT 'reserved'
        CHECK(status IN ('reserved','settled','unknown'))
);
CREATE TABLE analysis_answers (
    run_id TEXT NOT NULL REFERENCES analysis_runs(id), request_id TEXT NOT NULL,
    body_hash TEXT NOT NULL, resulting_version INTEGER NOT NULL,
    PRIMARY KEY(run_id,request_id)
);
