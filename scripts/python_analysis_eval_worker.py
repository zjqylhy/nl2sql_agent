"""评测 Worker 的全局供应商调用上限，跨线程原子扣减且不改模型输出。"""
from agent.platform_app.database import Database
from agent.platform_app.policy import Policy
from agent.platform_app.repository import Repository
from agent.platform_app.worker import run
from agent.runtime.model_transport import ModelConfigurationError, ModelTransport


def main():
    db = Database()
    original = ModelTransport.post
    def bounded(self, *args, **kwargs):
        with db.transaction() as conn:
            value = conn.execute("UPDATE evaluation_budget SET issued=issued+1 WHERE id=1 AND issued<maximum RETURNING issued").fetchone()
            if not value:
                raise ModelConfigurationError("evaluation_budget_exhausted_before_send")
        return original(self, *args, **kwargs)
    ModelTransport.post = bounded
    try:
        run(Repository(db), Policy.from_env())
    finally:
        db.close()


if __name__ == "__main__":
    main()
