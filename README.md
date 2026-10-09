# Olist 经营分析工作台

已实现 M1：Go API、Python 受限查询服务、React 工作台，支持账号/租户、六项指标、历史任务、持久化 SSE、权限撤销、任务停止和受控 SQL。需求与分期见 [需求规格](docs/spec.md)，结构见 [技术方案](docs/architecture.md)。

没有模型密钥时使用明确标注的规则解析；配置模型后只解析结构化意图，指标 SQL 仍由程序编译。归因、多轮、LangGraph/Celery、报告及完整线上运营验收属于 M2/M3。

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

[云部署步骤](docs/deployment.md)提供云主机 Compose 和 Kubernetes 两条路径，同一套镜像连接云托管 PostgreSQL/MySQL。[验证记录](docs/validation.md)列出已验证内容及未验证边界。
