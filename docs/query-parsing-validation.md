# 查询解析可靠性实现与验证

日期：2026-10-15。

状态：需求与技术方案已获用户确认，实现和自动化验证完成，待人工验收。尚未执行本版本的真实模型三轮、MySQL/PostgreSQL 集成或部署验收。

## 实现行为

首轮问题统一生成 QueryPlan。共享受限语法按原文位置匹配完整子句，不再通过删除否定词来判断成功。“不包含 SP”“不看 sp，看其他州”生成 SP 排除筛选；“不要只看 SP，查看全部州”取消州筛选。多轮取消州筛选会保留金额等其他条件。

未覆盖的否定、修饰或未知句式交给已配置的模型理解完整原文，不能将其中的肯定子串作为硬约束。多轮不再因规则修改目标为空而跳过模型；完整子句、手动选择、操作类型、筛选 ID、目录和全部指标权限继续约束候选计划。单次解析至多调用一次模型；无效 JSON、截断、空操作或模型失败不执行 SQL，也不推进有效计划。

规则诊断只保存句式类型、覆盖原因与原文区间。语法、Schema 和范围检查不能证明任意自然语言被正确理解，独立题集仍需检查计划语义及查询数值。

## 自动化验证

| 检查 | 结果与范围 |
| --- | --- |
| Python 全量测试 | 307 passed、15 skipped；跳过项依赖未配置的数据库或其他运行环境 |
| 原有常见问题 | 30/30，核对完整计划和独立 CSV 重算结果，规则零模型调用 |
| 原有规则与边界多轮链 | 20 条链、70 个问句通过；包括草稿补答、继承、取消和权限边界 |
| 补充语义题集 | 共 28 问；20 道支持题中规则正确完成 13 道，其余 7 道未配置模型而要求澄清；8 道边界题全部正确 |
| 错误规则直出 | 补充题集中 0 次；13 个成功查询的计划及 CSV 结果均正确 |
| 模型兜底 mock | 检查完整原文、否定作用范围、手动条件、全部指标权限、无效响应、单次调用及评测总预算，不代表真实模型理解正确率 |
| Go | go vet ./...、go test ./... -count=1 均通过 |
| 前端 | npm run build 通过 |
| 文档及补丁 | git diff --check 通过 |

补充题集保持 draft 标注状态，尚未标记人工复核。规则覆盖之外的七道支持题计入分母，不将澄清排除后报告完成率。当前环境没有模型密钥，Docker 引擎未运行；真实模型及数据库集成验证明确保留为未完成项。

## 复现

在仓库根目录执行：

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
.\.venv\Scripts\python.exe -m pytest agent/tests -q -p no:cacheprovider --tb=short
.\.venv\Scripts\python.exe scripts/evaluate_query_parsing.py --output .cache/query-parsing-rules.json
go vet ./...
go test ./... -count=1
```

在 web 目录执行 npm run build。Windows 沙箱限制临时目录访问时，需要允许测试进程使用正常的临时目录；本次全量测试在该权限允许后执行。

真实模型模式需在当前进程配置 LLM_API_KEY、LLM_MODEL、LLM_BASE_URL，以及网关所需的可信 CA 配置，再显式执行：

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_query_parsing.py --live --rounds 3 --max-model-calls 120 --output .cache/query-parsing-live.json
```

评测总预算最多 120 次出站请求，包含全部轮次；达到预算时保留已有记录并标记 incomplete，不报告全部执行完成。该脚本通过实际 Python 查询入口执行，以内存 SQLite 承接经过安全校验的 SQL，再用原始 CSV 独立重算黄金结果；范围不包含 MySQL 方言、数据库权限、Go 持久化、界面或 ECS 发布验收。

## 证据

实现提交：`6b9d2dc880b16c219a3744faeddeb7689852342c`。

原始记录见 [规则模式评测](evidence/query-parsing-rules-20261015.json)，题集见 [补充语义题集](../evals/understanding/parsing-cases.json)。记录包含全部 28 问、失败原因、模型和 SQL 调用数、源码提交号及源码未修改标志，以及题集、运行时和 CSV 夹具摘要。后续真实模型报告应单独保存，不覆盖本次规则模式证据。
