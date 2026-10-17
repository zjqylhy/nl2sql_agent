# Python 平台部署、迁移与恢复

默认入口 `compose.yaml`：一套 PG，Python API/Worker 同镜像分进程，React/Nginx。`platform_migrator` 只用于 Alembic、框架检查点建表及 Olist 导入；服务分别使用 `platform_api` / `platform_worker`，分析连接使用独立只读角色。API 不授予 `agent_checkpoint` 权限，两个平台运行账号均不授予原始事实表权限。

登录限流状态由 PG 共享。默认按直接对端识别来源；前置代理部署可配置 `TRUSTED_PROXY_CIDRS`，仅信任明确代理地址，并从右向左找第一个不可信对端，不能直接信任客户端提交的 X-Forwarded-For。公网部署的 Origin、HTTPS、安全 Cookie 和代理边界需要单独验收。

## 新库与只读数据

1. 配置 `.env` 的随机密码。执行 `docker compose config --quiet`，再启动默认栈；迁移成功后才创建运行账号并启动 API/Worker。
2. 执行 `docker compose run --rm import`，保存导入摘要。导入是事务，拒绝覆盖已有 schema。
3. 写入角色/数据源映射，重建 API/Worker。执行 `docker compose run --rm seed` 创建管理员。镜像启动不会自动创建默认密码账号。
4. 先用规则问数核对数据，再确认模型映射与价格、启用分析。分析仍需人工验收真实模型结果。

同种数据库可以分实例；当前 schema 分域并不隔离资源。分析 SQL 服务端超时 10 秒，客户端异步等待 15 秒，取消收尾最多另需驱动的有限等待；查询只读、返回最多 1000 组，分析州候选最多 27 组。Worker 租约为 60 秒、每 10 秒续租；失去执行权不能提交操作回执或图状态。

## 历史版本与升级

仓库中的 Go/MySQL 运行、发布及旧库接管入口已退役。历史命令需要检出对应历史提交，不能在当前分支混用。服务器既有镜像、旧发布目录和数据卷保留，具体切换记录见 `python-platform-ecs-validation.md`。

当前升级先确认无待执行或等待补充的任务并完成一致备份，再更新 API/Worker 镜像。运行代码摘要改变后不允许旧活动图跨版本恢复；已完成报告继续受原指标授权约束。Alembic 历史迁移及当前备份/空库恢复入口保留，不重写历史迁移。

私有备份从发布目录执行 `python3 -m scripts.python_private_backup`，默认读取服务器受限配置；配置解析在 `scripts/private_env.py`，不依赖旧双库工具。备份镜像标签必须与当前 APP_TAG 配套，定时器为 `python-private-backup.timer`。

## 一致备份与空库恢复

备份使用 PostgreSQL 官方 `pg_dump` custom archive。导出一个 repeatable-read snapshot，逐表计数与 pg_dump 使用同一 snapshot，覆盖 `platform`、`agent_checkpoint` 和 `analytics_*`。保存摘要、服务器版本、逐表计数和所需数据库角色名；不导出其他数据库，也不把全局账号密码写进清单。

```powershell
# DSN 由环境注入，不能写进命令行参数或提交到 Git。
$env:BACKUP_DATABASE_URL = '<管理员 DSN>'
.venv/Scripts/python.exe scripts/backup_python_platform.py backup .cache/backups/20261017 --pg-bin '<客户端 bin 目录>'

# 先显式建立一个空目标库；重建所需角色并从密钥管理中配置密码。
$env:RESTORE_DATABASE_URL = '<空目标库的管理员 DSN>'
.venv/Scripts/python.exe scripts/backup_python_platform.py restore .cache/backups/20261017 --pg-bin '<客户端 bin 目录>'
```

`pg_dump` 客户端不能低于源服务器主版本；恢复按相同或兼容的新版本验证，不能假设向旧版本兼容。恢复工具先验摘要、目标是否为空和所需角色是否存在，再以单事务恢复并核对所有表计数；拒绝覆盖非空库。恢复前停止对目标库的服务访问，避免检查后出现并发写入。备份包含账号 hash、执行问题和证据，应存放于受访问控制的加密存储。

跨主机恢复时先创建 `platform_api` / `platform_worker` 和清单中的分析只读账号，再恢复授权；生产密钥取自部署密钥管理，不能使用测试密钥。新的目标连接身份、schema 和快照版本需写回可信数据源配置；绑定变化不能绕过历史访问限制。

清理命令 `python -m agent.platform_app prune`：24 小时补充等待过期；30 天终态详细步骤/原始依据与图线程清理，保留最终报告及用量汇总。Worker 每分钟执行一轮有界清理；失败可重试，已归档用量不会归零。

官方机制参考：[pg_dump 快照](https://www.postgresql.org/docs/current/app-pgdump.html)、[LangGraph interrupt 重启语义](https://docs.langchain.com/oss/python/langgraph/interrupts)。框架和工具不能替代恢复演练。
