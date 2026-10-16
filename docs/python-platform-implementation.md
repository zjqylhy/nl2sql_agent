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
