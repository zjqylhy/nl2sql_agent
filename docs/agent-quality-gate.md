# Agent 评测与变更准入

2026-10-10：需求及方案已确认，首批实现供代码/测试人工验收。

## 门禁规则

`evals/policy.json` 定义项目初始策略：支持问题正确完成率至少 85%，相对基线及每类别的下降不能超过 **2 个百分点**。这是项目起始值；小样本中一个错误即可超过这个幅度。关键案例由通过变失败、任何权限/澄清/范围拒绝失败、规则样本模型调用非零均阻止放行。未完成、缺题、数据集不一致、模型未配置、诊断报告不能替代完整报告。

支持问题以全部应支持案例为分母，供应商失败仍计为失败。报告另外记录已完成答案分母、类别结果、解析路径、模型调用/已知 Token/未知用量、阶段耗时。未知用量不填 0，不将运行成功率作为答案正确率。

当前题单为 120 条表达、35 个业务/权限组，开发和验收分区各 60 条，同组改写不跨分区。预期条件来自规格，数值由原始合成 CSV 的独立 Decimal 计算器产生。人工题单复核、真实分布样本及总规格的 300+ 评测仍未完成。CI 回归集保持冻结；验收题一旦用于针对性调优，需要转为回归并补充新验收题。

## 两层 CI

- `Agent quality / agent-quality`：每次 push/PR 在隔离 PostgreSQL/MySQL 上，经过登录、任务、worker、Python 与 SQL，跑开发分区 50 条确定性案例，比较 PR base 或 push 前一提交。无付费模型调用。
- `Live agent acceptance / live-agent-quality`：手动启动，填写已发布基线完整 commit SHA。使用 `agent-evaluation` 环境的 `LLM_API_KEY` Secret 与 `LLM_BASE_URL`、`LLM_MODEL` Variables，验收分区两版本各跑 3 轮，每轮均须过门禁。每个版本每轮最多 45 个模型请求，总上限 270 次；正常情况下 15 次/轮，两版本共 90 次。没有凭据直接失败，禁止降级为离线通过。

模型请求数由独立探针观察真实 HTTP 调用；不替换模型响应。SQL 调用、计划条件、排序、NULL、样本数和结果同时判定。两版本在同一个候选 Go 平台上运行，比较对象是 Python agent；Go/权限平台变更另外由真实数据库集成测试检查。因此不能把此报告称为完整发布镜像的端到端认证。

首个基线没有评测文件时使用脚本固定的 85%/2 个百分点策略，随后使用基线策略。同一个实现 PR 修改题单、夹具、oracle 或门槛会被 `compare_agent.sh` 拦截，需要独立人工评审与新版本基线初始化；不得通过删题、改预期或降低门槛让实现 PR 过关。

仓库管理员需把 `agent-quality` 和原有 `checks` 设为合并必需状态检查，禁止绕过；保护 `agent-evaluation` 环境，只允许可信分支/人工批准运行带密钥的工作流。**本次只提交工作流，尚未修改远端保护规则，也没有远端 CI 运行记录。** 当前没有自动执行阿里云发布的工作流。

## 本地运行

显式配置测试专用 `TEST_DATABASE_URL`、`MYSQL_TEST_HOST`、`MYSQL_TEST_PORT`、`MYSQL_TEST_USER`、`MYSQL_TEST_PASSWORD`。账号需要创建和清理临时 schema/数据库/用户；严禁指向生产数据库。评测为每轮创建随机临时身份、只读视图，正常退出自动清理。Linux 为 CI 与评测主环境；Windows 强制终止子进程可能遗留 PostgreSQL 测试 schema，需按 `test_eval_` 名称人工核对清理。

```bash
pip install -r agent/requirements.lock
go build -o .cache/evalfixture ./cmd/evalfixture
python scripts/evaluate_agent.py --fixture-server .cache/evalfixture \
  --source-commit "$CANDIDATE_SHA" --output .cache/evaluation/candidate.json
# 基线源码位于独立 checkout；先跑同一套题，不自行生成“通过”报告。
python scripts/evaluate_agent.py --agent-root /path/to/baseline/agent \
  --fixture-server .cache/evalfixture --source-commit "$BASELINE_SHA" \
  --output .cache/evaluation/baseline.json
python scripts/quality_gate.py --candidate .cache/evaluation/candidate.json \
  --baseline .cache/evaluation/baseline.json --policy evals/policy.json
```

真实模型增加 `--mode live --split acceptance --max-model-calls 45`；`--case-id` 只用于诊断，报告明确标为 diagnostic，门禁拒绝。模型凭据通过环境传入；从 Compose `.env` 复制配置时注意 Docker `--env-file` 不解析外层引号，不能把引号作为密钥内容。

报告默认仅包含安全案例 ID、统计与配置哈希，不输出问题、SQL、结果、凭据和 endpoint。代码 commit、runtime 全部 Python/JSON 内容、模型/供应商标识、题单/夹具/oracle 哈希关联报告。供应商内部权重版本仍不可确定。详情留在忽略目录，不上传供应商响应或容器环境。

## 正式发布边界

`quality_gate.py --manifest release-manifest.json` 额外核对 live/acceptance、commit、配置哈希、三个镜像 `registry/image@sha256:...` 的一致性，缺失或 tag 镜像拒绝。当前隔离源码评测报告不包含发布镜像摘要，因此不能直接作为该发布关卡的通过证明。实际 ACR 构建摘要、在目标镜像上的评测记录及部署 job 的依赖关系需在真实发布流程接入，禁止手工给源码报告补三个摘要冒充镜像实测。

阿里云上线还需 TLS/私网证书、运行账号授权、备份恢复、固定资源 P95/并发压测。此批门禁覆盖语义与权限回归；共享 CI runner 耗时不作为线上 P95 验收，生产 Token/费用硬预算留在 B 批。
