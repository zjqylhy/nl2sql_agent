"""任务约定按版本保存，查询中间结果逐份保存，回执只引用结果。"""
from alembic import op

revision = '0005_task_contracts'
down_revision = '0004_push_tree'


def upgrade():
    op.execute("""
    ALTER TABLE platform.tasks ADD COLUMN contract_version integer NOT NULL DEFAULT 0;
    ALTER TABLE platform.tasks ADD COLUMN intent_policy jsonb;
    ALTER TABLE platform.analysis_artifacts ADD COLUMN contract_version integer NOT NULL DEFAULT 0;
    CREATE TABLE platform.task_contracts(
        task_id text NOT NULL, tenant_id text NOT NULL, user_id text NOT NULL,
        version integer NOT NULL CHECK(version>0), status text NOT NULL CHECK(status='confirmed'),
        body jsonb NOT NULL, plan_hash text NOT NULL, validation jsonb NOT NULL,
        confirmation_request_id text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(task_id,version), UNIQUE(task_id,confirmation_request_id),
        FOREIGN KEY(tenant_id,user_id,task_id) REFERENCES platform.tasks(tenant_id,user_id,id)
    );
    CREATE TABLE platform.task_evidence(
        task_id text NOT NULL, contract_version integer NOT NULL,
        result_ref text NOT NULL, body jsonb NOT NULL CHECK(octet_length(body::text)<=1048576),
        created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(task_id,contract_version,result_ref),
        FOREIGN KEY(task_id,contract_version) REFERENCES platform.task_contracts(task_id,version)
    );
    """)


def downgrade():
    raise RuntimeError('任务约定与证据不能丢弃，请保留追加结构')
