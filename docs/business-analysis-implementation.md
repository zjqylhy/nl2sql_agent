# 有限经营分析助手：实现与验证

> 本记录对应现有 Go/Python 版本。新的[Python 单栈目标架构](python-agent-target-spec.md)尚未实现，下面测试结果不能作为新架构的验收证据。

2026-10-17。用户已确认方案并授权实现。代码已在隔离分支实现和验证，等待人工审阅；未推送、未部署。本记录对应[原技术方案](business-analysis-assistant-design.md)，历史图中的“待实现”表示当时的设计状态。

## 1 实现边界与状态

| 部分 | 扩展前已有 | 本次已实现 | 尚待验收或扩展 |
| --- | --- | --- | --- |
| 授权问数 | QueryPlan、指标目录、SQL 编译、安全检查、MySQL 只读视图 | 抽出共享 `execute_plan`；`query_metrics` 直接接收原 QueryPlan，不再调用自然语言解析器 | 新指标和新维度不在本次范围 |
| 分析决策 | 单次规则或模型解析；受控多轮条件继承 | 单模型每次选择一个工具，根据已保存观察继续决策 | 真实模型的任务完成率待对照实测 |
| 确定性工具 | 排序和 Top N 查询 | 两期比较、下降排名、sum、加减乘除、变化率；检查单位、日期和覆盖集合 | 不接收自由 SQL、表达式 eval 或任意代码 |
| 输出 | 查询表、SQL、样本信息 | 下降州核查、Top N 合计、整体客单价及占比；受限 finding 与引用；说明降级 | 自由长报告和因果推断不在本次范围 |
| 失败处理 | 任务状态、租约、outbox、停止、SSE | 有限参数修正、只读 SQL 重试、澄清、未知调用停止、预算账本 | 线上故障演练与容量验收待发布阶段执行 |
| 恢复 | 原单步任务重试、多轮成功计划/草稿 | 步骤输入、回执、结果哈希、版本围栏、已完成动作缓存 | 不承诺跨 PostgreSQL、MySQL 和模型网关的 exactly-once |
| 页面 | 问数及会话工作台 | 经营分析入口、私有历史、步骤、依据、补充、停止、失败部分结果 | 生产配置仍默认关闭 |
| 独立评测 | 既有问数/多轮题集与隔离建库器 | 新题集、独立 CSV oracle、A/B/C 执行器、分类统计和配对差异报告 | 当前为 80 条表达、40 意图组的试点；人工复核和扩充至方案建议的 80 意图组待完成 |

实现沿用 Go 编排、PostgreSQL 单一状态源、Python 无状态工具、MySQL 授权视图和 React 页面。未引入 LangGraph、多 Agent 或第四个项目。

## 2 调用链和关键代码

`React → Go analysis-runs → 持久化步骤/预留 → Python decide 或 tool → 原子保存回执 → 再次入队`。

每次领取只执行一个决策或工具。保存失败时不启动下一动作。最终 `finish` 仍由代码检查全部必需证据，模型不能自行把任务标为成功。

| 路径 | 责任 |
| --- | --- |
| `agent/runtime/analysis.py` | 规格、工具协议、Decimal 计算、覆盖检查、完成判定、受限说明 |
| `agent/runtime/analysis_api.py` | 指标目录、查询适配、模型决策和安全错误回执 |
| `agent/runtime/analysis_fixed.py` | C 组固定依赖顺序；只在首次提取规格时调用模型 |
| `internal/store/migrations/004_analysis.sql` | run、step、attempt、artifact、budget ledger、answer 幂等表 |
| `internal/store/analysis.go` | 原子预算、检查点、恢复、权限、澄清、过期与清理 |
| `internal/api/analysis.go` | 对外 API、可信身份注入、当前绑定检查、单步调度与取消 |
| `web/src/Analysis.tsx` | 新分析工作区 |

工具集为 `lookup_metrics / query_metrics / compare_periods / rank_declines / calculate / request_clarification / finish`。身份、数据源、授权指标由 Go 注入，模型不能指定租户、账号、SQL 或数据源。

`query_metrics.arguments.plan` 复用原 QueryPlan：metrics、dimensions、time_range、filters、order_by、top_n。分析模式额外约束已确认的时间、筛选、指标和州范围。比较排名前禁止 Top N 裁剪；补查只能在原条件上收窄到已选州。

Top N 查询一次读取完整州候选，再用代码选前 N；最多 27 州，不增加额外 SQL。保留 `candidate_rows` 与质量标记，防止被裁剪掉的未知金额破坏排名。GMV 合计只针对所选州；整体客单价按所选州 GMV 总额 / 有效订单总数重算，不能把各州客单价求和或简单平均。

模型在 `finish.findings` 中选择陈述类型、引用及顺序。代码校验真实方向并填入数字，不接受自由数字或因果解释。说明无效或缺失时输出确定性说明，并记录 `narrative_fallback=true`。两期天数不同会提示原始总量未换算日均；不足 N 州下降、无下降和不可比较州分别说明。

