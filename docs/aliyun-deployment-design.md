# 阿里云 ECS 部署要求

日期：2026-10-10。

状态：用户已明确后续部署到阿里云，并选择云主机 + Docker Compose。平台要求纳入实现约束；资源选型与技术方案待确认，尚未创建资源或完成云端验收。

## 部署基线

采用阿里云 ECS Linux 主机运行 Docker Compose，复用本地 API、agent、web 三个镜像；首批以 linux/amd64 为镜像验证基线。若选择 ARM ECS，需另构建并验证目标架构。

建议应用与 RDS、ACR、SLS 使用同一地域，ECS 与数据库处于同一 VPC。数据库优先采用 RDS PostgreSQL 和 RDS MySQL，具体系列、容量、地域及费用需结合用户资源确认。现有 Linux 回归版本为 PostgreSQL 17、MySQL 8.4，目标 RDS 优先保持相同大版本；阿里云已公开支持这两个版本，所选地域/系列的实际可购版本与兼容性仍需验收。[PostgreSQL 17](https://help.aliyun.com/zh/rds/apsaradb-rds-for-postgresql/apsaradb-rds-for-postgresql-supports-postgresql-17)、[MySQL 版本及 SSL 支持](https://help.aliyun.com/zh/rds/apsaradb-rds-for-mysql/configure-a-cloud-certificate-to-enable-ssl-encryption)

RDS 是建议选型，不代表授权购买。若采用 ECS 自建数据库，另出持久化、独立备份与恢复方案，不能直接把本地 Compose 的 volume 当作云端灾备。

首批 HTTPS 入口建议使用 ECS 上的 Nginx，反向代理到仅绑定 127.0.0.1 的 web:8088；后续使用 ALB 时增加独立配置，限定受信任的代理来源。单台 ECS 有应用主机单点，不能据此宣称达到 99.9% 高可用。

## 网络与入口

ECS 安全组按实际入口放行 HTTPS 443；80 仅按跳转或证书验证需要开放；SSH 22 限定运维来源。应用 API/agent、8088、数据库 3306/5432 不直接对公网开放。安全组控制 ECS 的入站与出站流量，还需核对 Compose 端口绑定和宿主机防火墙。[安全组规则](https://www.alibabacloud.com/help/en/ecs/user-guide/security-group-rules)

RDS 使用 VPC 内网 DNS 连接串，配置只允许应用/运维来源的数据库白名单或安全组授权。验证 ECS 到 RDS、ACR 以及模型服务的实际连通性，包括 DNS、证书和 HTTPS 出站；云主机上的应用不能依赖 Windows Clash、localhost 代理或本地绝对路径。

Nginx 终止 TLS 后，同源转发前端与 `/api/*`。`PUBLIC_ORIGIN` 为最终 HTTPS origin，Cookie 使用 Secure、HttpOnly、SameSiteStrict。SSE 禁止响应缓冲，空闲超时高于 10 秒心跳，初始配置至少 65 秒，并验证客户端断线续传。

登录限速的客户端 IP 只能从明确的可信代理链获得；直接请求携带的 X-Forwarded-For 不可信。实现时覆盖本机 Nginx 和可选 ALB 的代理边界，不能将所有代理用户归为同一个 IP，也不能允许伪造头绕过限速。

若选择中国内地地域向公网提供网站服务，将域名及备案准备纳入上线前置检查，按实际地域和主体确认；不预设地域或备案已完成。[阿里云备案说明](https://help.aliyun.com/zh/icp-filing/basic-icp-service/getting-started/quick-start-for-icp-filing-for-personal-websites)

## 数据库与密钥

PostgreSQL 使用 `sslmode=verify-full`，CA 文件只读挂载到 API 与迁移容器，DSN 的 sslrootcert 指向容器路径；MySQL 使用配置中的 ssl_ca，并启用证书及连接主机名验证。为实际使用的 RDS 内网连接地址配置 SSL 保护，下载对应证书；连接 DNS 必须与证书匹配，验证失败不可通过关闭校验绕过。[PostgreSQL SSL](https://help.aliyun.com/zh/rds/apsaradb-rds-for-postgresql/configure-ssl-encryption-for-an-apsaradb-rds-for-postgresql-instance)、[MySQL SSL](https://help.aliyun.com/zh/rds/apsaradb-rds-for-mysql/configure-a-cloud-certificate-to-enable-ssl-encryption)

RDS 产品对 SSL 的系列、存储类型及地址保护存在限制，购买前核对所选实例；证书轮换纳入维护和重连验收。当前 PostgreSQL 云端证书配置文档不支持 Serverless，不能直接将其作为本方案的可替换实例。[配置前提](https://help.aliyun.com/zh/rds/apsaradb-rds-for-postgresql/configure-ssl-encryption-for-an-apsaradb-rds-for-postgresql-instance)

元数据迁移与运行身份分离；分析账号只读授权视图；导入管理员凭据只供一次性运维使用。RDS 管理身份不等同本地 root/superuser，必须验证建库、用户、视图和最小授权流程，不将本地管理权限假设带入 RDS。

首批可由运维将凭据放入 ECS 受限配置文件后注入对应容器，文件不入 Git，禁止打印完整 Compose 配置或环境。Compose 环境注入不能称为已接入 KMS；后续采用凭据管家时单独实现启动注入与轮换。应用访问云 API 时优先使用最小权限的 ECS RAM 角色和临时凭证，不在镜像内存放长期 AccessKey。[ECS RAM 角色凭证](https://help.aliyun.com/zh/cli/ecs-ram-role)

部署到阿里云不强制更换模型供应商。现有兼容接口继续通过 LLM_BASE_URL、LLM_MODEL、LLM_API_KEY 配置；只有选用其他模型服务时才验证该提供商的 JSON、用量和超时契约。

## 镜像、日志与发布

使用 ACR 私有镜像仓库，地址从实际实例控制台获取；部署记录三个镜像的不可变 digest、代码版本及迁移版本。ECS 只获拉取所需权限，CI 推送凭据与运行身份分离。仓库版本按容量、访问方式及预算确认，不预设个人版拥有企业版的性能保障。[ACR 版本差异](https://help.aliyun.com/zh/acr/product-overview/differences-between-personal-edition-instances-and-enterprise-edition-instances)

构建依赖、基础镜像拉取和最终镜像部署分别验证，避免只测试 ACR 拉取成功就忽略构建节点访问依赖源失败。云端发布复用已验证镜像，不要求 ECS 临时从 Docker Hub 构建。

应用输出结构化 JSON 到 stdout/stderr，本地继续使用 docker compose logs；阿里云运行时按项目/容器标签采集至 SLS，并配置日志留存、查询权限及错误率、超时、磁盘等告警。采集部署参照官方 Logtail 方式，采集器所需宿主机/容器权限只赋予采集器；日志字段继续执行脱敏限制。[Docker 标准输出采集](https://help.aliyun.com/zh/sls/user-guide/collect-docker-container-standard-output)

Docker 日志轮换与 SLS 采集兼容性需验证；云平台告警与应用答案正确率分别统计。CPU、内存、磁盘、连接数及费用由实际云资源监测，当前租户运行接口不能替代云资源监控。

发布顺序为确认备份与回滚镜像 → 拉取新镜像 → 独立迁移并等待成功 → 更新应用 → HTTPS/登录/规则与模型问数/SSE 检查。应用回滚不自动撤销迁移；数据库变更需保持旧版本兼容或提供单独恢复步骤。

RDS 备份策略及保留期由实际实例配置，恢复到独立测试实例验证用户、权限、任务、事件和分析视图；ECS 快照不代替 RDS 备份。目标 RPO/RTO、维护窗口和具体资源规格在部署前确定并记录实测值。

## 对当前项目的实现要求

| 模块 | 必须落实 |
| --- | --- |
| Compose/环境模板 | 阿里云资源占位配置、PostgreSQL CA 挂载、MySQL CA、日志轮换、探针、资源限制及失败退出 |
| HTTPS 入口 | ECS Nginx TLS 配置、SSE、同源头、证书续期；ALB 作为独立可选部署配置 |
| Go API | JSON 日志、执行关联、可信代理及登录限速；不信任浏览器传入地域、租户或云凭据 |
| Python agent | JSON 遥测、RDS 证书验证、错误分类；SQL 超时、只读与结果上限持续有效 |
| CI/发布 | linux/amd64 镜像验证、ACR 地址参数化、digest 发布、迁移与回滚步骤；实际推送另需授权 |
| 运维手册 | RAM/安全组/白名单、初始化、证书、SLS、备份恢复、目标环境压测及成本记录 |

现有 `deploy/compose.cloud.yaml` 是通用模板：目前仅挂载 MySQL CA，尚未提供 PostgreSQL CA 挂载、TLS 入口、云日志和可信代理的完整实现。后续应补齐并验证；文档更新不等于模板已适配完成。

## 云端验收

在目标 ECS/RDS 上验证迁移、Olist 导入、最小权限、六指标独立结果核对、遥测、跨租户隔离、HTTPS、登录限速、SSE 续传、容器/主机重启恢复、模型网络故障和证书错误。固定资源进行简单问数 P95 与并发测试；另做备份恢复，不将本地通过的测试替代云端结果。

地域、ECS 规格与架构、域名/备案、RDS 选型、ACR/SLS 实例、资源预算及现有资源清单尚待确定。现在可继续完成代码适配和本地验证；实际创建收费资源、镜像推送、开放公网与部署发布需按用户提供的目标及授权执行。
