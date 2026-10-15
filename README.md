# Olist 经营分析工作台

分支协作遵循 [master 保护规则](docs/branch-policy.md)：新建分支 push 后提交 PR/MR 合入 `master`；本地拦截与 GitHub 远端规则分别配置。

已实现 M1：Go API、Python 受限查询服务、React 工作台，支持账号/租户、六项指标、历史任务、持久化 SSE、权限撤销、任务停止和受控 SQL。需求与分期见 [需求规格](docs/spec.md)，结构见 [技术方案](docs/architecture.md)。

规则优先解析常见问题，完整命中时不调用模型、Token 为 0；支持范围内的陌生表达在配置模型后由 LLM 兜底。两条路径生成统一 QueryPlan，由版本化语义目录编译参数化 SQL。已支持一次 1–3 个指标、州/日/月分组、筛选、排序、Top N 和月趋势，详见 [语义查询规格](docs/semantic-query-design.md)。未配置模型、条件缺失或冲突时要求澄清。受控多轮已实现并在隔离环境验证，归因、报告与完整线上运营验收属于后续范围。

解析规则现仅接受完整受限句式，区分州包含、排除和取消筛选，支持小写州代码；未覆盖的否定或修饰表达交给模型理解完整原文，避免将“不包含 SP”反转成“只看 SP”。首轮和多轮的具体边界、独立题集及验证要求见 [解析可靠性方案](docs/query-parsing-reliability-spec.md)。

独立表达评测已增加 90 条问题、75 个意图组，并运行三轮真实模型同题对照：仅规则正确完成 30/60，模型加入后为 46/60、48/60、48/60，分别补足 16、18、18 道支持题。检查 QueryPlan、全部条件及独立 CSV 结果；保留拒答、失败和五道波动案例。题单仍待人工复核，统计不代表真实用户分布。详见 [实测证据与失败分析](docs/user-understanding-evidence.md)及[复现步骤](docs/user-understanding-evaluation.md)。

受控多轮在同一会话保存成功 QueryPlan 和独立澄清草稿，可说“最近半年 GMV 按月统计”→“改成按州”→“只看两个州，取前三名”，再补充州与排序。独立 30 链/100 问三轮真实模型评测均通过 100/100 问、30/30 链；无上下文对照均为 39/100 问、1/30 链。相同多轮实现仅规则为 80/100 问、20/30 链，模型补足十个陌生追问及十个后续继承轮次。题单仍为 draft；首次失败和重跑用量均保留。详见 [多轮实测](docs/multiturn-evidence.md)与[复现说明](docs/multiturn-evaluation.md)。

2026-10-14 已将多轮版部署到现有私有 ECS：迁移 003、最小运行授权、真实 HTTP/CSV 核对、重启后的草稿恢复及包含会话数据的双库恢复均通过，详见 [线上验收](docs/multiturn-ecs-validation.md)。

## Windows 本地启动

需要 Docker Desktop（Linux containers）、Docker Compose。源码调试另需 Go 1.27、Python 3.12、Node.js 24。数据库和数据均存本地，不提交 Git。

```powershell
Copy-Item .env.example .env
# 编辑 .env，替换三个密码/服务密钥占位值；随机十六进制值无需 DSN 转义。
docker compose config --quiet
docker compose up -d --build
```

访问 http://localhost:8088。首次还没有账号/数据源；完成下面步骤后才可问数。数据库仅在 Compose 内网开放，只有前端端口映射至本机。

生成随机值（每个变量分别生成）：

```powershell
$randomBytes = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($randomBytes)
([BitConverter]::ToString($randomBytes)).Replace('-', '').ToLowerInvariant()
```

### 数据和账号初始化

下载官方 [Olist 数据集](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce)，按其许可使用。脚本仅提取四个必需 CSV，并记录下载包 SHA256；需要 Kaggle 登录的环境请手动下载。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r agent/requirements.lock
.\.venv\Scripts\python.exe agent/tools/download_olist.py --output data/olist

