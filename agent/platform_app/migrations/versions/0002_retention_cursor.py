"""标记图清理完成，防止每轮反复领取同一批过期任务。"""
from alembic import op

revision = "0002_retention_cursor"
down_revision = "0001_python_platform"


def upgrade():
    op.execute("ALTER TABLE platform.analysis_runs ADD COLUMN graph_pruned_at timestamptz")


def downgrade():
    op.execute("ALTER TABLE platform.analysis_runs DROP COLUMN graph_pruned_at")
