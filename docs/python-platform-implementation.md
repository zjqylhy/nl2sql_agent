# Python 单栈实施记录

需求与方案：`python-agent-target-spec.md`，用户已确认并授权实现。旧部署与浏览器演示在切换前仍是 Go / Python / MySQL 基线。

## P0：真实 PostgreSQL 机制验证

- 锁定 LangGraph 1.2.15、langgraph-checkpoint-postgres 3.2.0，依赖见 `agent/requirements.lock`。
- `FencedPostgresSaver` 使用原生 saver 的 SQL 与序列化，仅替换游标事务边界：锁住平台任务、校验 execution_version、状态和租约后，在同一事务写 checkpoint/blob/pending writes。
- 每个领取者持有独立连接与 saver；不暴露任意 thread_id 的 API。父类私有 `_cursor` 是版本耦合点，升级必须重跑机制测试。
- 真实 PG、独立随机数据库、独立子进程测试：回执提交后进程硬退出，恢复复用已保存回执；interrupt 跨进程补充；模型 running 无回执则停止；旧 epoch 的 checkpoint 和 pending writes 均拒绝。
- 验证：`pytest agent/tests/test_python_checkpoint_poc.py -q`：3 passed。它证明框架组合机制，不代表新平台全功能完成或真实模型效果。

## 后续验收

P1 平台与鉴权、P2 PG 数据、P3 Agent 闭环、P4 兼容部署、P5 独立评测依次推进；真实模型实验需使用确认的计价配置，不能用模拟模型结果声明完成率收益。

## P1–P3：纵向闭环

新增 `agent/platform_app`：FastAPI HTTP 接纳、独立 Worker、PG 队列与操作账本、LangGraph StateGraph 的模型/工具循环和 interrupt。运行时不调用 Go 或内部单步 HTTP。Alembic 管理平台迁移；新库三个权限域，旧库必须显式停止旧服务并接管，不能隐式移动数据。

查询内核增加 PG 方言与 psycopg 只读连接；金额除法保持 NUMERIC，结果继续进入 Decimal 工具。`import_olist_postgres` 事务发布新 schema、先聚合商品再关联、评价取最新有效一条，只读角色仅获视图 SELECT；拒绝覆盖快照。

真实 PG 集成 `test_python_platform.py`：17 passed，包含两类任务、原单步/多轮 API、SSE 补读、租户与私有历史、撤权、补充幂等、回执写入后图失败恢复、未知模型不重发、SQL 一次退避重试、取消迟到回执、30 天清理，以及六指标导入口径。两份 CSV 的 36 个显式规格组与独立 oracle 核对通过；模型由测试适配器确定性选择动作，不代表真实模型理解准确率。

P4 的默认部署入口、备份恢复、真实完整 Olist 数据核对及 P5 新平台评测适配仍在进行。
