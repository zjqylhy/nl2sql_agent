# 模型网关操作说明

2026-10-21：远程 API/Worker 已改用 `https://api.linhengyi.xyz/v1`。远程 Compose 移除旧 SSH 隧道依赖及私有 CA 挂载，使用标准 HTTPS 证书校验；验证结果及回退见[域名切换发布记录](gateway-domain-deployment.md)。下文 Windows 本地隧道方式继续独立适用。

当前 `use_gateway.ps1` 与 `compose.gateway-local.yaml` 作用于 Python API/Worker；下文旧独立 `agent` 服务命令属于历史版本。服务器部署使用 `compose.python-private.yaml`，保留 CA 与主机名校验。

当前 Windows 通过 SSH 隧道访问服务器网关：`https://localhost:9443/v1`，逻辑模型 `chat`。9443 应由 SSH 监听；本机 Docker 网关的 8443 不属于本项目模型入口。

## 本地 Compose

Key 存放在 Windows 用户环境变量 `NL2SQL_AGENT_API_KEY`。在运行 Compose 的 PowerShell 进程重新读取，CA 使用服务器网关的已信任公开根证书；不要导出网关私钥或关闭 TLS 校验。

推荐直接运行脚本，自动读取用户变量、核对配置、重建 Agent 并等待就绪，结束后恢复进程环境：

```powershell
./scripts/use_gateway.ps1 -Build
```

CA 不在默认位置时添加 `-CaFile '<公开根 CA 文件>'`。镜像已经构建时可省略 `-Build`。等价手动命令如下：

```powershell
$env:NL2SQL_AGENT_API_KEY = [Environment]::GetEnvironmentVariable('NL2SQL_AGENT_API_KEY', 'User')
if ([string]::IsNullOrWhiteSpace($env:NL2SQL_AGENT_API_KEY)) { throw '缺少网关应用 Key' }
$env:GATEWAY_CA_FILE = (Resolve-Path '.cache/gateway-server-ca.pem').Path
docker compose -f compose.yaml -f deploy/compose.gateway-local.yaml config --quiet
docker compose -f compose.yaml -f deploy/compose.gateway-local.yaml up -d --build --no-deps --force-recreate agent
$env:NL2SQL_AGENT_API_KEY = $null
$env:GATEWAY_CA_FILE = $null
```

不要输出完整 `docker compose config`、容器环境或 Key。Key 轮换后必须重新读取用户变量并重建 Agent；普通 `restart` 不更新容器环境。

Compose 会将应用 Key 映射为容器的 `LLM_API_KEY`。本地覆盖设置 `LLM_CONNECT_HOST=host.docker.internal`，连接宿主机 SSH 隧道；HTTP Host、TLS SNI 和证书身份仍是 `localhost`。公开 CA 只读挂载为 `/run/secrets/gateway-ca.pem`，由 `LLM_CA_FILE` 指定。覆盖只能用于逻辑地址为回环地址的本机隧道。

应用两条模型路径只发送 `model/messages/max_tokens`。网关当前的路由不支持同时请求 `temperature` 和 `response_format`；因此由提示要求 JSON，应用继续严格验证 JSON schema、指标权限、筛选与时间约束。实测最小请求的 800 Token 输出额度也会被网关拒绝，256 Token 单指标与 512 Token 组合计划通过；默认分别采用 256/512，可用 `LLM_INTENT_MAX_TOKENS`、`LLM_PLAN_MAX_TOKENS` 调整为实际网关支持的额度。输出被截断时仍明确失败，不回退厂商、不放宽 SQL 校验。

## 宿主源码运行

Python 不在容器内时，`LLM_BASE_URL=https://localhost:9443/v1`，`LLM_MODEL=chat`，将用户变量映射为进程 `LLM_API_KEY`；`LLM_CONNECT_HOST` 留空，`LLM_CA_FILE` 指向本机信任的网关公开 CA。Key 不写到 `.env`。

## 云部署与 CI

ECS/Kubernetes 和 CI 使用其网络能访问的真实网关 HTTPS DNS。`NL2SQL_AGENT_API_KEY` 保存为该环境的 Secret，映射到 Agent/评测进程的 `LLM_API_KEY`。阿里云镜像工作流读取 GitHub Environment `aliyun-release` 的 `NL2SQL_AGENT_API_KEY` Secret，保留网关 URL 和模型到评测清单的绑定。

公有 CA 网关无需额外 CA 挂载；私有 CA 显式只读挂载，配置 `LLM_CA_FILE`。不沿用 Windows 端口、连接覆盖或本机 CA。新增网关请求适配会改变运行时配置哈希，旧版本的发布评测包不能直接复用。

## 故障判断

- 401/403：检查当前网关的应用 Key 与授权，日志分类为 `model_auth`。
- 400 `unsupported_capability`：检查额外能力及输出 Token 额度是否受当前路由支持。
- 429：网关限流，日志分类为 `model_rate_limit`。
- TLS/连接错误：检查 SSH 隧道、服务器 CA、证书身份和容器到宿主机的连通性；不关闭验证。
- 无效 JSON 或约束不一致：请求明确失败，SQL 不执行。规则完整命中时不调用网关。

需求与已确认方案见 [网关接入规格](model-gateway-spec.md)。