## 3 失败、恢复与预算

| 观察或中断 | 实际处理 |
| --- | --- |
| 缺日期、N、下降金额/比例等条件 | 模型选澄清工具；保存等待状态，用户以 request_id + expected_version 补充；最多两轮，24 小时过期 |
| 参数或模型 JSON 错误 | 整个 run 最多一次模型修正；不得改已冻结规格 |
| 无数据或必需证据不足 | 保留依据和 no_data 标记，不扩大范围、不补零；允许原规格内补查；同一无进展完成请求连续两次后停止 |
| SQL 超时、临时连接错误 | 整个 run 共用一次只读重试；仍计 SQL、步骤和活跃时长 |
| SQL 回执未知 | 延迟后才允许这一次重试，留出旧调用上界与租约过期时间；再次未知则停止 |
| 模型请求已发出、回执未知 | 停止，不自动重发；预留 Token/费用继续占额 |
| 结果保存失败 | 不推进状态；租约过期后识别 running 步骤，按未知执行处理；纯计算可重算 |
| 已完成动作恢复或再次提出 | 相同绑定、规格和参数下复用已持久化回执；SQL 不重复计发 |
| 用户停止、撤权、数据绑定变化 | 拒绝继续和迟到写回；历史、详情及 SSE 同样检查权限与绑定 |
| 旧 worker 回执 | 任务执行版本和 active_step 不匹配，拒绝写入 |

实际限制：模型决策最多 10 次、工具动作最多 12 次、SQL 最多 6 次，SQL 重试计入其中；活跃执行 180 秒、最多 40 个阶段记录；Token 预留与已知消耗合计不超过 30000，分析并发为 2。等待澄清和重试退避不占工作槽。

说明在最后一次 `finish` 决策中选择 finding，不额外调用一次说明模型。因此本实现总模型上限为 10 次，比原方案的“10 次决策 + 1 次说明”更紧。每次预留 6000 输入 + 1024 输出，实际已知用量后结算；未知用量不返还。供应商用量超过上界时立即停止并保留未知费用。

`byte_bpe_upper_bound` 使用 UTF-8 字节数 + 固定消息封装余量作为保守输入估算，不冒充精确 tokenizer。只允许维护者确认实际模型为兼容 byte-BPE、网关不附加超出余量的隐藏上下文后启用。未知模型映射保持关闭；实际供应商账单仍须单独核对。价格、货币和版本随 run 冻结。

`dataset_revision` 必须代表实际发布的不可变订单快照，而不是随意字符串。配置变更会改变 binding；数据库若在同一版本名下被管理员原地修改，应用无法检测，因此发布流程必须禁止这种更新。视图/行权限变更同时递增原有 `context_revision`；工具协议变化需更新 `VERSION`。

等待输入由 Worker 每分钟封存过期任务。`prune-analysis` 每次清理最多 100 个超过 30 天的终态任务详细步骤及原始依据，保留最终报告和汇总用量；页面明确标记依据已过期，终态不能恢复。

## 4 API 与启用

原问数和会话接口继续使用原协议。新增：

| 方法与路径 | 功能 |
| --- | --- |
| `GET /api/analysis-capabilities` | 当前功能开关 |
| `POST /api/analysis-runs` | question、request_id，可选已确认 spec；创建或幂等返回 |
| `GET /api/analysis-runs` | 仅本人可见且当前仍有权限的分析历史 |
| `GET /api/analysis-runs/{id}` | 规格、步骤、依据、预算和状态 |
| `POST /api/analysis-runs/{id}/answers` | answer、request_id、expected_version |
| `POST /api/analysis-runs/{id}/resume` | 仅核对 pending 和版本，不重置预算、不绕过退避、不重启终态 |
| 原 `tasks/{id}/events` 与 `tasks/{id}/stop` | 分析 SSE 与停止；同样按本人和依赖指标鉴权 |

需先以迁移身份执行迁移 004，再对运行身份补齐新表最小授权。`scripts/private_ops.py` 的授权/备份表集已包含全部新增表。本次仅修改部署模板，未对任何远端数据库迁移或授权。

配置见 `.env.example`。启用前配置 `ANALYSIS_ENABLED=true`、`ANALYSIS_STRATEGY=adaptive`、tokenizer 映射、实际 input/output price、currency、price version 和每任务 max cost；Go 和 Python 的 `LLM_MODEL` 必须一致。`fixed` 只作为 C 组对照，不由终端用户选择。模型未配置、价格无效或不可变数据版本缺失时拒绝分析，不回退为不受控 SQL。

从实际四份导入 CSV 生成版本：

```powershell
$env:PYTHONPATH='agent'
.venv/Scripts/python.exe -m tools.analysis_revision --csv-dir data/olist
```

