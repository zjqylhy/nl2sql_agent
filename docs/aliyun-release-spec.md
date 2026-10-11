# 阿里云部署准备：需求与技术方案

日期：2026-10-11。

状态：用户同意继续补齐阿里云部署准备；本文件为待人工确认的技术方案，尚未开始本批代码实现。沿用 [阿里云部署要求](aliyun-deployment-design.md) 的 ECS Linux + Docker Compose 路线。

## 目标与交付范围

在已有云模板上补齐可重复发布、RDS 初始化、数据库就绪检查、HTTPS 维护及运维验收。首批目标为 linux/amd64、单台 ECS、RDS PostgreSQL 与 RDS MySQL；地域、规格、域名、ACR/SLS 实例和预算由实际资源参数提供。

本批交付仓库代码、测试、配置模板与操作手册。云资源采购、真实镜像推送、修改云安全组、公网发布及模型付费评测在明确目标与授权后执行。保留 Windows 本地开发；业务指标、问数范围与模型供应商继续沿用现有实现。

| 编号 | 需求 | 验收标准 |
| --- | --- | --- |
| R1 | 参数化构建及 ACR 发布 | 手动触发流程；三个镜像使用完整 commit 标识，发布清单记录仓库 digest；失败不得进入部署 |
| R2 | 实际镜像质量门禁 | 通过待发布镜像的真实 API/agent 执行冻结题单；报告与 commit、模型配置、三个镜像 digest 一致；不能补写源码报告冒充镜像评测 |
| R3 | 可重复发布与回滚 | 每次发布独立执行迁移；等待服务就绪；失败退出；保留上一次成功发布清单；回滚只切应用，不降级数据库 |
| R4 | RDS 兼容初始化 | 支持已有空数据库及已有只读账号；导入器不擅自扩大账号授权，不覆盖已有业务表；TLS 验证失败必须退出 |
| R5 | 真实数据库就绪检查 | 能发现 MySQL 网络、证书、账号与受限视图不可用；探针不调用模型、不扫描业务数据、不返回连接信息 |
| R6 | HTTPS 和运维准备 | 补充 HSTS、证书续期与告警、SLS 采集说明、备份恢复步骤、云端验收清单；区分本地验证与云端实测 |

## 当前证据与缺口

- `deploy/compose.cloud.yaml` 已有 CA 只读挂载、独立迁移服务、启动依赖、资源限制、日志轮换及回环入口。
- `deploy/nginx.ecs.conf` 已提供 TLS、SSE 转发和可信代理链；尚无 HSTS 与证书维护流程。
- `agent/tools/import_olist.py` 已支持 `MYSQL_SSL_CA` 和证书/主机名验证，但将建库、导入、建账号及授权组合为一个流程。
- Agent `/healthz` 只检查目录、数据源配置与服务密钥；Go `/healthz` 已检查 PostgreSQL，均不能证明分析视图可查询。
- CI 已有测试、镜像构建及源码评测；`scripts/quality_gate.py --manifest` 已核对发布身份，但源码评测报告没有镜像摘要，也未执行待发布 API/web 镜像。
- 仓库已有本地验证记录；暂无本批实现、真实 RDS 私网/TLS、ACR 推送、SLS 采集或 ECS 发布的验收证据。

## 技术方案

### 1. 镜像构建、评测与发布身份

新增手动触发的镜像工作流，复用现有检查并固定构建平台为 `linux/amd64`。工作流配置 ACR 登录地址、镜像仓库路径和专用推送凭据，运行身份与推送身份分离。凭据通过 GitHub Environment/Secrets 注入；缺失配置时失败，不从开发机自动复制密钥。

执行顺序：检查源码 → 构建三个镜像 → 推送候选镜像并读取仓库 digest → 在隔离测试环境拉取这些 digest → 执行规则与真实模型验收 → 校验发布身份与质量门槛 → 输出可部署清单。候选镜像进入 ACR 不表示已经部署生产。

新增镜像评测入口，复用冻结题单和独立 CSV oracle，使用独立 PostgreSQL/MySQL 夹具与测试身份。登录、创建任务、worker、查询及 SSE 经实际 API/agent/web 镜像执行；遥测从测试元数据库读取，不向生产服务植入评测探针。源码评测继续用于快速回归。

基线由上一次通过验收的镜像清单指定。首次上线若无已发布基线，显式选择一个完整 commit 构建基线镜像并记录为首次比较基线，不将其描述为已上线版本。真实模型比较沿用三轮及现有调用预算；缺少模型配置或报告不完整时拒绝正式发布。

