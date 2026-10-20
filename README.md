# 推送效果与设备分析助手

基于 **Python、FastAPI、LangGraph、PostgreSQL 和 React**，按应用、设备厂商、实际通道与用户指定时间查询推送效果和设备覆盖。模型只能选择注册工具，查询、计算与证据完成条件由代码验证。

- 推送：发送量、受理量、已确认到达、点击、已知失败、未知到达、平均到达时延，及到达率、点击/发送率和点击/到达率。
- 设备：指定时点的总量、有效、无效、未知设备，自定义期间的活跃和活跃有效设备。按应用与设备 ID 去重，活跃依据应用前台事件。
- 统一问题入口：初始 AI 同时识别任务类型并生成约定；明确取数生成完整步骤计划，由程序检查并执行全部依赖；开放分析根据真实结果选择调查方向。汇总、趋势、Top N、两期比较均属于明确取数，不再保留固定业务目标入口。
- 支持列出指定发送期间实际出现的 App、模板等维度值，无须回执观察时长；模型失败后可手动重试，保留原问题和已补充条件，新任务单独记账。详见[维度取值](docs/dimension-value-query.md)和[手动重试](docs/model-request-retry.md)。
- 两轮补充保留完整条件，发模型前检查输入预算；已确认分析约定保存在服务端，后续模型仅提交动作。修复范围与真实验收边界见[输入与分析协议修复](docs/initial-context-budget.md)。
- 辅助表单无需模型；自然语言与开放分析需要模型配置。共享任务编号、权限检查、补充记录和预算；结果未知时保守停止。开放分析扩大范围前展示具体变更并等待确认。
- 会话：查询完成后点击“继续追问或调整回答”，可修改条件重查，或复用结果调整表格/卡片与口径解释。消息按树保存，回退后继续自动分叉；旧消息与回答版本保留，上下文只沿当前分支读取。

需求与验收依据见 [开放探索规格](docs/open-exploration-spec.md)、[技术方案](docs/open-exploration-technical-design.md)及[实施记录](docs/open-exploration-implementation.md)。原数据口径见 [推送与设备规格](docs/push-device-spec.md)。

## 数据与指标口径

内置数据由固定种子 `20261018` 生成，全部标为模拟数据，不代表真实厂商效果。3 个虚构应用，600 条应用设备记录对应 400 个模拟物理设备，21,960 条逻辑推送，另含 Token/权限历史、应用活跃、重试及原始回执。设备 ID 为直接生成的业务标识，不包含真实 IMEI、广告 ID 或可用 Token。

每条发送记录增加 `template_id`、`title`、`body`，由生成器产生三套模拟模板文案；可按模板 ID 分组与筛选。文案字段不改变原有随机发送/回执样本，不能据此声称已证明文案导致指标变化。

大规模模拟样本使用独立输出目录，支持约 20 万设备、1000 万逻辑发送，另含关联尝试、回执和活跃事件；不替换上述快速回归样本。生成、复核和导入都采用流式处理，详见[扩容方案](docs/push-scale-technical-design.md)、[本地实施验证](docs/push-scale-results.md)及[服务器发布记录](docs/push-scale-deployment.md)。

```powershell
.venv/Scripts/python.exe -m agent.tools.generate_push --output data/push-large --devices 200000 --deliveries 10000000
# IMPORT_DATABASE_URL 使用维护身份；账号密码从 PUSH_READONLY_PASSWORD 读取。
.venv/Scripts/python.exe -m agent.tools.import_push_postgres --data-dir data/push-large/data --schema analytics_push_large_v1 --readonly-user push_large_ro --report data/push-large/import-report.json
# 使用该快照的只读连接设置 PUSH_VERIFY_DATABASE_URL，revision 为导入输出。
.venv/Scripts/python.exe -m agent.tools.verify_push_scale --bundle data/push-large --schema analytics_push_large_v1 --revision '替换为导入输出的64位摘要' --report data/push-large/query-report.json
```

大样本输出为 `.jsonl.gz`，导入器直接逐行解压；原 `.jsonl` 输入继续可用。生成器拒绝覆盖已有大样本目录，导入器拒绝覆盖已有 schema。导入时一次性生成私有回执归并表，查询仍只使用授权指标视图，并保留原来的观察窗口、只读权限与超时限制。数据库发布不自动切换 API/Worker 的可信数据源映射。

发送样本为 `[2026-04-01,2026-10-01)`，快照为 `2026-10-15T00:00:00+08:00`。用户自定义带时区的 `[start,end_exclusive)`，没有固定周限制。发送期间、回执观察时长、设备状态时点分别指定；超出快照、早于数据范围或观察未成熟时明确拒绝。前端日期使用上海时区。

到达回执缺失保留未知，不等于失败。点击/到达率使用“有到达回执且点击”的交集作为分子，缺到达回执的点击单列。比率按总分子/总分母重算，零分母为无样本。有效设备要求权限明确开启且至少一个有效 Token；权限关闭或全部 Token 明确失效为无效，其余未知。历史存量按时点查询，不累加每日设备数。

公开数据检索与字段参考见 [数据说明](evals/push/README.txt)。公开研究数据不完整覆盖该业务，当前模拟数据与历史 Olist 评测成绩分别记录。

## Windows 本地运行