把结果填入可信 `TENANT_DATASOURCES` 对应角色的 `dataset_revision`；确认导入/受限视图与这份快照一致。不能仅复制示例 hash 启用。

保留期清理命令：`api -command prune-analysis`。备份、回滚和远端部署仍使用原项目流程；需在代码验收及真实模型评测后再执行。

## 5 验证结果

| 检查 | 结果与覆盖 |
| --- | --- |
| `go test ./... -count=1` | 通过；真实隔离 PostgreSQL，包含幂等、私有结果、版本围栏、SQL 重试、未知模型、撤权、停止迟到回执、预算超界、保留期和持久化失败注入 |
| `go vet ./...` | 通过 |
| `pytest agent/tests -q` | **334 passed，7 skipped**；跳过项为 Windows 无法执行的 Linux symlink/Bash 发布用例 |
| 实际 MySQL 工具流水线 | 新建只读授权视图；查询→合计→有效订单合计→相除→占比→finish，全程真实 SQL；两种导入授权模式均通过 |
| `npm --prefix web run build` | TypeScript 与 Vite 生产构建通过；保留原有 ECharts 537.6 kB 分包告警 |
| 浏览器验收 | 本地模拟模型 + 真实 PostgreSQL/MySQL：两期下降、Top N 合计、整体客单价、补充后继续、刷新复用、模型未知停止；不是模型准确率实测 |
| 新评测夹具烟测 | 独立双库建库→迁移→登录→capabilities→清理通过，供应商模型调用为 0 |
| 题集审计 | 数据结构、按意图组分区、独立预期及统计分母检查通过；36 个有明确规格的意图组经真实 SQL 工具链与独立 CSV oracle 交叉核对通过，含澄清补全后的规格；不测模型意图识别，真实对照尚未运行 |

浏览器手工核对的订单夹具：SP 的 GMV 1000→720，订单 10→8，客单价 100→90；MG/RJ 的下降额均为 100，按州代码打破并列。7 月前三州 GMV 合计 2320，整体客单价为 2320/28≈82.86。模型未知夹具只发一次决策、SQL 为 0，保留 7024 Token 预留并显示“未完成”。刷新完成任务后，模型/SQL 次数不增加。

## 6 独立任务集和对照复现

`evals/analysis/cases.json` 为新任务集，不复用旧问数题目作分析效果证据；同组两种表达在同一分区。当前 40 个组、80 条表达，development/acceptance 各 40 条，标注为 **draft**。第二表达目前主要是指令包装改写，需人工补充自然表达和更严格的隐藏题；不能声称覆盖真实用户分布或已满足原方案建议的 80 个独立意图组。

`oracle.py` 只读原始订单 CSV，独立计算预期，不导入执行器或生产 SQL。`orders.csv` 和 `edge-orders.csv` 分别验证常规数据、金额缺失。包含比较金额/比例、筛选、Top N 合计、加权客单价、占比、澄清脚本、无数据和越界任务。故障/恢复由独立数据库测试覆盖，不混入模型效果准确率。

默认执行只审计题集，不调用模型：

```powershell
.venv/Scripts/python.exe scripts/evaluate_analysis.py
.venv/Scripts/python.exe scripts/run_analysis_study.py
```

真实对照必须先提交相关代码，并显式提供隔离测试 PostgreSQL DSN、MySQL 管理连接、模型环境变量和真实价格配置。MySQL 身份必须能为测试创建/删除随机 `test_eval_*` schema 与只读账号，不可指向生产。模型 Key 只通过进程环境传入。

```powershell
go build -o .cache/analysis-evalfixture.exe ./cmd/evalfixture
# 设置 TEST_DATABASE_URL、MYSQL_TEST_HOST/PORT/USER/PASSWORD、LLM_*、ANALYSIS_*。
# max-model-calls 为每种策略、每个数据夹具的显式调用上限。
.venv/Scripts/python.exe scripts/run_analysis_study.py --with-model --split development --repeats 3 --max-model-calls 300 --output .cache/analysis-study-development.json
```

脚本依次运行 A 原单步、B 模型逐步决策、C 首次模型提取规格后固定流程；每种策略及数据夹具新建测试身份、数据 schema 和任务，使用相同订单数据、题目和授权。`fixed` 不读取黄金标注，模型输入始终只有问题、用户补充和运行时观察。

统计包括完整任务分子/分母、参数正确率、重复或错误调用、超过必要调用下界的次数、P50/P95、全部尝试的已知 Token/费用、未知调用和预留估算。失败保留在分母，重复实验保存全部记录，不择优；SQL 超时不直接当作参数错误。A 返回多行不由判分器代为求和，但 N=1 且结果已完整回答时公平计分。配对差异按意图组聚类 bootstrap，不把同组改写和重复实验视作独立样本。

当前未运行上述真实模型实验：没有新增成功率、调用节省或成本优势结论。价格与模型确认、题集人工复核、隐藏任务扩充、真实 A/B/C 实测以及线上发布验收仍待完成。
