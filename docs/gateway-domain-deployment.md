# 模型网关域名切换发布记录

2026-10-21，按用户分别确认的[需求和技术方案](gateway-domain-spec.md)完成部署，人工验收待确认。

远程 API、Worker 现使用 `https://api.linhengyi.xyz/v1`，实际模型生成目标为 `/v1/chat/completions`。原 Key、逻辑模型 `chat`、调用预算、输出额度和超时均保持不变。

## 发布内容

- 部署配置提交：`2da55e20d4c57d108237a68cbcea814bb9dabb08`。远程 Compose 删除专用 `gateway` 服务、API/Worker 的隧道启动依赖、`LLM_CONNECT_HOST`、`LLM_CA_FILE` 及私有 CA 挂载。
- API/Worker 继续使用 `push-analysis-platform:a43cf6f9a316c0145cca8cd54658d50782beb349`，Web 继续使用 `olist-python-web:9273ce6e3385636295d29849d883c655412b683e`。没有构建或更换应用镜像，没有数据库迁移。
- 发布源码 `agent/` 和 `scripts/` 与之前运行的发布逐字节一致。配置入口 `current` 指向新提交目录，`APP_TAG`、`SOURCE_COMMIT` 仍记录原后端镜像版本，`WEB_TAG` 仍记录原前端镜像版本。
- 远程环境的 `LLM_BASE_URL` 改为新域名，并清除仅用于旧隧道的 `TUNNEL_IMAGE`、`TUNNEL_KEY_FILE`、`GATEWAY_KNOWN_HOSTS_FILE`、`GATEWAY_CA_FILE`。实际运行进程不再包含连接覆盖或自定义 CA，也没有秘密文件挂载。
- 原隧道容器在切换验证通过后停止并移除；旧密钥材料和环境备份仅留在服务器用于回退。Windows 本地的独立 SSH 配置仍有使用场景，未退役。
- 服务器既有 `/srv/olist/python/ops.py` 原先按 `SOURCE_COMMIT` 查找旧 Compose，会继续要求已移除的隧道变量。本次保存原脚本后，将配置目录选择改为 `RELEASE=(ROOT/'current').resolve(strict=True)`，并核对其 Compose 项目仍是 `olist-python-agent`。镜像版本与部署配置目录各自使用对应来源。

发布时先确认无活动任务，将域名入口暂时切为维护页；停止 API 后再次确认无活动任务，才停止 Worker。重建 API/Worker 后重新加载现有 Web 的 Nginx，验证成功再恢复正式入口。Web 和数据库容器均未更换，可信代理地址保持原值。

## 验证结果

| 验证 | 结果 |
| --- | --- |
| 现有网关传输、预算及秘密配置回归 | 27 项通过，0 失败 |
| 候选 Compose | 无需旧隧道变量即可解析，API/Worker 使用新地址且没有旧依赖或 CA 挂载，镜像引用保持原值 |
| 新网关 DNS / TLS | 解析到 `47.113.231.252`；API/Worker 使用标准可信 CA 与主机名校验通过 |
| 实际进程只读鉴权 | `/v1/models` 未认证为 401，使用原 Key 为 200，并包含原逻辑模型 `chat` |
| 移除旧隧道后的复验 | API/Worker 各自使用实际运行配置访问新域名，只读鉴权均为 200 |
| 应用首页、健康、登录、历史读取、退出 | 均为 200；安全 Cookie 保持生效，退出后身份接口为 401 |
| 业务状态 | 发布前后 9 张业务表的计数和内容摘要一致，共 110 个历史任务；验收仅产生正常登录会话和限速记录 |
| Key、模型及其他配置 | 对比保存的环境确认未变化；没有将密钥写入本机、日志或 Git |
| 运维与备份 | 运维包装器配置校验通过；备份定时器 active，备份服务工作目录跟随当前发布 |
| 网关请求范围 | 共 10 次只读模型列表 HTTP 请求，真实模型生成请求为 0 |

初次本地沙箱测试为 22 通过、1 失败、4 环境错误，原因涉及回环套接字及临时目录权限。允许所需回环测试并改用项目临时目录后，完整的 27 项回归通过；没有通过关闭 TLS 校验处理失败。

当前网关证书到期时间为 `2027-01-18 20:36:15 UTC`。本次验证连接、鉴权、配置和应用读取，不验证实际生成结果、生成响应格式、网关路由的输出额度或费用；没有消耗历史任务预算或启动历史任务续跑。

脱敏技术证据见[发布摘要](evidence/gateway-domain-20261021.json)。生产业务正文、证书私钥及完整环境留在服务器，未导出本机。

## 后续运维及回退

- 远程配置由 `LLM_BASE_URL` 决定，通过 Compose 的基础环境注入 API/Worker；新域名不需要 `LLM_CONNECT_HOST` 或私有 CA。不得重新添加旧连接覆盖，否则现有程序会拒绝对公网域名应用回环覆盖。
- 改配置后需要重建 API/Worker，普通 restart 不会更新容器环境。使用当前发布的 Compose 和既有项目名，不从后端镜像版本推导配置目录。
- API 地址变化后重新加载 Web Nginx；Web 地址变化则重新核对精确可信代理配置。本次只重新加载 Web，没有重建它。
- 回退材料在 `/srv/olist/python/deployments/20261021-gateway-domain`：`rollback.env`、`rollback-Caddyfile`、`rollback-ops.py` 及此前发布目录。原隧道镜像及受限密钥材料仍在服务器。
- 回退时在无活动任务的窗口暂停入口和 API/Worker，恢复保存的环境及此前 `9273ce6...` 配置目录，用其 Compose 重建 `gateway`、API/Worker，刷新 Web Nginx 并核对原入口。按需恢复原运维包装器，不能只恢复 URL 而遗漏隧道、CA 或依赖配置。
- 本次成功发布后未执行实际回退或服务器重启，不删除数据库、任务证据、密钥材料或备份。
