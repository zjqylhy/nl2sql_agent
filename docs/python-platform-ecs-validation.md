# Python 平台 ECS 部署与验收（2026-10-17）

用户明确要求在服务器部署与测试，并单独授权切换入口、启用新备份定时器。本轮镜像构建、前端构建、Python 回归、HTTP/真实模型测试及数据恢复均在 ECS 执行。2026-10-17 14:35 CST 完成切换后核查，运行版本为 `54edc609d769a9af81b2c99e482dbff5c760b1c2`。

访问 [云端工作台](http://localhost:8088)，该地址经 `scripts/connect_private.ps1` 的 SSH 隧道访问 ECS，应用运行在服务器。继续使用原有账号。脱敏机器记录见 [验收证据](evidence/python-platform-ecs-20261017.json)，不包含账号、会话、密钥或备份内容。

## 部署和数据迁移

- 新栈：`olist-python-agent`，Python FastAPI / LangGraph Worker、PostgreSQL 17、React/Nginx、既有模型网关 SSH 转发。新数据卷独立于旧栈。
- 旧源入口 `/srv/olist/app` 保留；新发布源码放在 `/srv/olist/python/releases/<commit>`，受限环境文件为 `/srv/olist/python/.env.private`，不导出到本地。
- 切换前旧栈备份：`/srv/olist/backups/20261016T214729Z-18dcfde0`。PG archive SHA256：`60e540e74cbf1f063eb24d35a04822f7908bc869933bf946aa547238fbfdb576`。备份包括原 PG、MySQL、角色和账号授权，仅保存在服务器。
- 在新 PG 数据卷恢复副本；仅对副本补齐 004 并执行 `migrate --adopt-legacy`。保留原 2 个账号、35 条任务；副本中的 6 条旧待补充任务结束为取消，原库保留原状。旧会话不伪装成可恢复的新 LangGraph 执行。
- 从服务器原四份 CSV 导入 99,441 笔订单，发布 `analytics_alpha_v1`。数据版本：`0e4430a9bbc76eee2c4b305420fcd3f8ea30d81e3a1b38ff0172f9c473e29237`。

## 服务器构建

Docker Hub 直连超时，使用 Docker 官方镜像的 Public ECR 分发地址，在服务器下载、标记基础镜像后构建；不依赖本地 Docker。

```bash
for image in python:3.12-slim node:24-alpine nginx:1.28-alpine; do
  docker pull "public.ecr.aws/docker/library/$image"
  docker tag "public.ecr.aws/docker/library/$image" "$image"
done

cd /srv/olist/python/current
dc() {
  docker compose --env-file /srv/olist/python/.env.private \
    -f compose.yaml -f deploy/compose.python-private.yaml "$@"
}
dc config --quiet
dc build migrate web
docker build --build-arg "PLATFORM_IMAGE=olist-python-platform:<commit>" \
  -f deploy/Dockerfile.validation -t "olist-python-validation:<commit>" .
```

验收镜像在同一应用镜像上安装 PostgreSQL 17 官方客户端；源码/CSV 只读挂载，连接新 PG，在随机命名测试库中执行回归。Linux 发布脚本测试需要临时目录可执行，仅验收容器使用 `/tmp:exec,size=384m`；运行容器保持默认设置。启动验收前检查镜像存在，使用 `--pull=never`。

## 模型策略与失败处理

逻辑别名 `chat` 的实际响应模型为 `deepseek-flash`；网关模型登记为 `DeepSeek-V4.1-Flash`，TPM 上限 50,000。此处仅记录本轮观察，不保证别名后续路由不变。

用户明确允许以 **TEST_UNIT 测试预算单位**联调。该单位不是人民币或美元，不能作为真实费用、供应商账单或生产计价证据。本轮服务配置：输出上限 4096 Token、单次整体截止 45 秒、总 Token 预算 50,000、调用间隔 20 秒；新任务接纳时冻结，仍受 10 次模型调用、12 次工具动作、6 次 SQL、180 秒活跃执行和一次有限网关退避约束。

首次真实调用发现并修复：

1. 1024 输出上限会被推理 Token 耗尽。输出预算、预留和已知用量校验统一读取冻结策略。
2. PG 不支持在排序表达式中间接引用输出别名。指标排序改用原生 `NULLS LAST`，增加升降序、NULL、同值和 TopN 的真实 PG 用例。
3. 参数错误原先只有字段名，补充不含原始输入的约束原因，并明确 TopN/比较和 selection/scalar 的协议。
4. 模型单次等待和 Token 总预算支持有界配置；所有上限在请求前检查，部署配置不能扩大旧任务额度。
5. 真实连续调用遇到 429；仅识别网关明确执行前拒绝的契约，回执按零模型用量结算并持久退避一次。其他状态继续保留未知预算，不自动重发。
6. 低 TPM 环境的调用间隔通过 interrupt 保存；等待不占线程和下一次模型预留，恢复复用原操作回执。

前期复杂 TopN（额外订单、客单价和占比）出现输出截断、参数错误、证据类型错误、预算停止和未知响应，均保留在服务器任务与账本。基础用例与复杂用例分开记录，失败样本不计为通过。`python_server_smoke.py --extended-topn` 单独覆盖复杂用例；默认覆盖原始 TopN 合计与两期下降分析。

## 验收结果

| 检查 | 结果及边界 |
| --- | --- |
| 最终镜像回归 | **393 passed, 12 skipped，118.18 秒**；12 项为旧 MySQL 路径，目标 PG 路径已覆盖。故障机制测试使用确定性模型适配器，不计为模型效果样本 |
| 前端发布构建 | TypeScript / Vite 通过；原 ECharts 约 537 kB 分块提示仍存在 |
| 原问数 HTTP | 规则解析与真实模型解析各一条，通过；原账号登录、请求幂等及 SSE 终态/补读通过 |
| 真实模型基础 TopN | 2018 年 7 月前三州 SP/RJ/MG，合计 **517638.01 BRL**；4 次模型、1 次 SQL、6750 已知 Token |
| 真实模型两期比较 | 2018 年 7 月相对 6 月下降前三 SP/MG/RS，分别 **-18072.77 / -12164.76 / -4472.21 BRL**；订单量和客单价同步核查；6 次模型、2 次 SQL、14780 已知 Token |
| 独立数据核对 | 从 99,441 笔原 CSV 独立计算；核对选择集合、总额，以及逐州 GMV/订单/客单价的基期、当期和差值。报告注明原始总量、不同天数及不支持因果推断 |
| 权限与终止 | 真实数据库账号隔离、Origin/未登录拒绝、指标/管理权限、账号禁用后会话失效、无数据不补零、取消拒绝迟到结果，通过 |
| Worker 重启恢复 | 真实容器重启后补充任务保留原步骤和调用数；同一补充重复提交只消费一次；恢复后完成，合计 4 次模型、1 次 SQL |
| 完整备份恢复 | 同一快照的 28 张表恢复到独立空库并逐表核对，通过；包含 99,441 笔事实、72 条任务、357 个检查点、5 份报告；演练库已清理 |
| 切换后核查 | `/healthz` 为 `python-langgraph`，首页及发布 JS 可访问；原管理员登录成功；上述两份报告的步骤与产物不变，SSE 补读通过 |

基础 TopN 和两期比较记录于 `smoke-candidate.json`、`oracle.json`。运行时后端摘要为 `fd33f1db2baa26ab822a379328f9abb08eda22fa0518c9e065c3ffd1da31b534`；逐文件源码、依赖锁及镜像标签已比对。真实模型冒烟在 `8d10a88` 后端执行，最终 `54edc60` 只修改前端预算提示，后端源码及摘要相同。最终版本重新构建并完成全部回归与切换后验证。

### 未通过与待验证

扩展 TopN（额外核查订单量、整体客单价及占比）**未通过**：运行 `2b6ce04ae85755f72feccb512f10753023b2223c2e943fdb` 在第二次模型调用遇到 HTTP **503**。此前已执行一次 SQL；已知用量 3417 Token，另一次执行结果未知，保留 10096 Token 预留，账本合计占用 13513。系统停止、不自动重发、不生成完整报告。不能因确定性计算测试通过而将此真实模型任务计为成功，也不能把 503 当成已证明未计费。

两份核心样本的活跃耗时分别 18388 / 38747 ms，不包含持久等待；20 秒调用间隔会增加端到端耗时。这些冒烟记录没有证明成功率、成本或延迟改善。**完整 A/B/C 独立任务集实验、题集人工复核与隐藏组扩充仍待执行**；真实供应商费用、服务器重启恢复及公网 HTTPS 未验收。

后续完成了独立测试 PG 上的 [12 表达 A/B/C 预实验](analysis-study-pilot-results.md)，原始失败与事后判分修正分别记录；没有将上述 Olist 冒烟混入合成题统计，也没有替换本次生产运行版本。

## 备份、切换和回退

备份入口：在发布源码目录执行 `python3 -m scripts.python_private_backup`。定时器为 `python-private-backup.timer`；凭据从受限配置读取，通过临时环境文件传给备份容器，不放在进程参数中。恢复只针对显式建立的空库，校验 archive 摘要和全部表计数；演练结束只清理本次随机目标库。

- 恢复演练备份：`/srv/olist/python/backups/20261016T225705Z-39ea4ac3`；archive SHA256：`6201d85fc34e954894b5f2222663eb9c379acb063073f92665ed21cf706e22c6`。
- 切换后备份服务成功执行：`/srv/olist/python/backups/20261017T063205Z-a1a41c4a`；SHA256：`8bb90212b41bd957f2bc0b3ed644052b2ff959ab068cf2c5d03aad84b97a25a5`。该份已校验 manifest，空库恢复演练使用上一份，不混淆两者。
- 新定时器已启用并 active，服务器时间每天 03:00 CST 执行，`Persistent=true`；核查时下一次为 2026-10-18 03:00 CST。备份保留暂由人工管理，尚无自动清理策略。
- 新 Web 仅发布 `127.0.0.1:8088`，PG 不发布宿主机端口；`/srv/olist/python/current` 指向上述发布版本。旧栈容器全部停止，旧备份定时器禁用；旧数据卷仍存在。

旧镜像、源目录、配置和数据卷保留。回退时先停止新入口和新备份定时器，再用 `/srv/olist/.env.private` 与 `/srv/olist/app/deploy/compose.private.yaml` 启动旧栈、验证首页和健康、启用旧备份定时器。新栈新增任务保留在新数据卷，回退不会自动反向合并到旧库；禁止 `down -v`。
