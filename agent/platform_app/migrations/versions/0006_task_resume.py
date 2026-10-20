"""同一任务追加续跑轮次；原解析回执和费用记录不覆盖。"""
from alembic import op

revision = '0006_task_resume'
down_revision = '0005_task_contracts'


def upgrade():
    op.execute("""
    ALTER TABLE platform.tasks ADD COLUMN resume_round integer NOT NULL DEFAULT 0 CHECK(resume_round>=0);
    ALTER TABLE platform.analysis_runs ADD COLUMN evidence_lifecycle text NOT NULL DEFAULT 'available'
        CHECK(evidence_lifecycle IN ('available','pruning','pruned'));
    ALTER TABLE platform.analysis_runs ADD COLUMN recovery_checkpoint jsonb;
    UPDATE platform.analysis_runs SET evidence_lifecycle=CASE WHEN graph_pruned_at IS NOT NULL THEN 'pruned' ELSE 'pruning' END
        WHERE graph_pruned_at IS NOT NULL OR state->'feedback'->>'evidence_expired'='true';
    CREATE TABLE platform.task_resume_rounds(
        task_id text NOT NULL REFERENCES platform.tasks(id), round_no integer NOT NULL CHECK(round_no>0),
        request_id text NOT NULL, request_hash text NOT NULL, source_execution_version integer NOT NULL,
        created_by text NOT NULL, recovery jsonb NOT NULL,
        additional_model_calls integer NOT NULL CHECK(additional_model_calls BETWEEN 0 AND 10),
        usage_before jsonb NOT NULL, usage_after jsonb, previous_failure jsonb NOT NULL,
        status text NOT NULL DEFAULT 'accepted' CHECK(status IN ('accepted','running','completed','failed','canceled')),
        created_at timestamptz NOT NULL DEFAULT now(), ended_at timestamptz,
        PRIMARY KEY(task_id,round_no), UNIQUE(task_id,request_id), UNIQUE(task_id,source_execution_version)
    );
    ALTER TABLE platform.analysis_steps ADD COLUMN resume_round integer NOT NULL DEFAULT 0;
    ALTER TABLE platform.analysis_artifacts ADD COLUMN resume_round integer NOT NULL DEFAULT 0;
    ALTER TABLE platform.query_receipts ADD COLUMN resume_round integer NOT NULL DEFAULT 0;
    ALTER TABLE platform.query_receipts ADD COLUMN model_usage jsonb;
    ALTER TABLE platform.query_receipts DROP CONSTRAINT query_receipts_pkey;
    ALTER TABLE platform.query_receipts ADD PRIMARY KEY(task_id,resume_round);
    ALTER TABLE platform.query_followup_receipts ADD COLUMN resume_round integer NOT NULL DEFAULT 0;
    ALTER TABLE platform.query_followup_receipts ADD COLUMN model_usage jsonb;
    ALTER TABLE platform.query_followup_receipts DROP CONSTRAINT query_followup_receipts_pkey;
    ALTER TABLE platform.query_followup_receipts ADD PRIMARY KEY(task_id,version,resume_round);
    """)


def downgrade():
    raise RuntimeError('续跑授权和历史回执不能丢弃，请按备份恢复流程处理')
