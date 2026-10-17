# Python 单栈实施记录

需求与方案：`python-agent-target-spec.md`，用户已确认并授权实现。默认源码和 Compose 为 Python API/Worker + LangGraph + PostgreSQL，ECS 入口已按单独授权切换；当前部署、真实模型结果与失败记录见 [服务器验收](python-platform-ecs-validation.md)。下文保留首次本地实施时的验证证据，不能将其历史状态理解为当前线上状态。旧 Go/MySQL 保留为显式基线，不参与默认运行路径。

## P0：真实 PostgreSQL 机制验证

- 锁定 LangGraph 1.2.15、langgraph-checkpoint-postgres 3.2.0，依赖见 `agent/requirements.lock`。
- `FencedPostgresSaver` 使用原生 saver 的 SQL 与序列化，仅替换游标事务边界：锁住平台任务、校验 execution_version、状态和租约后，在同一事务写 checkpoint/blob/pending writes。
- 每个领取者持有独立连接与 saver；不暴露任意 thread_id 的 API。父类私有 `_cursor` 是版本耦合点，升级必须重跑机制测试。
- 真实 PG、独立随机数据库、独立子进程测试：回执提交后进程硬退出，恢复复用已保存回执；interrupt 跨进程补充；模型 running 无回执则停止；旧 epoch 的 checkpoint 和 pending writes 均拒绝。
- 验证：`pytest agent/tests/test_python_checkpoint_poc.py -q`：3 passed。它证明框架组合机制，不代表新平台全功能完成或真实模型效果。

## 验收边界

首次实施完成了 P0–P4 的代码、本地进程、持久化故障与数据验证；当时 Docker 服务未运行，只验证了 Compose 配置。后续镜像构建、运行及真实模型冒烟在 ECS 执行，见 [服务器验收](python-platform-ecs-validation.md)。P5 同平台评测入口已适配，题集仍须人工复核和扩充，真实 A/B/C 对照实验尚未执行，不能用模拟模型结果或少量冒烟声明完成率收益。

## P1–P3：纵向闭环

新增 `agent/platform_app`：FastAPI HTTP 接纳、独立 Worker、PG 队列与操作账本、LangGraph StateGraph 的模型/工具循环和 interrupt。运行时不调用 Go 或内部单步 HTTP。Alembic 管理平台迁移；新库三个权限域，旧库必须显式停止旧服务并接管，不能隐式移动数据。

查询内核增加 PG 方言与 psycopg 只读连接；金额除法保持 NUMERIC，结果继续进入 Decimal 工具。`import_olist_postgres` 事务发布新 schema、先聚合商品再关联、评价取最新有效一条，只读角色仅获视图 SELECT；拒绝覆盖快照。

真实 PG 集成 `test_python_platform.py`：17 passed，包含两类任务、原单步/多轮 API、SSE 补读、租户与私有历史、撤权、补充幂等、回执写入后图失败恢复、未知模型不重发、SQL 一次退避重试、取消迟到回执、30 天清理，以及六指标导入口径。两份 CSV 的 36 个显式规格组与独立 oracle 核对通过；模型由测试适配器确定性选择动作，不代表真实模型理解准确率。

## P4：完整数据、兼容与恢复

- 默认 `compose.yaml` 只有 PG、Python API/Worker、维护作业与 Web；`deploy/Dockerfile.platform` 同镜像分命令。旧本地配置归档为 `deploy/compose.legacy.yaml`。
- 完整 Olist CSV 真实导入 99441 单；2018 年 6、7 月，逐州逐项核对六指标的值和样本量，独立 oracle 来自原 CSV，不调用运行时公式。
- API / Worker 数据库账号与分析只读账号均用真实连接验证：API 不能读取图表；平台运行账号不能读取原始事实；查询账号不能读平台或原始表、不能写视图。
- 真实 EDB PG 18.6 `pg_dump/pg_restore`：同一导出 snapshot 的逐表计数、平台/图/分析数据完整恢复。除终态报告外，验证“查询回执保存后图崩溃”的备份恢复，重启后 SQL 仍只执行一次。
- 对隔离旧库验证 001–004 迁移接管：存在未结束任务拒绝接管；bcrypt 密码、已登录会话和旧单步历史保留。
- 故障覆盖参数修正一次、无数据不补零、未知模型不重发、SQL 超时/未知最多一次重试、重试回执提交后再次崩溃、取消迟到写入、撤权、绑定/代码版本、硬调用/Token/费用/时长/步骤预算和保留清理。

