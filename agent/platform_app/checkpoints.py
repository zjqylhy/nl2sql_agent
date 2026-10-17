"""在原生 saver 的同一事务内校验执行租约，覆盖 checkpoint 和 pending writes。"""
from contextlib import contextmanager

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row


class LeaseLost(RuntimeError):
    """取消、终态或新执行者接管后，旧执行者必须停止。"""


class FencedPostgresSaver(PostgresSaver):
    def __init__(self, conn, run_id: str, execution_version: int):
        super().__init__(conn)
        self.run_id = run_id
        self.execution_version = execution_version

    @contextmanager
    def _cursor(self, *, pipeline=False):
        # 显式事务代替父类的 pipeline，围栏检查与全部图写入原子提交。
        # 每个领取者独占一个 saver/连接；self.lock 串行化框架的后台写线程。
        with self.lock, self.conn.transaction():
            with self.conn.cursor(binary=True, row_factory=dict_row) as cur:
                row = cur.execute(
                    "SELECT id FROM platform.tasks WHERE id=%s AND execution_version=%s "
                    "AND status='querying' AND lease_until>clock_timestamp() FOR UPDATE",
                    (self.run_id, self.execution_version),
                ).fetchone()
                if not row:
                    raise LeaseLost("任务已停止或执行租约失效")
                yield cur
