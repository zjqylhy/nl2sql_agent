# 模型网关接入规格

2026-10-11。用户已确认以下服务器网关接入方案，代码与测试结果在交付时供人工验收。

## 需求

把现有模型出站请求从 AI 厂商直连切换为应用网关，再由网关转发。用户已确认使用服务器网关，SSH 隧道入口为 `https://localhost:9443/v1/chat/completions`，逻辑模型为 `chat`，认证为 `Authorization: Bearer <应用 Key>`。保留现有规则优先、模型兜底及查询权限校验。

应用 Key 保存到 Windows 用户环境变量 `NL2SQL_AGENT_API_KEY`，不写入代码、示例、提交或日志。当前已完成保存；运行 Compose 的进程需从用户环境重新读取，因为已有进程不会自动获得后续新增的用户变量。

## 已检查的现场

- 已确认 `127.0.0.1:9443` 由 `ssh` 进程监听。本机及应用镜像容器经宿主机地址访问该入口，TCP/TLS 均可达，TLSv1.3。
- 网关使用 Caddy CA，逻辑 TLS 身份为 `localhost`。已从 Windows 信任库导出能够验证服务器入口的公开根 CA，存放于 Git 忽略的 `.cache/gateway-server-ca.pem`。未导出私钥，未关闭应用 TLS 校验；此文件与此前本机 8443 网关的 CA 分开保存。
- 已使用保存的 Key 从应用镜像调用该服务器入口。仅发送 `model/messages/max_tokens` 时返回 HTTP 200，存在 `choices`、`usage`，JSON 回复解析正确，`finish_reason=stop`；请求输出上限为 64 Token。
- 同时携带 `temperature=0` 和 `response_format={"type":"json_object"}` 时，两次均返回 HTTP 400、`unsupported_capability`，消息为 `no configured model supports this request`。不能将现有厂商请求参数直接原样转给该网关，也不能宣称服务器支持强制 JSON 输出模式。
- 当前真实验证合计三次：一个成功的最小格式请求、两个不支持能力的请求；应用完整问数链路尚未验收。本机 8443 网关不作为本项目的出站目标，不修改其服务。

实现阶段另发现：只带最小字段但要求 800 Token 也会触发 `unsupported_capability`。系统消息加 64 Token 请求成功；实际单指标提示加 256 Token、组合计划提示加 512 Token 均成功。由此增加可配置的输出预算，默认单指标 256、组合计划 512，适配已验证的网关路由；截断和结构不完整仍明确失败，具体端到端结果在验证记录中更新。

## 接入方案

1. 使用该网关已经实测通过的 Chat Completions 格式：只发送 `model/messages/max_tokens`，不发送 `response_format` 或 `temperature`。两条模型路径继续通过系统提示要求 JSON，严格解析返回内容并校验结构、指标权限和原始约束；无效 JSON 明确失败，不放松验证。逻辑模型设置为 `chat`，由网关决定实际厂商与模型；应用不保存厂商 Key，不在网关失败时回退厂商。
2. `LLM_BASE_URL=https://localhost:9443/v1`，`LLM_MODEL=chat`。Compose 把宿主 `NL2SQL_AGENT_API_KEY` 注入容器现有 `LLM_API_KEY`，兼容评测/发布校验读取的运行时配置；不得继续沿用本地 `.env` 中旧的厂商 Key 或厂商 URL。示例配置采用网关入口与变量引用，不放真实密钥。
3. 抽取两条模型路径共用的 HTTP 配置，增加可选 `LLM_CONNECT_HOST` 与 `LLM_CA_FILE`。Windows 宿主直接运行时不配置连接覆盖；Docker Desktop 本地覆盖为 `host.docker.internal`，CA 只读挂载进 Agent。
4. 逻辑请求目标继续是 `localhost`；Docker 连接时仅把 TCP 目标改为 `host.docker.internal`，HTTP `Host` 与 TLS SNI/证书身份校验仍是 `localhost`。连接覆盖只允许原逻辑地址为回环入口，生产网关使用其真实 HTTPS DNS 名，不套用本地覆盖。[HTTPCore 的 SNI 扩展](https://www.encode.io/httpcore/extensions/#sni-hostname)支持分离连接地址与证书身份。
5. 严格校验 HTTPS、证书链与主机名；使用显式 SSLContext 加载网关公开 CA，保持现有 20 秒模型请求超时和不追随重定向。模型请求不隐式经过环境中的 HTTP 代理，避免本地请求误路由。[HTTPX SSL 配置](https://www.python-httpx.org/advanced/ssl/)
6. 本地宿主连接/CA 配置放在独立 Compose 覆盖文件，云模板接收网关 URL、`chat` 与应用 Key。云上若使用私有 CA，显式添加该环境的 CA 挂载；不得把 Windows localhost、宿主地址或本机 CA 当成 ECS 通用配置。

## 验收与交付

- 单元测试覆盖单指标和组合计划两条模型路径的网关 URL、`chat`、Bearer 认证、最小请求字段、JSON 回复校验、超时、重定向与证书配置；必须确认不会携带已导致 `unsupported_capability` 的两个字段。
- 验证连接覆盖后仍使用原 HTTP Host 和 TLS 身份；不匹配证书、无效配置和缺失 Key 必须明确失败，日志与响应不泄露 Key。
- 规则命中不调用网关；保留现有 SQL 安全、权限、遥测及错误处理测试。
- 目标入口与 Key 已实测通过；方案确认后进行少量真实网关验收：结构化意图、单指标、组合指标及 SSE。记录实际成功/失败与调用次数，不把 Mock 测试当作真实网关验收。
- 本地构建并只重建受影响的应用服务，保留数据库卷；检查运行容器确实使用网关配置，旧厂商配置不再承担模型出站。
- 代码与测试验证后提交独立 commit，交付运行结果，供用户完成代码与测试节点的人工验收。不推送 Git、不修改网关项目或服务器资源。

实施与验证结果见 [网关操作说明](model-gateway-operations.md)及 [验证记录](validation.md)。
