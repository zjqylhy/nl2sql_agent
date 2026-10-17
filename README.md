# Olist 经营分析助手

默认后端为 **Python + FastAPI + LangGraph + PostgreSQL**，前端 React。API 和 Worker 使用同一镜像、分进程运行；默认部署不启动 Go 或 MySQL 服务。

支持原单次/多轮问数，以及范围有限的经营分析：比较两个时间段各州 GMV、按下降金额或比例排名并核查订单量和客单价；汇总 GMV 前 N 州的金额、订单量、整体客单价和占比。模型选择下一步，数字由受限 SQL 与 Decimal 工具计算；指标变化只作描述性分析。

需求与边界见[实施规格](docs/python-agent-target-spec.md)，实现见[实施记录](docs/python-platform-implementation.md)，当前云端结果及限制见[ECS 部署验收](docs/python-platform-ecs-validation.md)。Go/Python/MySQL 首版保留作历史基线，相关实测不能当成新平台的模型效果证据。

## 运行边界

- API 只做认证、授权、幂等接纳和结果读取。任务/outbox 在同一事务写入；Worker 领取后执行整张 StateGraph。
- PostgreSQL 分 `platform`、`agent_checkpoint`、`analytics_*` 三个权限域。API 无图状态权限，分析查询账号仅能读取授权视图；schema 不提供 CPU/I/O 故障隔离。
- 图保存下一节点和 interrupt；操作账本保存预算、执行回执和证据。恢复先核账，已完成操作复用回执，模型调用结果未知时停止。
- 默认上限：模型 10 次、工具 12 次、SQL 6 次、全局参数修正 1 次、SQL 重试 1 次、活跃执行 180 秒、30000 Token、并行分析 2 个。价格和模型映射随任务冻结。
- 输出上限、单次截止、Token 总预算及调用间隔在任务接纳时冻结。明确的网关执行前拒绝最多退避一次；通用 429、503、断连和超时不推断为零费用。
- 两轮补充、等待最长 24 小时。详细证据与图线程保留 30 天，最终报告及已知/未知用量汇总保留。

## Windows 本地启动

需要 Python 3.12、Node.js，以及 PostgreSQL。容器启动需 Docker Desktop Linux containers；源码入口无需 Go。

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# 已有 .env 时保留原配置，按新版字段补齐；也可另存并用 --env-file 指定。
# 为各账号分别生成随机十六进制密码，替换 .env 占位值。
docker compose config --quiet
docker compose up -d --build
```

打开 [本地工作台](http://localhost:8088)。新栈名称 `olist-python-agent`、数据卷 `python_postgres_data`；不会复用或覆盖旧 Go/MySQL 栈的数据卷。首次启动后还需导入数据、配置映射并创建账号。

### 初始化数据与账号

下载官方 [Olist 数据集](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce)，按其许可使用，四份必需 CSV 放入 `data/olist`。

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r agent/requirements.lock
.venv/Scripts/python.exe agent/tools/download_olist.py --output data/olist
docker compose run --rm import
docker compose run --rm seed
```

导入器输出实际 CSV 的 SHA256 版本摘要，拒绝覆盖已有 schema。将以下映射作为单行 `TENANT_DATASOURCES` 写入本地 `.env`，替换密码和版本；每个启用角色显式配置可信映射：

```json
{
  "alpha:admin": {
    "driver": "postgresql",
    "host": "postgres",
    "port": 5432,
    "database": "platform",
    "schema_name": "analytics_alpha_v1",
    "user": "alpha_ro",
    "password": "替换为 OLIST_READONLY_PASSWORD",
    "analysis_date": "2018-08-31",
    "dataset_revision": "替换为导入器输出的64位摘要"
  }
}
```

```powershell
docker compose up -d --force-recreate api worker
```

使用 `alpha / admin` 和 `.env` 的 `BOOTSTRAP_PASSWORD` 登录。可查询六指标：GMV、有效订单量、客单价、取消率、平均交付时长、平均评分。`seed` 不会覆盖已有账号。数据库密码和 DSN 不进入浏览器；不要分享 `.env` 或完整 Compose 配置输出。

数据发布先按订单聚合商品，再关联每单最新有效评价，避免重复累计。带州限制的数据源可用新的 schema/只读账号导入并指定 `--states SP RJ`。每个新快照使用新 schema 和版本；修改绑定后旧任务不能继续执行。