# 管理密码应与 .env 中 MySQL 密码一致，只读密码另行生成。
$env:MYSQL_ADMIN_PASSWORD = [Net.NetworkCredential]::new('', (Read-Host 'MySQL 管理密码' -AsSecureString)).Password
$env:MYSQL_READONLY_PASSWORD = [Net.NetworkCredential]::new('', (Read-Host '新只读账号密码' -AsSecureString)).Password
docker compose run --rm -e MYSQL_HOST=mysql -e MYSQL_ADMIN_PASSWORD -e MYSQL_READONLY_PASSWORD -v "$((Get-Location).Path)/data/olist:/data:ro" agent python -m tools.import_olist --csv-dir /data --schema olist_alpha --readonly-user alpha_ro
Remove-Item Env:MYSQL_ADMIN_PASSWORD, Env:MYSQL_READONLY_PASSWORD
```

导入只创建新 schema，拒绝覆盖已有数据库。生产数据仅来自下载 CSV；测试夹具从不进入生产导入。导入先聚合商品和去重评价，生成订单事实和受控视图；只读账号仅获得视图 SELECT，不能读原表或写数据库。

将下面 JSON 作为 `TENANT_DATASOURCES` 的值写入本地 `.env`（单行）；每个启用的角色必须配置自己的可信映射：

```json
{
  "alpha:admin": {"host":"mysql","database":"olist_alpha","user":"alpha_ro","password":"替换只读密码","analysis_date":"2018-08-31"},
  "alpha:ops": {"host":"mysql","database":"olist_alpha","user":"alpha_ro","password":"替换只读密码","analysis_date":"2018-08-31"},
  "alpha:data": {"host":"mysql","database":"olist_alpha","user":"alpha_ro","password":"替换只读密码","analysis_date":"2018-08-31"}
}
```

示例选择 2018-08-31 作为历史分析基准，结果始终显示该日期。不同租户使用独立 schema/账号；受限角色可使用另一个带州过滤的 schema，导入时加 `--states SP,RJ`。浏览器不能指定数据源、租户权限或角色。

```powershell
docker compose up -d --force-recreate agent
# stdin 提供密码，避免写入命令参数与终端历史。
$bootstrapPassword = [Net.NetworkCredential]::new('', (Read-Host '管理员初始密码（12–72字节）' -AsSecureString)).Password
$bootstrapPassword | docker compose run --rm -T --no-deps api -command bootstrap -tenant alpha -user admin -role admin
$bootstrapPassword = $null
```

管理员可创建本租户账号、启停账号；指标授权通过 `PATCH /api/users/{id}` 提供。六指标：GMV、有效订单量、客单价、取消率、平均交付时长、平均评分。示例：“本月 GMV 按州统计”“上周有效订单量是多少？”。

### 规则解析与模型兜底

“本月 GMV 和有效订单量按州统计”“最近 6 个月 GMV 和客单价按月统计”“本月商品金额大于 100 的订单量”“本月 GMV 按州排名，取前 5 名”等完整规则表达，即使配置了模型也走规则，Token 为 0。“本月 GMV 和订单量，只看圣保罗州”等陌生表达可由模型兜底。六指标最多组合三个，金额筛选不含运费；不支持品类、跨表关联、同比/环比和自由 SQL。缺少时间、手动指标冲突、非法州代码或越权时直接澄清或拒绝。

模型请求通过网关转发。应用 Key 使用 Windows 用户环境变量 `NL2SQL_AGENT_API_KEY`，Compose 注入容器的 `LLM_API_KEY`；不使用厂商 Key。当前服务器入口为 `https://localhost:9443/v1`、逻辑模型 `chat`，通过本机 SSH 隧道访问；Docker 的连接覆盖和公开 CA 挂载见 [网关操作说明](docs/model-gateway-operations.md)。修改配置后需重建 agent。手动选择的参数约束模型结果；模型失败或无效 JSON 不回退厂商，也不执行未完整识别的 SQL。

### 源码调试

自行准备 PostgreSQL/MySQL，使用只读分析账号。在三个独立 PowerShell 终端设置环境变量。Compose 数据库不开放主机端口；混合调试需另加仅绑定 127.0.0.1 的开发 override。

```powershell
# Go 终端：另需设置 DATABASE_URL 和 SERVICE_KEY。
$env:PUBLIC_ORIGIN = 'http://localhost:5173'
$env:AGENT_URL = 'http://127.0.0.1:8000'
$env:COOKIE_SECURE = 'false'
$env:HTTP_ADDR = '127.0.0.1:8080'
go run ./cmd/api -command migrate
go run ./cmd/api

# Python 终端：配置相同 SERVICE_KEY 和 TENANT_DATASOURCES。
Set-Location agent
..\.venv\Scripts\python.exe -m uvicorn runtime.app:app --host 127.0.0.1 --port 8000 --reload

# 前端终端。
Set-Location web
npm ci
npm run dev
```

配置变更需重启服务；不要共享包含密码的环境、DSN 或 `docker compose config` 完整输出。

## 验证与云部署

```powershell
# 集成测试仅使用专用测试数据库，可创建/删除测试 schema 与账号。
# 配置 TEST_DATABASE_URL 和 MYSQL_TEST_HOST/PORT/PASSWORD 后运行数据库测试。
# 先关闭 npm run dev，避免 Windows 锁住 esbuild 阻止 npm ci。
.\scripts\verify.ps1
```

未配置数据库时脚本会明确跳过数据库集成测试。真实 Olist HTTP 验收见 `scripts/smoke.py --help`：要求基准 2018-08-31、第二租户只读 SP 视图，以及专用测试账号；密码由 `SMOKE_PASSWORD` 环境变量传入。脚本会临时修改测试运营账号权限并恢复。

组合查询的独立 CSV 核对：在项目根目录执行 `.\.venv\Scripts\python.exe scripts/verify_semantic.py`，要求本地 Compose、`data/olist` 原始 CSV、alpha/admin 全范围授权视图及基准 2018-08-31。默认八条规则问题不调用模型；追加 `--with-model` 会增加一次真实模型调用并记录 Token。核对指标值、样本、NULL、筛选、分组及 Top N；不修改账号权限。

[云部署步骤](docs/deployment.md)提供云主机 Compose 和 Kubernetes 两条路径，同一套镜像连接云托管 PostgreSQL/MySQL。[验证记录](docs/validation.md)列出已验证内容及未验证边界。

下一阶段的[问数质量与线上运行方案](docs/quality-operations-design.md)细化独立评测、逐次执行记录、租户统计及云主机 Compose 验收；需求和技术方案待人工确认，尚未实现。

当前 ECS 部署采用[自建双数据库与 SSH 私有访问方案](docs/ecs-private-deployment-spec.md)，运维见[私有部署手册](docs/ecs-private-operations.md)。RDS、ACR 和公网 HTTPS 的正式发布模板继续保留，后续按[阿里云部署要求](docs/aliyun-deployment-design.md)迁移和验收。