需要 Python 3.12、Node.js、PostgreSQL，或 Docker Desktop Linux containers。首次配置从 `.env.example` 复制 `.env`；已有 `.env` 请保留并补充所需字段。各账号使用独立随机密码。

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r agent/requirements.lock
.venv/Scripts/python.exe -m agent.tools.generate_push --output data/push
docker compose up -d --build
docker compose run --rm import
docker compose run --rm seed
```

导入输出不可变数据版本 SHA256。把如下可信映射作为单行 `TENANT_DATASOURCES` 写入本地 `.env`，替换密码和摘要。不要将配置或凭据提交 Git。

```json
{"alpha:admin":{"domain":"push","driver":"postgresql","host":"postgres","port":5432,"database":"platform","schema_name":"analytics_push_v1","user":"push_alpha_ro","password":"替换为 PUSH_READONLY_PASSWORD","analysis_date":"2026-10-14","data_start":"2026-04-01T00:00:00+08:00","data_cutoff":"2026-10-15T00:00:00+08:00","is_synthetic":true,"dataset_revision":"替换为导入器输出的64位摘要"}}
```

```powershell
docker compose up -d --force-recreate api worker
```

访问 [工作台](http://localhost:8088)，使用 `alpha / admin` 和 `BOOTSTRAP_PASSWORD`。平台任务与数据使用新项目 `push-analysis-agent` 的独立数据卷。API 与 Worker 使用同一镜像、分进程运行；API 不执行 Agent。只启动 API 时任务会排队。

原生启动使用以下命令，各进程分别配置数据库账号。管理员 `DATABASE_URL` 仅用于维护；API 用 `platform_api`，Worker 和检查点用 `platform_worker`，数据源映射使用只读账号。

```powershell
# 维护终端，管理员 DATABASE_URL。
.venv/Scripts/python.exe -m agent.platform_app migrate
.venv/Scripts/python.exe -m agent.platform_app provision-roles
# IMPORT_DATABASE_URL 为管理员连接，PUSH_READONLY_PASSWORD 为新只读账号密码。
.venv/Scripts/python.exe -m agent.tools.import_push_postgres --data-dir data/push/data --schema analytics_push_v1 --readonly-user push_alpha_ro
.venv/Scripts/python.exe -m agent.platform_app seed --tenant alpha --username admin --domain push
# API 终端，PUBLIC_ORIGIN=http://localhost:5173。
.venv/Scripts/python.exe -m agent.platform_app api --host 127.0.0.1 --port 8080
# 独立 Worker 终端。
.venv/Scripts/python.exe -m agent.platform_app worker
# 前端终端，在 web 目录。
npm ci
npm run dev
```

新快照使用新 schema，导入器拒绝覆盖。`--app-ids app_1` 可以发布只限某应用的视图；每个租户/角色显式配置对应账号与映射。设备历史与多通道先归并成互斥区间，原始回执先按逻辑投递聚合，不向查询账号授予原始表权限。

## 模型与恢复

配置 `NL2SQL_AGENT_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`，确认 tokenizer、真实价格与总预算后设置 `ANALYSIS_ENABLED=true`。推送任务的类型由统一入口识别：明确取数只在开始时生成完整计划，开放分析始终根据观测选择动作；原 `ANALYSIS_STRATEGY=fixed` 仅保留在既有订单评测域，不能将开放推送任务锁回固定业务流程。

升级需先运行迁移与运行账号授权，新增结构由 `0005_task_contracts` 创建。旧数据没有模板字段时，重新生成样本并用**新 schema、只读账号及数据版本**发布；导入器拒绝覆盖旧快照。更新映射后再启动 API/Worker，不能只替换代码就让旧视图宣称具备模板能力。已按用户授权完成服务器升级，实际运行版本、数据范围及限制见 [服务器发布记录](docs/push-scale-deployment.md)。

默认最多模型 10 次、工具 12 次、SQL 6 次、参数修正 1 次、SQL 重试 1 次、活跃执行 180 秒、30000 Token，并行分析 2 个。调用截止和预算随任务冻结。`ANALYSIS_INPUT_PRICE_MICROS` / `OUTPUT_PRICE_MICROS` 表示每百万 Token 的微货币价格，1000000 微单位等于 1 货币单位。未知用量仍占预留，测试单位不能作为真实账单。

图持久化下一节点，操作账本持久化额度、发送意图和回执。回执先完成而图未推进时复用回执，不再发送调用。权限、数据、目录、模型和代码绑定变化后旧任务不能继续执行。检查点、平台、分析域位于同一 PostgreSQL 实例的三个权限域；schema 不提供资源故障隔离。

推送会话将原文、回答版本和摘要分别存储。接近输入预算时摘要较早的祖先消息，保留来源引用和当前完整查询条件；原始消息不删除。必要内容仍超预算则提示缩短问题或新建会话。每个会话最多30条用户消息，单个查询任务的摘要/决策共用4次模型调用与30000预留Token上界。实际结构、恢复边界和发布验证见 [消息树说明](docs/push-tree-results.md)。

## 验证

```powershell
$env:PYTHONUTF8='1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
# TEST_DATABASE_URL 必须指向允许创建/删除临时库与角色的专用测试实例。
.venv/Scripts/python.exe -m pytest agent/tests -q --basetemp=.cache/pytest-push
# 在 web 目录。
npm run build
```

新增推送测试使用真实 PostgreSQL、API/Worker、图与只读视图，对齐独立样本预期；覆盖自定义日期、设备时点、跨应用去重、重复回执、零分母、权限、数据绑定及完成证据。模型协议测试使用模拟响应，不能当作真实模型效果评测。验证结果见 [实施验证记录](docs/push-device-results.md)。

显式 `domain=orders` 的数据源与 `seed --domain orders` 用于历史订单兼容和冻结回归，历史证据见 `docs/analysis-study-pilot-results.md`。推送查询不复用订单多轮计划，分析任务可在缺条件时补充并继续。备份与空库恢复机制见 [平台运维](docs/python-platform-operations.md)，数据域仍与平台/检查点来自同一快照。
