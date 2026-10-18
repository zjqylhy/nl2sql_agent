"""同一查询保存有界补充；每轮单独记回执，避免恢复时重放模型调用。"""
from alembic import op

revision = "0003_query_clarification"
down_revision = "0002_retention_cursor"


def upgrade():
    op.execute("""
        ALTER TABLE platform.tasks ADD COLUMN query_version integer NOT NULL DEFAULT 0
            CHECK(query_version BETWEEN 0 AND 2);
        ALTER TABLE platform.tasks ADD COLUMN query_dialogue jsonb NOT NULL DEFAULT '[]'
            CHECK(jsonb_typeof(query_dialogue)='array' AND jsonb_array_length(query_dialogue)<=2);
        CREATE TABLE platform.query_answers(
            task_id text NOT NULL REFERENCES platform.tasks(id),
            request_id text NOT NULL,
            body_hash text NOT NULL,
            version integer NOT NULL CHECK(version BETWEEN 1 AND 2),
            PRIMARY KEY(task_id,request_id), UNIQUE(task_id,version)
        );
        CREATE TABLE platform.query_followup_receipts(
            task_id text NOT NULL REFERENCES platform.tasks(id),
            version integer NOT NULL CHECK(version BETWEEN 1 AND 2),
            status text NOT NULL CHECK(status IN ('running','completed')),
            output jsonb, created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(task_id,version)
        );
    """)


def downgrade():
    raise RuntimeError("补充记录与调用回执不可丢弃；回退应用时保留追加结构")