### 模型与分析开关

原问数的完整规则表达不调用模型；陌生表达可以由配置的网关模型兜底。经营分析需配置 `NL2SQL_AGENT_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`，并确认 tokenizer 映射、真实价格与总预算后设置 `ANALYSIS_ENABLED=true`。

`ANALYSIS_INPUT_PRICE_MICROS` / `OUTPUT_PRICE_MICROS` 表示每百万 Token 的微货币单位价格，1000000 微单位等于 1 货币单位。预留与已知用量不等于供应商最终账单。示例中的空价格不能当作免费价格使用。默认策略 `adaptive`，`fixed` 用于对照实验。

模型网关默认示例为 `https://localhost:9443/v1`。容器中 `localhost` 指容器本身，需按实际网络配置可达地址、TLS CA 和连接覆盖；参见[网关手册](docs/model-gateway-operations.md)，其中旧 agent 服务名在新部署中对应 worker。不能关闭 TLS 校验绕过证书问题。

### 源码调试

在项目根目录、分别设置环境变量的终端运行。API 使用平台 API 账号；Worker 使用平台 Worker 账号；`TENANT_DATASOURCES` 使用只读分析账号。管理员连接只用于迁移和初始化。

```powershell
# 维护终端：DATABASE_URL 为管理员 DSN。
.venv/Scripts/python.exe -m agent.platform_app migrate
.venv/Scripts/python.exe -m agent.platform_app provision-roles

# API 终端：DATABASE_URL 为 platform_api DSN。
$env:PUBLIC_ORIGIN='http://localhost:5173'
.venv/Scripts/python.exe -m agent.platform_app api --host 127.0.0.1 --port 8080

# Worker 终端：DATABASE_URL 和 CHECKPOINT_DATABASE_URL 为 platform_worker DSN。
.venv/Scripts/python.exe -m agent.platform_app worker

# 前端终端。
Set-Location web
npm ci
npm run dev
```

FastAPI 不启动后台 Agent；只有 API 进程时任务会保持排队。无需 `SERVICE_KEY` 或内部 Go→Python HTTP 协议。`/healthz` 检查平台数据库，`/livez` 为进程存活检查。

## 验证、评测与恢复

测试数据库必须是专用环境，允许创建和删除随机测试数据库/账号；生产 DSN 不得用作 `TEST_DATABASE_URL`。

```powershell
$env:PYTHONUTF8='1'
# 配置 TEST_DATABASE_URL；运行新平台、图持久化和原查询内核回归。
.venv/Scripts/python.exe -m pytest agent/tests -q
# 可选完整 Olist 和真实备份恢复验证：另配置 OLIST_TEST_CSV_DIR、PG_TEST_BIN。
Set-Location web
npm run build
```

独立分析评测默认零模型调用，只审计题集。A 单步 / B 自适应 / C 固定流程均使用新 Python 平台、相同 PG 数据与授权，不混用旧 Go/MySQL 的延迟作为 Agent 收益证据。

```powershell
.venv/Scripts/python.exe scripts/run_analysis_study.py
# 确认模型、价格和题集后，显式启用真实调用；上限按每组每夹具计算。
.venv/Scripts/python.exe scripts/run_analysis_study.py --with-model --split acceptance --repeats 3 --max-model-calls 500
```

题集为 80 表达 / 40 意图组、标注仍为 draft。当前确定性工具和故障测试不能证明真实模型完成率提升；真实模型实验及人工题集复核单独验收。历史解析/多轮实测见[解析证据](docs/user-understanding-evidence.md)、[多轮证据](docs/multiturn-evidence.md)，均属于旧平台基线。

备份和迁移操作见[Python 平台运维](docs/python-platform-operations.md)。所有三个权限域必须来自同一备份快照；恢复只允许空目标库，恢复后重建可信数据源映射和账号密钥。经用户单独授权，新版已切换至 ECS 私有入口并启用备份定时器，结果见[ECS 部署验收](docs/python-platform-ecs-validation.md)；未推送远端 Git。

旧 Go/MySQL 本地配置保留在 `deploy/compose.legacy.yaml` 和 `deploy/legacy.env.example`；现有 `compose.private/cloud`、旧 release 脚本及其线上记录属于历史发布链，不能直接用于新平台发布。新版私有部署使用 `compose.yaml` 与 `deploy/compose.python-private.yaml`，当前版本及验收结果见上述 ECS 记录。