`release-manifest.json` 包含版本、源码 commit、三个完整镜像引用、从实际 agent 镜像内容及模型 endpoint/model 计算的配置哈希、评测报告摘要与生成时间。模型密钥、数据库密码、租户映射和完整环境不写入清单或报告。保留当前质量门槛及题单版本；本批不降低门槛或删减失败案例。

ACR 地址从用户实例控制台取得。网络登录、推送和拉取按 [阿里云 ACR 官方说明](https://help.aliyun.com/zh/acr/getting-started/use-a-container-registry-enterprise-edition-instance-to-push-and-pull-images) 配置；不假设 ECS RAM 角色会自动完成 Docker 登录。

### 2. 发布与回滚脚本

新增 Linux Bash 发布入口及清单校验工具；ECS 预装 Docker Compose、Python 3、curl、flock。脚本接收受控环境文件、候选清单、评测报告及发布状态目录。环境文件是数据，不使用 `source` 或 `eval` 执行；真实配置不提交 Git，不打印 `compose config` 展开的内容。

发布前核对清单与门禁、CA 可读、HTTPS origin、镜像 digest、端口绑定及实际 Compose 配置；检查部署目录权限并加锁，防止两个发布同时修改状态。备份记录与回滚兼容性作为发布前置记录，脚本不声称自动完成 RDS 备份。

每次发布执行：拉取准确镜像 → 新建一次性迁移容器并等待退出码 0 → 更新 agent/API/web → 在有限时间内等待健康 → 检查 HTTPS 与外部冒烟结果 → 原子记录成功清单。迁移失败不切换应用，失败发布不覆盖上一成功状态；保留退出阶段及不含密钥的诊断。

迁移服务使用 profile 从日常应用启动中分离；应用启动依赖保留 agent/API 健康关系，迁移由发布入口显式串行控制。手工启动云应用的文档同步调整，避免 `up` 跳过或重用旧迁移。更新应用时使用重建及等待选项，不以 `restart` 代替配置更新，依据 [Compose up](https://docs.docker.com/reference/cli/docker/compose/up/) 与 [Compose restart](https://docs.docker.com/reference/cli/docker/compose/restart/) 的行为。

回滚入口接收上一次成功清单并核对其镜像及数据库兼容性，重建三个应用服务并等待健康；不运行旧迁移、不删除数据库、不自动降级 schema。若新迁移不兼容旧应用，则停止应用回滚并按单独恢复步骤处理。首次发布没有旧版本时明确记录无法切回旧应用。

单机重建存在短暂不可用；本批不承诺零停机或 99.9% 高可用。Compose healthcheck 用于就绪判定，容器运行中变为 unhealthy 由告警发现，不能将 restart 策略描述为自动修复所有依赖故障。

### 3. RDS 数据初始化

保留本地一键建库/建账号路径；新增 `--use-existing-database` 与 `--skip-account-provisioning` 两个明确模式。

- 使用已有数据库时验证数据库存在且为空，再创建表/视图；存在表或视图立即退出，不执行 DROP、TRUNCATE 或覆盖。DDL 非事务操作，中途失败保留现场并报告，恢复使用新空数据库，不自动清理可能属于用户的数据。
- 跳过账号创建时不读取或修改只读账号密码，不执行 CREATE USER、ALTER USER 或 GRANT。只读账号由管理员预建并按独立授权步骤只授予 `metric_orders` 视图 SELECT；文档提示控制台的整库只读授权不能代替视图级授权。
- `SQL SECURITY DEFINER` 视图由具备表权限的导入身份创建；该身份及其权限需保留，不能在上线后删除视图 definer。
- 云操作显式提供管理用户名，不假设存在 root；RDS 不提供 root，账号创建路径按 [官方账号说明](https://help.aliyun.com/zh/rds/apsaradb-rds-for-mysql/create-an-account-on-an-apsaradb-rds-for-mysql-instance) 验证。
- 导入连接沿用 CA、证书与主机名验证；云端步骤要求填写 `MYSQL_SSL_CA`，证书错误不降级为明文连接。

初始化管理员、迁移账号、运行账号及分析只读账号分别记录所需权限；初始化身份不注入常驻服务。导入后核对订单数、六指标、视图 SELECT 成功及原始表/写操作拒绝。

### 4. Agent 数据库就绪检查

保留 `/healthz` 的配置检查，新增仅内网使用的 `/readyz`。新接口检查配置后，以每个配置映射的只读账号访问受限视图，使用 `SELECT ... FROM metric_orders LIMIT 0` 验证视图可用和所需列权限，不读取业务行或调用模型。

检查按相同连接身份去重，在固定小并发和总时间预算内执行；连接与读超时单独收紧，避免复用查询路径的 12 秒读超时。总检查预算 5 秒，调用端与 Compose 探针预留额外超时；超过预算或任一映射失败返回通用 503。关闭连接，避免重复健康检查积累任务。

云 Compose 的 agent 健康检查改用 `/readyz`，API 继续等待 agent healthy；本地配置检查保持原有入口。就绪接口不经公网 Nginx 转发，响应不含租户、主机、账号或驱动异常。模型 HTTPS 出站由发布冒烟单独验证，避免定时探针产生模型费用。

### 5. HTTPS、日志与运维

在宿主 TLS server 增加 HSTS；初期只对当前域名生效，不启用 preload 或 includeSubDomains。增加证书安装/续期说明：续期后先 `nginx -t`，通过后 reload；配置外部证书到期告警，不在镜像内保管私钥或自动选择证书供应商。

为应用容器增加稳定的项目/服务日志标签，提供 SLS 采集字段、过滤、留存、权限和告警清单。保持现有白名单 JSON 日志，不记录请求体、Cookie、SQL 参数、密钥或 DSN。Logtail 按 [官方标准输出采集说明](https://help.aliyun.com/zh/sls/user-guide/collect-docker-container-standard-output) 独立安装，采集器权限不赋予业务容器；实际采集和日志轮换兼容性在 ECS 上验收。

补充宿主 Docker/Nginx 开机启动、容器异常/资源告警、数据保留及清理步骤。sessions、tasks/events 的删除策略另行确认；本批复用现有过期 attempt 清理，不擅自删除业务历史。

备份与恢复手册分别列出 PostgreSQL 元数据和 MySQL 分析数据、账号/视图权限及证书配置的恢复核对。RDS 恢复到独立实例演练，ECS 快照不能代替数据库备份。RPO/RTO、备份保留期、维护窗口和费用由目标实例确定并记录实测。

## 拟修改文件

| 位置 | 改动 |
| --- | --- |
| `.github/workflows/` | 新增手动镜像构建/验收工作流与参数说明 |
| `scripts/` | 镜像评测入口、清单校验、Linux 发布和回滚脚本 |
| `agent/tools/import_olist.py` | 已有空库及跳过账号管理模式 |
| `agent/runtime/app.py` 与测试 | 数据库就绪检查、超时与脱敏失败响应 |
| `deploy/compose.cloud.yaml`、`deploy/cloud.env.example` | 独立迁移 profile、就绪探针与运维参数 |
| `deploy/nginx.ecs.conf` | HSTS 与证书维护注释 |
| `docs/deployment.md`、`docs/agent-quality-gate.md`、`docs/validation.md` | 发布/回滚、初始化、观测、恢复和验收记录 |

## 验证与人工验收

本地验证：导入模式与只读权限的 MySQL 集成测试；就绪成功、视图缺失、连接失败、TLS 错误、超时与脱敏测试；发布的迁移失败/健康超时/重入锁/状态保护/回滚测试；摘要或报告不匹配时门禁失败；Bash 语法、Compose 配置、Nginx 配置及三个 Linux 镜像检查。通过变更相关检查后运行现有必要回归。

镜像评测先以无付费调用的规则/拒绝/权限题单验证执行链路，真实模型验收在明确评测环境和预算后执行；不得把规则评测描述为真实模型准入通过。实际 ACR/RDS/ECS/SLS 测试等待目标资源，测试报告明确记录未执行项。

云端最终检查：HTTPS 与 Cookie、登录 IP 限速、规则/模型问数、跨租户拒绝、SSE 续传、主机重启、数据库故障与证书轮换、固定资源下 P95/并发、双库备份恢复。新批次完成后记录 commit、镜像摘要、测试结果与人工验收状态。

按用户提供的 AGENTS.md，需求沟通、技术方案、代码与测试须有人工作为确认节点。本次“继续”作为继续部署准备方向的确认；本方案确认后实施，代码和测试完成后提交人工验收。每批仓库改动提交 commit，不将现有未提交的仓库规则变更纳入本批提交。
