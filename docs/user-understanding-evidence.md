# 用户意图理解实测证据

报告生成时间：2026-10-12 23:42:12（上海时间）。代码提交：`32702ad4c0cc4b0643796bb6c5ec6853439b32e2`。
报告 SHA256：`eaa174c95829fb357cbd54f5a1a43a6dab7bc9c734e33a578271ea238cd6f3b7`。题集 SHA256：`e595808776f986059e3fb003f23c8ddca874f546fb614c06607030eb376e6112`。

数据为独立合成 CSV 订单夹具；分析基准 2018-08-31，分区 all。包含 90 条表达、75 个意图组。

题单复核状态：待人工复核；统计用于展示当前实现的表现，尚不能称为人工复核验收正确率。

统计只适用于本题集，真实 Olist 数据核对另行报告；源码夹具评测不能替代发布镜像验收。模型供应商权重版本不可确定。

## 同题对照结果

| 轮次 | 仅规则正确完成 | 完整模式正确完成 | 模型补足 | 退化 | 完成率差 |
| --- | --- | --- | --- | --- | --- |
| 1 | 30/60（50.0%） | 46/60（76.7%） | 16 | 0 | 26.7 个百分点 |
| 2 | 30/60（50.0%） | 48/60（80.0%） | 18 | 0 | 30.0 个百分点 |
| 3 | 30/60（50.0%） | 48/60（80.0%） | 18 | 0 | 30.0 个百分点 |

模型补足只统计仅规则未正确完成、完整模式计划及结果均正确且确实调用模型的同一道题。失败与拒答保留在支持题分母中。

## 模型理解与条件完整性

| 轮次 | 模型路径意图正确 | 模型路径结果正确 | 模型路径完整正确 | 可观察计划 | 意图或条件错误 | 计划不可观察 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 16/25（64.0%） | 16/25（64.0%） | 16/25（64.0%） | 46 | 0 | 14 |
| 2 | 18/25（72.0%） | 18/25（72.0%） | 18/25（72.0%） | 48 | 0 | 12 |
| 3 | 18/25（72.0%） | 18/25（72.0%） | 18/25（72.0%） | 48 | 0 | 12 |

未返回计划的失败不会虚构成条件遗漏；模型路径意图和结果指标以所有调用模型的支持题为分母。

## 边界与分类结果

| 轮次 | 常用表达 | 陌生表达 | 缺条件 | 冲突 | 范围外 | 权限 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 29/30（96.7%） | 17/30（56.7%） | 8/8（100.0%） | 6/6（100.0%） | 7/8（87.5%） | 8/8（100.0%） |
| 2 | 29/30（96.7%） | 19/30（63.3%） | 8/8（100.0%） | 6/6（100.0%） | 7/8（87.5%） | 8/8（100.0%） |
| 3 | 29/30（96.7%） | 19/30（63.3%） | 8/8（100.0%） | 6/6（100.0%） | 7/8（87.5%） | 8/8（100.0%） |

## 用量与稳定性

| 轮次 | 模型请求 | 已知输入 Token | 已知输出 Token | 已知总 Token | 未知用量请求 | 重试 | 任务 P95 毫秒 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 44 | 31381 | 14863 | 46244 | 1 | 18 | 19185.2 |
| 2 | 43 | 31238 | 14616 | 45854 | 0 | 17 | 17905.7 |
| 3 | 44 | 31585 | 14539 | 46124 | 0 | 18 | 16445.3 |

共 3 轮，所有轮次均通过的题目 73/90；表现波动的案例：unfamiliar-14、unfamiliar-18、unfamiliar-19、unfamiliar-21、unfamiliar-22。
未知用量没有补成零费用；当前不提供推算账单。延迟来自本次隔离夹具，包含排队和任务轮询，不代表线上持续压力表现。

## 可公开复查的开发案例

### common-01

本月 GMV

结果：通过；解析路径 rules；模型调用 0；错误类别 无。

预期：

```json
{
  "metrics": [
    "gmv"
  ],
  "dimensions": [],
  "time_range": {
    "start": "2018-08-01",
    "end_exclusive": "2018-09-01"
  },
  "filters": [],
  "order_by": [],
  "top_n": null
}
```
### unfamiliar-01

这个月卖出去的商品合计多少钱？按 GMV 的口径算。

结果：通过；解析路径 llm；模型调用 1；错误类别 无。

预期：

```json
{
  "metrics": [
    "gmv"
  ],
  "dimensions": [],
  "time_range": {
    "start": "2018-08-01",
    "end_exclusive": "2018-09-01"
  },
  "filters": [],
  "order_by": [],
  "top_n": null
}
```
### missing-01

GMV 按州统计

结果：通过；解析路径 unknown；模型调用 0；错误类别 clarification。

预期：

```json
{
  "status": "needs_clarification",
  "reason": "缺少时间"
}
```
### common-27

本月平均评分按州升序，取前 2 名

结果：失败；解析路径 unknown；模型调用 0；错误类别 clarification。

预期：

