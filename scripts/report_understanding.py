"""展示只从经身份校验的实测报告生成；验收题不作为公开演示题。"""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.understanding.dataset import load
from evals.understanding.report import digest, summarize


def fraction(value):
    rate = f"{value['rate']:.1%}" if value["rate"] is not None else "无样本"
    return f"{value['correct']}/{value['total']}（{rate}）"


def markdown(study, report_hash):
    summary = summarize(study)
    identity = study["identity"]
    created = datetime.fromisoformat(study["created_at"])
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    local_time = created.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    suite = {c.id: c.model_dump() for c in load().cases}
    selected = [suite[case_id] for case_id in identity["case_ids"]]
    lines = ["# 用户意图理解实测证据", "", f"报告生成时间：{local_time}（上海时间）。代码提交：`{identity['source_commit']}`。",
             f"报告 SHA256：`{report_hash}`。题集 SHA256：`{identity['dataset_hash']}`。",
             "", f"数据为独立合成 CSV 订单夹具；分析基准 {identity['analysis_date']}，分区 {identity['split']}。包含 {len(selected)} 条表达、{len({c['group'] for c in selected})} 个意图组。",
             "", "题单复核状态：" + ("人工已复核。" if summary["human_reviewed"] else "待人工复核；统计用于展示当前实现的表现，尚不能称为人工复核验收正确率。"),
             "", "统计只适用于本题集，真实 Olist 数据核对另行报告；源码夹具评测不能替代发布镜像验收。模型供应商权重版本不可确定。", "",
             "## 同题对照结果", "", "| 轮次 | 仅规则正确完成 | 完整模式正确完成 | 模型补足 | 退化 | 完成率差 |", "| --- | --- | --- | --- | --- | --- |"]
    for index, value in enumerate(summary["rounds"], 1):
        paired = "full" in value
        gain = f"{value['accuracy_gain'] * 100:.1f} 个百分点" if paired and value["accuracy_gain"] is not None else "未执行"
        lines.append(f"| {index} | {fraction(value['rules_only']['supported'])} | {fraction(value['full']['supported']) if paired else '未执行'} | {len(value['rescued_ids']) if paired else '未执行'} | {len(value['regressed_ids']) if paired else '未执行'} | {gain} |")
    lines += ["", "模型补足只统计仅规则未正确完成、完整模式计划及结果均正确且确实调用模型的同一道题。失败与拒答保留在支持题分母中。", "", "## 模型理解与条件完整性", "",
              "| 轮次 | 模型路径意图正确 | 模型路径结果正确 | 模型路径完整正确 | 可观察计划 | 意图或条件错误 | 计划不可观察 |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for index, value in enumerate(summary["rounds"], 1):
        mode = value.get("full", value["rules_only"])
        lines.append(f"| {index} | {fraction(mode['model_intent'])} | {fraction(mode['model_result'])} | {fraction(mode['model_complete'])} | {mode['plan_observed']} | {mode['condition_error_cases']} | {mode['plan_unobservable']} |")
    lines += ["", "未返回计划的失败不会虚构成条件遗漏；模型路径意图和结果指标以所有调用模型的支持题为分母。", "", "## 边界与分类结果", "",
              "| 轮次 | 常用表达 | 陌生表达 | 缺条件 | 冲突 | 范围外 | 权限 |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for index, value in enumerate(summary["rounds"], 1):
        mode = value.get("full", value["rules_only"])
        lines.append(f"| {index} | " + " | ".join(fraction(mode["categories"][kind]) for kind in ("common", "unfamiliar", "missing", "conflict", "unsupported", "permission")) + " |")
    lines += ["", "## 用量与稳定性", "", "| 轮次 | 模型请求 | 已知输入 Token | 已知输出 Token | 已知总 Token | 未知用量请求 | 重试 | 任务 P95 毫秒 |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for index, value in enumerate(summary["rounds"], 1):
        mode = value.get("full", value["rules_only"])
        latency = mode["task_latency_ms"]["p95"]
        lines.append(f"| {index} | {mode['model_calls']} | {mode['known_input_tokens']} | {mode['known_output_tokens']} | {mode['known_total_tokens']} | {mode['unknown_usage_calls']} | {mode['retries']} | {latency:.1f} |")
    stability = summary["stability"]
    lines += ["", f"共 {stability['rounds']} 轮，所有轮次均通过的题目 {stability['all_passed']}/{len(selected)}；表现波动的案例：" + ("、".join(stability["flaky_ids"]) or "无") + "。",
              "未知用量没有补成零费用；当前不提供推算账单。延迟来自本次隔离夹具，包含排队和任务轮询，不代表线上持续压力表现。", "", "## 可公开复查的开发案例", ""]
    pair = study["rounds"][0]
    rules = {c["id"]: c for c in pair["rules_only"]["cases"]}
    cases = pair.get("full", pair["rules_only"])["cases"]
    public = [c for c in cases if suite[c["id"]]["split"] == "development"]
    examples = []
    for predicate in (lambda c: c["passed"] and c["parser"] == "rules" and c["category"] in ("common", "unfamiliar"),
                      lambda c: c["passed"] and c["model_calls"] > 0 and not rules[c["id"]]["passed"],
                      lambda c: c["passed"] and c["category"] not in ("common", "unfamiliar"),
                      lambda c: not c["passed"]):
        match = next((c for c in public if predicate(c) and c not in examples), None)
        if match:
            examples.append(match)
    for c in examples:
        annotated = suite[c["id"]]
        lines += [f"### {c['id']}", "", annotated["question"], "", f"结果：{'通过' if c['passed'] else '失败'}；解析路径 {c['parser']}；模型调用 {c['model_calls']}；错误类别 {c['error_code'] or '无'}。"]
        lines += ["", "预期：", "", "```json", json.dumps(annotated["expected"] or {"status": annotated["expected_status"], "reason": annotated["reason"]}, ensure_ascii=False, indent=2), "```"]
        if c["differences"]:
            lines += ["", "字段差异：", "", "```json", json.dumps(c["differences"], ensure_ascii=False, indent=2), "```"]
    if not examples:
        lines += ["本报告没有开发分区的可展示题，验收题未在此公开。"]
    lines += ["", "## 失败分布", ""]
    failures = [c for c in cases if not c["passed"]]
    if failures:
        lines += ["| 案例 | 类别 | 状态 | 错误类别 | 可观察差异 |", "| --- | --- | --- | --- | --- |"]
        for c in failures:
            differences = ", ".join(d["field"] + ":" + d["kind"] for d in c["differences"])
            if not differences:
                differences = "计划一致但结果不正确" if c["intent_correct"] is True else "计划不可观察" if c["intent_correct"] is None else "协议或计划错误"
            lines.append(f"| {c['id']} | {c['category']} | {c['task_status']} | {c['error_code'] or '无'} | {differences} |")
    else:
        lines += ["本轮没有失败案例。"]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        text = markdown(json.loads(args.report.read_text(encoding="utf-8")), digest(args.report))
    except (ValueError, KeyError, TypeError):
        print("Invalid or incomplete understanding report", file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
