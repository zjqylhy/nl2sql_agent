# ECS 自建数据库运维

适用已确认的 [私有部署规格](ecs-private-deployment-spec.md)。应用根目录 `/srv/olist/app`，环境文件 `/srv/olist/.env.private` 权限 0600。密钥目录 `/srv/olist/secrets` 权限 0700。环境文件由 Compose 解析，不执行 shell source，不打印完整 Compose 配置。

## 首次部署

本机构建并验证 API、agent、web、tunnel 镜像，使用完整 commit 作为 tag；记录全部应用与数据库 image ID。通过 SSH 上传镜像包、源码发布包、四个官方 CSV 和网关公开 CA，核对 SHA256 后加载。ECS 不构建镜像。

在 ECS 生成专用 tunnel-key（UID 10002 所有、0600），从已认证的网关连接获取其公开主机密钥，保存 gateway-known-hosts（0444），公开 CA 为 gateway-ca.pem（0444）。在网关配置 nl2sql-tunnel 专用身份：仅来源 8.155.128.79、AllowTcpForwarding local、PermitOpen 127.0.0.1:8443、PermitListen none、PermitTTY no、ForceCommand /bin/false。先 sshd -t 再 reload。

`scripts/private_ops.py init-config --commit 完整SHA` 从 stdin JSON 读取 gateway_key、postgres_image、mysql_image，生成随机双库/只读/服务/管理员密码；镜像引用应绑定实际 image ID 或已验证 tag。不允许覆盖已有环境文件。

```bash
cd /srv/olist/app
python3 scripts/private_ops.py initialize
python3 scripts/private_ops.py smoke
python3 scripts/verify_semantic.py --csv-dir /srv/olist/csv \
  --env-file /srv/olist/.env.private --compose-file deploy/compose.private.yaml
```

初始化遇到已有 olist_alpha 时拒绝覆盖。失败后保留数据库与凭据，检查退出阶段并采用显式恢复流程；不要删除数据 volume 重试。已经初始化的更新不能再次调用 initialize。

## Windows 访问

本机 8088 需空闲；若同一项目本地容器占用，可先显式停止本地 web。运行 `scripts/connect_private.ps1`，默认使用用户 Downloads/aliyun_sk.pem，通过受限临时副本完成 SSH 认证后删除副本。主机公开密钥位于项目 .cache/cloud/ecs-known-hosts。访问 http://localhost:8088；隧道关闭后重跑脚本。

首次管理员为租户 alpha、账号 admin，密码读取单独交付的受限本地凭据文件或云端 .env.private 的 BOOTSTRAP_PASSWORD。不要将该文件或完整环境放入 Git、聊天或日志。

## 备份与恢复

```bash
python3 /srv/olist/app/scripts/private_ops.py backup
python3 /srv/olist/app/scripts/private_restore_check.py /srv/olist/backups/实际备份目录
```

完整备份包括 PostgreSQL custom dump、角色定义、MySQL 数据/视图、alpha_ro 身份和授权、内容摘要与行数。全部成功后原子改名，才轮换本工具的备份目录，保留最近七份；`.partial-*` 为失败现场，不作为有效备份。运维需检查失败现场并处理磁盘占用，脚本不自动清理失败数据。

安装 deploy/private-backup.service 和 private-backup.timer 到 /etc/systemd/system，启用 timer。备份每天按服务器时区 03:00 执行；部署时设为 Asia/Shanghai。通过 systemctl status/journalctl 检查失败。初始化和备份共用 flock，避免操作交错。

恢复检查创建带专用标签的随机临时容器，逐库恢复、核对用户/任务与订单行数，并验证只读视图权限；临时容器与其匿名 volume 在验收后移除。源数据库和源 volume 不变。

实际恢复到新主机时：准备兼容的数据库镜像和受限 .env.private；恢复 PostgreSQL 角色，再创建 platform 数据库并用 pg_restore 恢复；恢复 MySQL 数据/视图，再恢复 mysql-accounts.sql；核对视图 definer、运行账号、CA/隧道及服务映射。不能只恢复数据而遗漏账号和配置。

备份中的账号定义含密码哈希，应按秘密文件保护。首次备份下载本机保存一份；持续异地备份尚需配置独立目标。同盘备份不能覆盖主机或云盘丢失。

## 更新与停止

更新前创建并下载备份，核对新 schema 与旧应用兼容。加载新镜像，更新环境文件中的应用镜像引用；单独 run migrate，按新增业务表清单补充最小授权，再 up --wait --force-recreate agent api web。失败不记录成功发布，不自动降级数据库。保留此前镜像和成功身份供兼容回滚。

```bash
docker compose --env-file /srv/olist/.env.private -f /srv/olist/app/deploy/compose.private.yaml ps
docker compose --env-file /srv/olist/.env.private -f /srv/olist/app/deploy/compose.private.yaml stop
```

停止不会删除数据。运行环境禁止使用 down -v；资源紧张时先检查 docker stats、OOM、重启、磁盘和任务队列。此配置没有完成长期压力或高可用验收。
