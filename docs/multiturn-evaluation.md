# 多轮问数复现与验收

先阅读 [已确认规格](controlled-multiturn-spec.md)和[实测证据](multiturn-evidence.md)。题集为 `evals/multiturn/chains.json`，不要用解析器输出改写期望；人工复核后才可修改 draft 状态并署名。`author.py`保存显式标注来源，评测时不运行。

## 隔离配对评测

使用独立测试 PostgreSQL/MySQL，设置 `TEST_DATABASE_URL`、`MYSQL_TEST_HOST`、`MYSQL_TEST_PORT`、`MYSQL_TEST_USER`、`MYSQL_TEST_PASSWORD`。评测器创建随机前缀 schema、只读账号及临时身份，结束时精确清理；不连接 ECS 应用数据库。参与评测的业务代码须先提交，未提交业务修改会拒绝运行。

```powershell
go build -o .cache/evalfixture-multiturn.exe ./cmd/evalfixture
.venv/Scripts/python.exe scripts/evaluate_multiturn.py --fixture-server .cache/evalfixture-multiturn.exe --split all --output .cache/multiturn/rules.json
```

真实模型显式设置 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL` 和必要的 `LLM_CA_FILE`，TLS 验证保持开启。不要在命令或报告中写凭据。

```powershell
.venv/Scripts/python.exe scripts/evaluate_multiturn.py --fixture-server .cache/evalfixture-multiturn.exe --split all --live --rounds 3 --max-model-calls 120 --output .cache/multiturn/live.json --details-dir .cache/multiturn/details
.venv/Scripts/python.exe scripts/summarize_multiturn.py .cache/multiturn/live.json
```

两种模式共用一个出站观测代理，每轮 120 次是成对总预算，含 Go 重试。最多三轮；达到预算保留 incomplete 报告，不能当通过。重复诊断须从剩余预算扣除。详细任务结果只写 ignored `.cache`；展示报告省去原文、SQL 及数值行。

`--split development`、`--split acceptance`按整链选择；`--chain-id chain-16`等单链诊断不能替代完整验收。离线汇总要求声明分区的链全部存在，拒绝把单链当整分区结果。两分区各 15 链，当前 51/49 问。

## 验证与判定

每轮核对终态、完整 QueryPlan、原始 CSV 数值、未修改字段及持久计划。澄清轮还检查 missing 和已明确 Top N；取消操作检查计划未变、草稿清空。整链须每轮全部正确。前文期望来自黄金标注，实际状态由会话接口读取，不用上一实际结果生成下一黄金期望。

竞态和接口权限由真实 PostgreSQL 集成测试验证，不依赖模型随机输出：

```powershell
go test ./... -count=1
go vet ./...
.venv/Scripts/python.exe -m pytest agent/tests -q
cd web
npm run build
```

MySQL 集成测试要求上述环境变量。未配置外部依赖的跳过项如实报告，不计为通过。旧单轮 95 题门禁及 90 题表达评测各保留原版本。

## 独立 Olist 与浏览器复核

使用含当前已提交 runtime 的只读 Agent Compose，只需授权 MySQL 视图，不运行新 Go 迁移。分析基准须为 `2018-08-31`，原始 CSV 位于 `data/olist`。

```powershell
.venv/Scripts/python.exe scripts/verify_multiturn_olist.py --compose-file <只读测试配置> --with-model --output .cache/multiturn/olist.json
```

默认两条规则链；`--with-model`增加一条陌生表达链、一次模型调用。服务 key 在容器内读取，模型 key 不进入输出。SQL 进入执行阶段来自运行时遥测，与合成配对评测的出站计数分别标注。复测不得删除首次失败或未知用量。

浏览器依次输入：最近半年 GMV 按月统计 → 改成按州 → 只看两个州，取前三名 → SP 和 RJ，按 GMV 从高到低。第三轮澄清；第四轮继承半年、GMV 和前三名，最多返回两个州。刷新应恢复四轮，当前条件另行显示。

再验证取消草稿、手动选择提交后清空和旧历史入口。两个窗口修改同一会话，旧版本提交须拒绝并保留输入；点击“按当前条件继续”后才重发。未理解筛选后改变分组仍须保留草稿，不能执行少条件查询。