```json
{
  "metrics": [
    "review_score"
  ],
  "dimensions": [
    "state"
  ],
  "time_range": {
    "start": "2018-08-01",
    "end_exclusive": "2018-09-01"
  },
  "filters": [],
  "order_by": [
    {
      "field": "state",
      "direction": "asc"
    }
  ],
  "top_n": 2
}
```

## 失败分布

| 案例 | 类别 | 状态 | 错误类别 | 可观察差异 |
| --- | --- | --- | --- | --- |
| common-27 | common | needs_clarification | clarification | 计划不可观察 |
| unfamiliar-09 | unfamiliar | needs_clarification | clarification | 计划不可观察 |
| unfamiliar-10 | unfamiliar | needs_clarification | clarification | 计划不可观察 |
| unfamiliar-11 | unfamiliar | needs_clarification | clarification | 计划不可观察 |
| unfamiliar-12 | unfamiliar | failed | model_format | 计划不可观察 |
| unfamiliar-18 | unfamiliar | needs_clarification | clarification | 计划不可观察 |
| unfamiliar-21 | unfamiliar | needs_clarification | clarification | 计划不可观察 |
| unfamiliar-22 | unfamiliar | failed | model_format | 计划不可观察 |
| unfamiliar-24 | unfamiliar | failed | model_format | 计划不可观察 |
| unfamiliar-25 | unfamiliar | needs_clarification | clarification | 计划不可观察 |
| unfamiliar-26 | unfamiliar | needs_clarification | clarification | 计划不可观察 |
| unfamiliar-27 | unfamiliar | needs_clarification | clarification | 计划不可观察 |
| unfamiliar-28 | unfamiliar | failed | model_format | 计划不可观察 |
| unfamiliar-30 | unfamiliar | needs_clarification | clarification | 计划不可观察 |
| unsupported-06 | unsupported | needs_clarification | clarification | 计划不可观察 |

## 结果解读与复现依据

支持题中的规则完整正确覆盖率为 50%。模型主要补足指标口语、语序变化、州名和部分组合筛选，三轮额外正确完成 16、18、18 道支持题，完整正确率为 76.7%、80.0%、80.0%。这份更丰富的题集仍未达到项目原先的 85% 目标；不通过删题、改变预期或降低门槛隐藏失败。

有返回计划的支持题，计划及数值均与预期一致，因此本次可观察条件错误为零。这并不意味着所有表达都理解正确：未返回计划的支持题分别为 14、12、12 道，继续作为主指标失败。单个案例可能先得到无效模型输出，经任务重试后完成；调用和 Token 包含这些尝试。

失败包含两种不同限制：规则阶段的保守校验使部分原本支持的表达无法进入模型；模型返回的计划可能与既有约束不一致，或者输出被截断、无法解析。前者可见于按州代码升序和部分区间、枚举改写，后者可见于维度改写、否定及排名表达。需要结合本地详细结果中的澄清说明与遥测定位，不能仅凭没有计划判定模型遗漏了哪些条件。

范围外题有一题被拦截且未执行 SQL，但给出了时间冲突的澄清原因，没有明确说明不同范围 OR 查询的能力边界，因此严格判定失败。权限、缺条件和冲突题三轮全部通过。现有冻结回归题单与新表达题集分别解释回归和覆盖，不能混为同一个正确率。

原始可复查数据：[三轮安全报告](evidence/understanding-20261012.json)。报告只包含受控字段差异和遥测，不含凭据、SQL、原始供应商响应或私有数据。报告的代码提交是实际执行提交，后续文档提交不改写该身份。

运行命令及环境要求见 [评测运行说明](user-understanding-evaluation.md)。生成表格的命令为：

```powershell
.\.venv\Scripts\python.exe scripts/report_understanding.py `
  --report docs/evidence/understanding-20261012.json --output .cache/understanding/reproduced-summary.md
```

## 独立真实数据核对与代码验证

真实 Olist 原始 CSV 另行核对八个规则案例和一个模型案例，9/9 的数值、NULL、样本、分组和 Top N 一致；该模型案例用量为 1135 Token。验证使用临时只读容器加载当前源码与有效网关配置，连接既有只读授权视图；验证范围为 Python 到 MySQL，完整 Go 任务链路由上述隔离双库评测覆盖。未修改既有运行服务。数据摘要及逐例记录见 [Olist 核对报告](evidence/understanding-olist-20261012.json)，不能与合成夹具合并成总体正确率。

Python 全量回归 260 通过、7 个 Windows 不适用的 Linux 发布用例跳过；Go vet 及真实 PostgreSQL API/store 测试通过。变更前提交 77b44e6 与执行提交 32702ad 采用相同冻结题单、夹具和 oracle，各自确定性 95/95，原质量门禁通过，支持题 50/50，零下降。独立测试 Compose 配置校验通过。

题单预期和代码测试结果仍需人工验收。当前题单复核状态为 draft，已经用于此次能力盘点的验收题在后续调优时须转为回归并补题。受控多轮问数按下一阶段规格推进。
