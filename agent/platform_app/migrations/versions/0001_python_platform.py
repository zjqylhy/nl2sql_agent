"""保留账号/会话/任务表契约，增加图恢复的持久输入与单步调用回执。"""
from pathlib import Path
from alembic import op

revision = "0001_python_platform"
down_revision = None


def upgrade():
    conn = op.get_bind()
    if not conn.exec_driver_sql("SELECT to_regclass('platform.tasks')").scalar():
        for path in sorted((Path(__file__).parents[1] / "baseline").glob("*.sql")):
            conn.exec_driver_sql(path.read_text(encoding="utf-8"))
    conn.exec_driver_sql("""
        ALTER TABLE tasks ADD COLUMN ready_at timestamptz NOT NULL DEFAULT now();
        ALTER TABLE tasks ADD COLUMN query_role text;
        ALTER TABLE tasks ADD COLUMN query_binding text;
        ALTER TABLE analysis_runs ADD COLUMN resume_input jsonb;
        CREATE TABLE query_receipts(
            task_id text PRIMARY KEY REFERENCES tasks(id),
            status text NOT NULL CHECK(status IN ('running','completed')),
            output jsonb, created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX task_ready ON tasks(ready_at,created_at) WHERE status IN ('pending','querying');
        CREATE TABLE login_windows(identity text PRIMARY KEY, started_at timestamptz NOT NULL, attempts integer NOT NULL);
    """)


def downgrade():
    raise RuntimeError("不可自动回滚持久任务数据；按备份恢复流程回退")
