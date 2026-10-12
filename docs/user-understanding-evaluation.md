# 用户意图理解评测运行说明

新评测用相同题集、源码提交和独立 CSV 夹具，对照仅规则与规则加真实模型，统计模型补足的表达数量。题集共有 90 条表达、75 个意图组；开发集 49 条、验收集 41 条。当前题单为待人工复核草稿，不代表真实用户分布。

预期由业务规格逐条编写，数值通过 `evals/oracle.py` 从原始 CSV 独立计算。意图、数值和边界原因分别评分；SQL 成功执行不是正确答案的判据。评测不修改生产解析器、提示词、冻结 CI 题集或门槛。

## 隔离测试环境

需要现有 Python 虚拟环境、Go 和 Docker Desktop。必须使用测试数据库；以下 Compose 创建仅在本机开放端口的双库，数据使用临时内存文件系统，关闭容器后丢弃。

```powershell
# 该值只用于独立测试容器，不得使用生产密码。
$env:UNDERSTANDING_TEST_PASSWORD = 'test-only-understanding'
docker compose -p understanding-test -f deploy/compose.understanding-test.yaml up -d --wait
$env:TEST_DATABASE_URL = "postgres://test:$($env:UNDERSTANDING_TEST_PASSWORD)@127.0.0.1:15432/test?sslmode=disable"
$env:MYSQL_TEST_HOST = '127.0.0.1'
$env:MYSQL_TEST_PORT = '13306'
$env:MYSQL_TEST_PASSWORD = $env:UNDERSTANDING_TEST_PASSWORD
go build -o .cache/evalfixture-understanding.exe ./cmd/evalfixture
```

端口被占用时先设置 `UNDERSTANDING_POSTGRES_PORT` 或 `UNDERSTANDING_MYSQL_PORT`，同时调整评测连接变量。Linux 对应使用 shell 的 export 与 `.cache/evalfixture-understanding`。

运行前提交评测相关改动。入口拒绝未提交的 runtime、题集、scorer 和执行器，报告记录完整 Git SHA、运行时和执行器摘要，以及实际 fixture 二进制摘要。

## 仅规则与真实模型对照

```powershell
# 不调用模型，但也执行全部陌生表达，保留无法理解的失败。
.\.venv\Scripts\python.exe scripts/evaluate_understanding.py `
  --fixture-server .cache/evalfixture-understanding.exe --split all `
  --output .cache/understanding/rules.json

# 模型入口与 CA 使用已确认的网关配置，当前 Windows 入口需要 SSH 隧道。
$env:LLM_BASE_URL = 'https://localhost:9443/v1'
$env:LLM_MODEL = 'chat'
$env:LLM_CA_FILE = (Resolve-Path .cache/gateway-server-ca.pem).Path
$env:LLM_API_KEY = [Environment]::GetEnvironmentVariable('NL2SQL_AGENT_API_KEY', 'User')
.\.venv\Scripts\python.exe scripts/evaluate_understanding.py `
  --fixture-server .cache/evalfixture-understanding.exe --split all --live --rounds 3 `
  --max-model-calls 120 --output .cache/understanding/paired-live.json `
  --details-dir .cache/understanding/details
$env:LLM_API_KEY = $null
```

每轮先执行仅规则，再执行完整模式；两种模式共用代码、题集、权限配置、CSV 和基准日期。真实模型最多三轮，每轮最多 120 次请求，出站前限制预算，重试也计入。预算耗尽或观测不完整时报告为 incomplete，不生成验收结论。供应商失败保留在支持题分母中。

`--split development` 用于开发，`--split acceptance --live --rounds 3` 用于冻结验收；默认只跑开发分区一轮。`--case-id` 可重复指定，生成 diagnostic 报告，不能替代完整验收。三轮全题集实测用于本次能力盘点；从中调优过的验收问题需要转为回归并补题，不能继续称为未见验收题。

详细结果只能写入忽略的 `.cache` 目录。默认报告不含问题、SQL、凭据、供应商响应或私有数据；逐题字段差异只涉及人工构造问题的受控计划。报告中未知 Token 调用与已知 Token 总和并列，不将未知用量补成零费用。

## 展示与复核

```powershell
.\.venv\Scripts\python.exe scripts/report_understanding.py `
  --report .cache/understanding/paired-live.json --output .cache/understanding/summary.md
```

生成器核对题集、分区、同题配对身份、完整轮次、预算和判定后输出各轮分子与分母。模型补足只计仅规则未正确完成、完整模式计划及结果均正确且确实调用模型的题；同时显示退化、计划不可观察、条件错误、边界原因、用量和波动。

新报告与 Markdown 使用 LF 保存，与仓库约定一致。历史报告保留执行时的原始摘要；题集和夹具校验仅兼容 LF 与 CRLF 的字节转换，其他内容变化仍拒绝。展示中的报告 SHA256 对应仓库公开文件。

公开示例只选开发分区，包括规则成功、模型补足、边界拦截和失败。验收题不在展示摘要中公开。完整题单仍在仓库供人工复核，不能将它描述为秘密或外部机构评测。

人工复核需检查题意、预期计划、边界原因和分区；复核后记录 reviewer 与 annotation_status。题集摘要会随之改变，旧报告不能改名为新题集验收报告，须重新运行。

本次证据见 [实测结果](user-understanding-evidence.md)。真实 Olist 数据使用已有 `scripts/verify_semantic.py` 单独核对，不能与合成夹具合并成一个正确率。

## 清理与验证

```powershell
.\.venv\Scripts\python.exe -m pytest agent/tests -q
go vet ./...
go test ./... -count=1
docker compose -p understanding-test -f deploy/compose.understanding-test.yaml down
```

评测正常或异常退出均清理本轮随机 MySQL schema/账号和 PostgreSQL schema；Windows 子进程强制结束时由 fixture 的受限清理入口处理精确前缀。操作系统直接终止整个父进程时仍可能留下测试数据，确认容器属于本次临时 Compose 项目后关闭它即可，不对运行中的项目数据库执行清理。
