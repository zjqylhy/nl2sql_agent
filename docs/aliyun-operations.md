# 阿里云发布与运维手册

对应 [已确认的技术方案](aliyun-release-spec.md)，目标为 ECS Linux + Docker Compose。真实地域、资源、域名与预算由用户提供；本地验证不能替代云端验收。

## 资源准备

记录地域、VPC、ECS 架构/规格、双 RDS 版本/系列、ACR/SLS 实例、域名/备案和预算。当前镜像验证平台为 linux/amd64，ARM 主机需另验收。ECS 与数据库同 VPC；RDS 使用内网 DNS，白名单仅允许应用/运维来源。

公网仅开放 HTTPS 443，80 按证书验证/跳转需要开放，SSH 限定运维来源。3306/5432、API/agent 和 8088 不对公网开放。核对宿主防火墙、Docker 端口绑定及网段冲突。验证 RDS、ACR、基础镜像/依赖源和模型 HTTPS 出站，不依赖 Windows 本机代理。

ECS 安装 Docker/Compose、Python 3、curl、flock 和 Nginx，启用 Docker/Nginx 开机启动。容器 restart 策略不能替代依赖故障告警；变为 unhealthy 不会自动重建。

## 数据库初始化

PostgreSQL 分离迁移和运行身份：先 migrate，再按 deployment.md 授权运行账号，运行账号无 DDL。CA 只读挂载，DSN 使用 `sslmode=verify-full&sslrootcert=/run/secrets/postgres-ca.pem`。

CA 是公开信任证书，宿主文件需允许容器 UID 65532（API）和 10001（agent）读取，可设置 0444 后只读挂载。HTTPS 私钥另行限制为 Nginx 运维身份可读，不能套用 CA 文件权限。

MySQL 创建导入身份、空数据库和普通分析账号；导入身份有建表/建视图权限。提供 MYSQL_HOST/PORT、MYSQL_ADMIN_USER、MYSQL_SSL_CA 和管理密码，在一次性私网运维环境执行：

```bash
python agent/tools/import_olist.py --csv-dir /srv/olist/csv \
  --schema olist_alpha --readonly-user alpha_ro \
  --use-existing-database --skip-account-provisioning
```

两个模式不会建库、建账号、授权或读取只读账号密码；指定库不存在或非空时拒绝。DDL 中途失败保留现场，使用新空库恢复，不自动清理旧数据。

由管理员单独执行最小授权，替换实际名称：

```sql
GRANT SELECT ON olist_alpha.metric_orders TO 'alpha_ro'@'%';
```

控制台整库只读授权不等同视图级授权。保留视图 definer 身份和底表权限；核对订单数及六指标，确认分析账号只可读视图、不可读底表/写入。所有角色映射填写 `ssl_ca=/run/secrets/mysql-ca.pem`；为实际内网连接地址启用 SSL，证书错误不关闭校验。

租户管理员通过一次性 `api -command bootstrap` 创建，密码通过 stdin 输入；导入/初始化管理凭据不注入常驻服务。

## 镜像发布包

GitHub Environment `aliyun-release` 配置 Variables：ACR_REGISTRY、ACR_NAMESPACE、LLM_BASE_URL、LLM_MODEL；Secrets：ACR_USERNAME、ACR_PASSWORD、NL2SQL_AGENT_API_KEY（网关应用 Key，评测进程映射为 LLM_API_KEY）。runner 与 ECS 使用各自可达的网关 HTTPS DNS，逻辑模型为 `chat`，不能使用 Windows 的 localhost SSH 隧道地址。配置 runner 的 ACR 网络访问及专用推送身份；ECS 使用只拉取权限，RAM 角色不代表自动完成 Docker 登录。

授权真实推送和付费评测后，手动触发 `Aliyun candidate images and acceptance` 并指定基线完整 commit SHA。首次基线必须明确选择相同冻结题单的兼容版本，不表示已上线。流程构建/推送三个镜像、读取仓库 digest、运行三轮实际镜像验收，再输出 artifact，不通过 SSH 部署 ECS。

每轮每版本最多 45 次模型出站，三轮两个版本上限 270 次，包含重试；预算耗尽或配置缺失拒绝正式发布。调用预算不代表 Token/费用硬上限，费用以供应商账单为准。

下载 artifact 并一起保留 release-manifest.json 与摘要命名的 candidate/baseline 报告。发布重验三轮结果、冻结题单、策略、commit、实际镜像 digest、模型配置和 SSE。源码报告或 `--local-images` 诊断报告不能打包为正式发布依据。

## 发布和回滚

复制 cloud.env.example 为权限 600 的 .env.cloud，填写双库、CA、HTTPS origin、精确可信代理、模型等真实配置。创建由运维用户拥有的权限 700 状态目录；配置不入 Git。环境文件由 Compose 解析，不执行 shell source；三个镜像由已验收清单覆盖，模板中仍需提供合法 IMAGE 值以便解析。

