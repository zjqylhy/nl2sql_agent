CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin','data','ops')),
    binding TEXT NOT NULL CHECK (length(binding)=64),
    analysis_date DATE NOT NULL,
    catalog_hash TEXT NOT NULL CHECK (length(catalog_hash)=64),
    version INTEGER NOT NULL DEFAULT 0 CHECK (version>=0),
    turn_count INTEGER NOT NULL DEFAULT 0 CHECK (turn_count BETWEEN 0 AND 30),
    plan JSONB,
    pending JSONB,
    active_task_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (tenant_id,user_id) REFERENCES users(tenant_id,id),
    UNIQUE(tenant_id,user_id,id), UNIQUE(tenant_id,id)
);
ALTER TABLE tasks ADD COLUMN conversation_id TEXT;
ALTER TABLE tasks ADD COLUMN conversation_turn INTEGER;
ALTER TABLE tasks ADD COLUMN request_id TEXT;
ALTER TABLE tasks ADD COLUMN conversation_context JSONB;
ALTER TABLE tasks ADD COLUMN admission_version INTEGER;
ALTER TABLE tasks ADD CONSTRAINT task_conversation_owner FOREIGN KEY(tenant_id,user_id,conversation_id) REFERENCES conversations(tenant_id,user_id,id);
ALTER TABLE tasks ADD CONSTRAINT task_conversation_complete CHECK (
    (conversation_id IS NULL AND conversation_turn IS NULL AND request_id IS NULL AND conversation_context IS NULL AND admission_version IS NULL)
    OR (conversation_id IS NOT NULL AND conversation_turn IS NOT NULL AND conversation_turn BETWEEN 1 AND 30 AND request_id IS NOT NULL AND conversation_context IS NOT NULL AND jsonb_typeof(conversation_context)='object' AND admission_version IS NOT NULL AND admission_version>0)
);
CREATE UNIQUE INDEX conversation_turn_identity ON tasks(tenant_id,conversation_id,conversation_turn);
CREATE UNIQUE INDEX conversation_request_identity ON tasks(tenant_id,conversation_id,request_id);
CREATE UNIQUE INDEX task_conversation_identity ON tasks(tenant_id,conversation_id,id);
ALTER TABLE conversations ADD CONSTRAINT conversation_active_task FOREIGN KEY(tenant_id,id,active_task_id) REFERENCES tasks(tenant_id,conversation_id,id);
CREATE INDEX conversation_owner_time ON conversations(tenant_id,user_id,updated_at DESC,id);