## P5：同平台评测准备

`scripts/run_analysis_study.py` 默认审计：80 表达、40 意图组、draft、模型调用 0。`--with-model` 才进入真实 A/B/C；三组均使用 Python 平台、相同 PG 夹具与授权，每组/夹具独立数据库。Worker 在发送前原子扣减全局模型调用上限，API 观测端点只在评测专用模块注册。失败、未知用量、额外调用、延迟与成本继续进入分母和配对报告。

真实 HTTP 子进程验证：独立 API 与 Worker 登录、规则查询、观察 SQL=1 / 模型=0 / 未知=0、统计与清理通过。未执行真实模型对照，未将固定适配器的动作当成模型准确率。

## 首次本地浏览器操作验收（历史）

新版本机预览 `http://localhost:5180/`，完整 Olist、真实 PG、真实图/账本，模型动作明确模拟。账号 `preview / analyst`，仅演示密码 `preview-password-only`，与远端账号无关。

比较 2018 年 6/7 月按 GMV 下降金额取前三：SP -18072.77、MG -12164.76、RS -4472.21 BRL，分别核查订单量与客单价；5 次模拟决策、2 次 SQL。页面明确不同天数比较原始总量、指标变化不代表因果。刷新并重新打开原分析，仍为相同任务、5/2 调用，没有重复执行。此结果用于工作台与数据链路验收，不是模型意图识别评测。

最终版本再核验 Top 3 州 MG/RJ/SP：GMV 517638.01 BRL、有效订单 4106、整体客单价 126.07 BRL/单、占全范围 58.95%；7 次模拟决策、1 次 SQL。模拟未知调用展示未完成、SQL=0、7024 Token 保守预留及未知用量说明。页面持续显示模拟模型标记。

## 首次本地验证汇总（历史）

2026-10-17，首次实施版本在本机执行（后续 ECS 版本以服务器验收记录为准）：

| 检查 | 结果 | 说明 |
| --- | --- | --- |
| `pytest agent/tests -q` | **379 passed, 7 skipped** | 配置真实测试 PG、MySQL 基线、完整 Olist 和 PG 客户端；7 项为 Windows 不适用的 Linux 发布/链接脚本测试 |
| `go vet ./...` / `go test ./... -count=1` | 通过 | 仅保留的 Go 历史基线回归，不参与新平台运行 |
| `web: npm run build` | 通过 | TypeScript + Vite；原 ECharts 分块仍有约 537.6 kB 提示 |
| `docker compose --env-file .env.example config --quiet` | 通过 | Docker Linux Engine 管道不存在，因此未执行镜像构建和容器启动，不能宣称容器已验收 |
| 评测题集审计 | 80 表达 / 40 组，draft，模型调用 0 | 真实 A/B/C 效果、成本与延迟结论尚未产生 |
| 浏览器 | 通过 | 新 Python API/Worker、完整 PG 数据、模拟动作；比较、Top N、刷新恢复和未知失败提示 |

额外保留期故障检查覆盖超过 100 个终态任务：清理成功后保存图清理标记，下一轮推进后续批次；图清理失败不标记完成，下轮安全重试，已归档用量不会归零。固定流程 C 从首个工具起由固定计划选择，模型仅提供完整规格；代码升级阻止旧活动图继续执行，同时保留授权仍有效的已完成历史。

运维与回退命令见 [Python 平台运维](python-platform-operations.md)。首次本地实施未推送或部署；后续按用户授权在新服务器数据卷迁移、验收并切换入口，旧卷保留，详见 [服务器验收](python-platform-ecs-validation.md)。Git 远端仍未推送。