发布前核对双库备份和旧应用兼容性，准备运维 JSON 记录：backup_reference 为实际备份编号，target_commit 为候选 commit，previous_commit 为 current.json 的 commit（首次 null），old_app_compatible=true 表示已人工复核兼容。脚本不自动备份，不能为通过校验填写虚假记录。

为专用验收账号注入 SMOKE_TENANT、SMOKE_USER、SMOKE_PASSWORD；SMOKE_MODEL_QUESTION 可指定支持范围内的模型兜底表达。默认问题为“本月 GMV，只看圣保罗州”，冒烟会实际调用模型。

```bash
chmod +x scripts/deploy_cloud.sh scripts/cloud_smoke.py
bash scripts/deploy_cloud.sh deploy /srv/olist/.env.cloud \
  /srv/olist/release/release-manifest.json /srv/olist/state \
  /srv/olist/backup-record.json "$PWD/scripts/cloud_smoke.py"
```

顺序：门禁/配置 → 拉取 → 新建一次性迁移 → 重建应用并等待健康 → HTTPS/登录/规则/模型/SSE → 记录成功。迁移失败不切应用；后续任一步失败保留成功状态。状态目录连同评测报告一起保存，current.json 为最后成功版本，previous.json 为此前成功版本。两者通过 active 指针一次切换至新快照，保存失败保留完整旧状态；备份时保留符号链接、快照和报告。

手工维护必须先显式迁移再启动，正式发布仍需门禁入口：

```bash
docker compose --env-file .env.cloud -f deploy/compose.cloud.yaml run --rm --no-deps migrate
# 仅在迁移成功后执行。
docker compose --env-file .env.cloud -f deploy/compose.cloud.yaml up --wait --wait-timeout 120 api agent web
```

API healthz 检查 PostgreSQL；agent readyz 验证所有映射账号的视图/列权限，不读数据行或调用模型。检查最多等待 5 秒，固定线程池避免故障堆积；任一映射失败返回通用 503。readyz 不经公网 Nginx 转发。

回退成功升级时选择 previous.json；恢复失败发布时选择 current.json。提供兼容性 JSON 记录：current_commit 对应状态当前成功 commit、target_commit 对应目标、schema_compatible=true 表示已复核现有数据库兼容目标应用。恢复目标版本的模型配置及数据源/CA。

```bash
bash scripts/deploy_cloud.sh rollback /srv/olist/.env.cloud \
  /srv/olist/state/previous.json /srv/olist/state \
  /srv/olist/rollback-record.json "$PWD/scripts/cloud_smoke.py"
```

回滚不执行旧迁移、不删除数据、不降级 schema；数据库不兼容时按单独恢复步骤处理。首次没有成功状态时不存在应用回滚目标。单机更新存在短暂不可用，不承诺零停机或 99.9% 高可用。

## HTTPS、日志与告警

使用 nginx.ecs.conf，替换域名和证书路径。初期 HSTS 当前域名 86400 秒，不启用 includeSubDomains/preload。证书在宿主续期，续期后先 nginx -t 再 reload；设置剩余天数告警，私钥仅供 Nginx 运维身份访问。实测 SSE 无缓冲、心跳、续传和真实客户端 IP 限速。

按 [SLS 标准输出采集](https://help.aliyun.com/zh/sls/user-guide/collect-docker-container-standard-output) 独立安装 Logtail。按标签 olist.project=nl2sql、olist.service=api/agent/web 选择容器并解析 JSON；配置留存、查询权限及 json-file 轮换兼容性。采集器权限不赋予业务容器，日志不记录问题、SQL 参数、Cookie、DSN 或密钥。

告警覆盖 unhealthy/重启、API/agent 错误与超时、模型认证/限流、CPU/内存/磁盘、RDS 连接/存储、证书到期和采集异常；阈值按真实负载确定。运行失败与答案正确率分开统计。

attempt 清理继续使用 api -command prune-attempts，30 天前结束记录每次至多 1000 条，按 deleted 数分批执行。sessions、tasks/events 保留策略另行确认，本批不删除业务历史。

## 备份恢复及最终验收

双库分别配置备份/PITR 和保留期，恢复至独立测试实例。核对账号、视图 definer/权限、任务/事件/attempt、订单数和六指标、数据库 CA/TLS DNS；ECS 快照不代替 RDS 备份。记录恢复点、数据损失范围、开始/结束时间和实测 RPO/RTO。

目标验收包含 HTTPS/Cookie、IP 限速、规则/模型、跨租户/角色拒绝、SSE 续传、主机/容器重启、数据库故障、证书错误/轮换、双库恢复及固定资源 P95/并发。记录资源、模型、数据规模、样本数、digest、commit 和成本，未执行项保持未验证。
