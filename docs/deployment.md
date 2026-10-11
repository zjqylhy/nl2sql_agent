# 本地与云部署

2026-10-10：用户明确云端目标为阿里云 ECS + Docker Compose。后续实现须遵循 [阿里云部署要求](aliyun-deployment-design.md)；云模板已补齐双数据库 CA、宿主 TLS 入口、探针、资源限制、日志轮换和可信代理配置，尚未在阿里云验收。Kubernetes 保留为后续参考，当前交付优先 ECS 路径。

2026-10-11：继续部署准备的需求与技术方案见 [阿里云发布规格](aliyun-release-spec.md)，覆盖实际镜像评测、ACR 发布、迁移/回滚、RDS 初始化、就绪检查与运维验收。技术方案待人工确认，本批尚未开始代码实现。

## 本地部署（Windows）

项目目录：`C:\Users\LHY95\Documents\Codex\nl2sql_agent`。本地容器部署入口为 http://localhost:8088；需要 Docker Desktop 的 Linux containers 模式。

```powershell
Set-Location C:\Users\LHY95\Documents\Codex\nl2sql_agent
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# 首次启动前编辑 .env，替换密码与 SERVICE_KEY 占位值。
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

首次还需导入 Olist、填写数据源映射、创建管理员账号，完整命令见 [README：Windows 本地启动](../README.md#windows-本地启动)。仅启动容器不会自动创建账号或导入数据。

| 本地方式 | 浏览器入口 | 说明 |
| --- | --- | --- |
| Docker Compose | http://localhost:8088 | 前端为唯一主机入口，数据库与分析服务在容器内网 |
| 源码调试 | http://localhost:5173 | Vite 前端代理 Go :8080；Python :8000，分别启动 |

2026-10-10 初次功能验收使用本机原生进程及临时数据库，入口为 :5173；验收后已全部关闭。同日修复 Docker Desktop 与代理连接后，已完整构建本地镜像并启动 Compose，:8088 页面和 `/healthz` 均返回 HTTP 200。容器环境已核对 Olist 导入并完成单次真实模型查询，完整回归和人工验收尚未完成。详见 [验证记录](validation.md)。

```powershell
# 查看日志或停止本地容器；down 不删除数据库 volume。
docker compose logs --tail 100 api agent web
docker compose down
```

### Docker Hub 认证连接超时

若构建提示 `failed to fetch anonymous token`，并在访问 `auth.docker.io` 时超时，先检查网络与代理。Docker Desktop 的引擎代理和执行 Compose 的终端代理需要分别确认；本机排查中，引擎拉取成功，但 Buildx 获取令牌仍直连，设置终端代理后 API 镜像构建通过。

Docker Desktop 在 Settings → Resources → Proxies 中使用系统代理或正确的手动代理。然后在执行构建的 PowerShell 窗口设置 HTTP/HTTPS 代理；下面的 `33210` 是本机 Clash 当前端口，其他机器应替换为实际 HTTP 或 mixed 端口：

```powershell
$env:HTTP_PROXY = 'http://127.0.0.1:33210'
$env:HTTPS_PROXY = 'http://127.0.0.1:33210'
docker compose up -d --build
docker compose ps
```

这些环境变量只影响当前终端及其子进程，新终端需重新设置。不要将本机代理地址写入应用镜像或云部署配置。Docker Desktop 代理设置见 [官方说明](https://docs.docker.com/desktop/settings-and-maintenance/settings/#proxies)。

## 部署结构

代码不依赖本机路径或云厂商 SDK。浏览器只访问同源 Nginx；Go API 访问 PostgreSQL；Python 私网服务访问授权 MySQL 视图与 HTTPS 模型接口。正式环境使用托管数据库、镜像仓库、受控 Secret 与 TLS 入口。

| 配置 | 本地 | 云端 |
| --- | --- | --- |
| 镜像 | Compose 本机构建 | CI 构建/推送，不可变 tag/digest |
| 元数据 | PostgreSQL 17 | 托管 PostgreSQL，运行账号无 DDL |
| 分析库 | MySQL 8.4 | 托管 MySQL 8，视图专用只读账号 |
| Cookie / 入口 | localhost HTTP / 非 Secure | HTTPS / Secure、HttpOnly、SameSiteStrict |
| 数据库连接 | Compose 内网 | 私网安全组 + 证书/主机名验证 |
| 模型 | 可无密钥，标注规则解析 | Secret 注入 key 和 HTTPS endpoint |
| 文件 | 导入时读取 CSV | 一次性运维 Job，运行时不需要 CSV |

## 云主机 Compose

1. 准备私网托管数据库、镜像仓库、TLS 代理或负载均衡器，以及数据库初始化运维环境。数据库、API、分析服务不向公网开放。
2. 构建并推送三个镜像；地址只是模板。

```powershell
docker build -f deploy/Dockerfile.api -t REGISTRY/olist-api:VERSION .
docker build -f deploy/Dockerfile.agent -t REGISTRY/olist-agent:VERSION .
docker build -f deploy/Dockerfile.web -t REGISTRY/olist-web:VERSION .
# 登录自己的仓库后推送以上版本。
```

3. 复制 `deploy/cloud.env.example` 到受控 `.env.cloud`，替换占位值。PostgreSQL 使用 `sslmode=verify-full`；`POSTGRES_CA_FILE` 指定宿主 CA 文件，模板自动挂载至 API/迁移容器；DSN 的 `sslrootcert` 为 `/run/secrets/postgres-ca.pem`。模型与数据库 DNS 必须可解析。
4. MySQL 映射配置 `ssl_ca: "/run/secrets/mysql-ca.pem"`；`MYSQL_CA_FILE` 填宿主机数据库厂商 CA 的绝对路径。程序验证证书及主机名，不能随意把 DNS 替换成 IP。
5. 在私网运维环境安装锁定依赖，按 README 导入真实数据，建立受控角色映射。初始化管理账号不注入长驻 API/agent。
6. 校验并启动，不打印解析后的密码。

```bash
docker compose --env-file .env.cloud -f deploy/compose.cloud.yaml config --quiet
docker compose --env-file .env.cloud -f deploy/compose.cloud.yaml up -d
```

迁移使用独立 `MIGRATION_DATABASE_URL`，完成后 API 才启动。迁移按文件版本记录 SHA256，在事务与锁下串行执行；禁止改写已应用版本，通过新增 SQL 演进。应用回滚不会自动撤销破坏性 schema 变更。

7. 私网一次性执行 `api -command bootstrap`，stdin 提供管理员密码。可由专门初始化身份执行，不需要把 DDL 权限赋给运行账号。
8. TLS 代理将同域名 `/` 和 `/api/*` 转发到 web:8080。`PUBLIC_ORIGIN` 必须是最终 HTTPS origin（无路径/尾斜杠）。跨机器负载均衡时 `BIND_HOST` 设私网地址，安全组只允许负载均衡来源；本机 TLS 代理保持 127.0.0.1。
9. SSE 上游禁用缓冲，空闲超时大于心跳 10 秒（建议至少 65 秒）；入口配置登录限速、请求体限制、HSTS。API 仅信任 `TRUSTED_PROXY_CIDRS` 中的精确代理地址，从右向左回溯 X-Forwarded-For；宿主 Nginx 覆盖公网传入的代理链。云模板默认只信任 web 地址与宿主网关；变更 Docker 网段/转发拓扑时同步调整，并现场核对真实 IP。未设置时仍按直接连接 IP 限速。

PostgreSQL 最小运行授权示例（由管理员执行，替换实际 schema/身份）：

```sql
GRANT CONNECT ON DATABASE platform TO platform_runtime;
GRANT USAGE ON SCHEMA public TO platform_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON tenants, users, sessions, tasks, task_events, task_outbox, task_attempts TO platform_runtime;
-- 无 schema_migrations 权限，无 DDL；检查默认 PUBLIC schema 权限。
```

容器以非 root、只读根文件系统运行，临时文件在 tmpfs。日志不记录请求体、Cookie、密钥或 DSN。

ECS 宿主入口配置见 `deploy/nginx.ecs.conf`，在 Nginx 的 `http {}` 内加载，替换域名与证书路径。证书文件由宿主续期；先 `nginx -t` 再 reload。默认只发布 80/443，8088 保持回环地址，安全组不开放数据库/API/agent。云模板默认 API/agent 各 512 MiB、web 128 MiB 与对应 CPU 上限，仅为起始配置，需结合 ECS/RDS 资源压测调整。Docker 日志每服务 3×10 MiB 轮换；SLS 采集需在实际云环境接入。

### 执行统计与清理

管理员登录后可同源读取 `/api/operations/summary?days=7`（支持 1/7/30 天），普通角色返回 403。统计按会话租户汇总，分别列任务和每次 attempt、重试、错误、已知 Token/未知用量、各耗时 P50/P95 与样本数；不提供问题/SQL 明细。

```bash
# 默认只删除结束超过 30 天的 attempt，每次至多 1000 条；不删除任务或事件。
docker compose --env-file .env.cloud -f deploy/compose.cloud.yaml \
  run --rm --no-deps api -command prune-attempts
```

运维定期显式执行清理，按返回 deleted 数判断是否还需分批执行。版本 002 新增表需在新版 API 启动前迁移，并将 `task_attempts` 授权给运行账号；云 Compose 已按迁移成功 → agent 健康 → API 健康 → web 的依赖启动。重启不会自动降级数据库迁移。

Agent 发布准入与本地评测命令见 [评测与变更准入](agent-quality-gate.md)。代码评测报告不等于发布镜像或阿里云实测证明。

## Kubernetes

`deploy/kubernetes.yaml` 含 API/agent/web 双副本、ClusterIP、TLS Ingress、入站 NetworkPolicy、独立迁移 Job、资源限制和探针。它是待配置模板，不能直接宣称线上可用。

1. 创建 `olist` namespace、`olist-config` ConfigMap：PUBLIC_ORIGIN、LLM_BASE_URL、LLM_MODEL。密码不放 ConfigMap。
2. 创建 `olist-secrets`：DATABASE_URL、MIGRATION_DATABASE_URL、SERVICE_KEY、TENANT_DATASOURCES、可选 LLM_API_KEY；`mysql-ca` Secret 包含 `ca.pem`，映射使用 `/run/secrets/mysql/ca.pem`。配置静态加密、RBAC、轮换，实际 Secret YAML 不提交 Git。
3. 替换镜像、域名、Ingress class；配置 ingress controller 与 `olist-web-tls` Secret。NetworkPolicy 要求支持它的 CNI；安全组限制数据库及出口。
4. 单独执行 `platform-migrate` Job，等待成功再滚动 Deployment。整份 YAML 的资源顺序不能保证迁移完成。每次发布使用新 Job 名称。
5. API 只获运行 DSN/服务密钥，agent 只获数据源/模型密钥，web 不接触数据库密钥。初始账号通过一次性 Job 创建。
6. 先 `kubectl apply --dry-run=server`，再验收探针、登录/问数、SSE 跨实例续传、越权、停止、滚动重启与故障恢复。

Go 用数据库租约与执行版本抢占任务，多实例共享 50 活跃/100 排队额度，同用户一个执行任务。M1 使用 PostgreSQL worker/outbox，尚未完成 Celery/LangGraph。

## 运营与验收边界

启用托管库备份/PITR、恢复演练、容量/错误告警，固定资源压测。更新先备份/迁移再切镜像，数据源变更重启 agent。过期 sessions、任务/事件需明确保留策略。

M1 限制：分析调用 35 秒，SQL 10 秒，任务累计 3 次执行且首次开始后 5 分钟；模型输出旧 Intent 800、扩展 QueryPlan 1500 tokens，结果 1000 组/2 MiB。1001 行内部探测超限后拒绝；Top N 必须明确要求。提供商费用预算及节点预算未完成。停止禁止后续写回和重试，Go 中断等待；已送入 MySQL 的查询可能继续到 10 秒上限，分布式主动取消是 M2 验收项。

本机已验证 Linux 镜像、Compose 启动、真实 PostgreSQL/MySQL、规则与模型解析和浏览器查询；未部署云账号。agent 的 Dockerfile 复制整个 runtime，包含版本化语义目录；云部署复用这套镜像，不需额外语义服务。TLS、Ingress、多副本滚动、灾备、P95 和 99.9% 必须在目标环境实测。
