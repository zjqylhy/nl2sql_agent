"""检查配对身份、固定分母和缺题；未知用量与失败不会被排除。"""
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re

from .dataset import DATASET, load

ROOT = Path(__file__).resolve().parents[2]
IDENTITY_FIELDS = ("source_commit", "dataset_hash", "annotation_status", "reviewer", "fixture_hash", "scorer_hash",
                   "runtime_hash", "executor_hash", "model_configuration_hash", "analysis_date", "split", "case_ids", "fixture_kind", "fixture_server_hash")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def text_hashes(path):
    # 历史报告保留运行时字节摘要，只兼容 Git 的 LF/CRLF 转换。
    raw = path.read_bytes()
    normalized = raw.replace(b"\r\n", b"\n")
    return {hashlib.sha256(value).hexdigest() for value in (raw, normalized, normalized.replace(b"\n", b"\r\n"))}


def fingerprints(paths):
    value = hashlib.sha256()
    for path in sorted(paths):
        value.update(path.relative_to(ROOT).as_posix().encode())
        value.update(path.read_bytes())
    return value.hexdigest()


def validate(study):
    if study.get("report_version") != 1 or study.get("status") != "complete":
        raise ValueError("incomplete or unknown study")
    identity = study["identity"]
    if not re.fullmatch(r"[0-9a-f]{40}", identity["source_commit"]):
        raise ValueError("full source commit required")
    data = load()
    if identity["split"] not in ("development", "acceptance", "all", "diagnostic") or not identity["case_ids"]:
        raise ValueError("invalid or empty selection")
    if identity["fixture_kind"] != "synthetic_order_facts" or identity["fixture_hash"] not in text_hashes(ROOT / "evals/fixture.csv"):
        raise ValueError("unexpected fixture identity")
    if identity["dataset_hash"] not in text_hashes(DATASET) or identity["annotation_status"] != data.annotation_status or identity["reviewer"] != data.reviewer:
        raise ValueError("dataset or annotation identity mismatch")
    suite = [c for c in data.cases if identity["split"] == "all" or c.split == identity["split"] or identity["split"] == "diagnostic" and c.id in identity["case_ids"]]
    expected = {c.id: c for c in suite}
    if len(identity["case_ids"]) != len(set(identity["case_ids"])) or set(identity["case_ids"]) != set(expected):
        raise ValueError("missing or duplicate selected cases")
    rounds = study["rounds"]
    if not rounds or len(rounds) > 3 or study["mode"] not in ("rules_only", "paired_live") or identity["analysis_date"] != data.analysis_date:
        raise ValueError("invalid study execution")
    if identity["split"] == "diagnostic" and not identity["case_ids"]:
        raise ValueError("empty diagnostic study")
    for index, pair in enumerate(rounds, 1):
        modes = ("rules_only", "full") if study["mode"] == "paired_live" else ("rules_only",)
        if set(pair) != set(modes):
            raise ValueError("missing paired mode")
        for mode in modes:
            report = pair[mode]
            if report["status"] != "complete" or report["mode"] != mode or report["round"] != index:
                raise ValueError("invalid round status")
            if any(report["identity"][field] != identity[field] for field in IDENTITY_FIELDS):
                raise ValueError("paired identities differ")
            cases = report["cases"]
            if len(cases) != len(expected) or {c["id"] for c in cases} != set(expected):
                raise ValueError("missing or duplicate cases")
            for c in cases:
                if c["category"] != expected[c["id"]].category or type(c["passed"]) is not bool:
                    raise ValueError("case contract mismatch")
                for field in ("model_calls", "sql_calls", "attempts", "unknown_usage_calls", "known_input_tokens", "known_output_tokens", "known_total_tokens"):
                    number = c[field]
                    if type(number) is not int or number < 0:
                        raise ValueError("invalid observation count")
                if not math.isfinite(c["duration_ms"]) or c["duration_ms"] < 0:
                    raise ValueError("invalid duration")
                if c["unknown_usage_calls"] > c["model_calls"] or mode == "rules_only" and c["model_calls"] != 0:
                    raise ValueError("invalid model usage")
                if c["category"] in ("common", "unfamiliar") and c["passed"] and (c["intent_correct"] is not True or c["result_correct"] is not True):
                    raise ValueError("successful answer has invalid verdict")
                if c["category"] not in ("common", "unfamiliar") and c["passed"] and c["boundary_correct"] is not True:
                    raise ValueError("successful boundary has invalid verdict")
            if sum(c["model_calls"] for c in cases) != report["issued_calls"] or report["issued_calls"] > 120:
                raise ValueError("budget or observation mismatch")
    return study


def ratio(numerator, denominator):
    return {"correct": numerator, "total": denominator, "rate": numerator / denominator if denominator else None}


def quantile(values, fraction):
    if not values:
        return None
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def mode_summary(cases):
    supported = [c for c in cases if c["category"] in ("common", "unfamiliar")]
    models = [c for c in supported if c["model_calls"] > 0]
    observed = [c for c in supported if c["intent_correct"] is not None]
    return {"supported": ratio(sum(c["passed"] for c in supported), len(supported)),
            "rules_complete": ratio(sum(c["passed"] and c["parser"] == "rules" and c["model_calls"] == 0 for c in supported), len(supported)),
            "model_intent": ratio(sum(c["intent_correct"] is True for c in models), len(models)),
            "model_result": ratio(sum(c["result_correct"] is True for c in models), len(models)),
            "model_complete": ratio(sum(c["passed"] for c in models), len(models)),
            "plan_observed": len(observed), "plan_unobservable": len(supported) - len(observed),
            "condition_error_cases": sum(c["intent_correct"] is False for c in observed),
            "condition_errors": dict(Counter(d["kind"] for c in observed for d in c["differences"])),
            "categories": {kind: ratio(sum(c["passed"] for c in cases if c["category"] == kind), sum(c["category"] == kind for c in cases)) for kind in ("common", "unfamiliar", "missing", "conflict", "unsupported", "permission")},
            "model_calls": sum(c["model_calls"] for c in cases),
            "known_input_tokens": sum(c["known_input_tokens"] for c in cases), "known_output_tokens": sum(c["known_output_tokens"] for c in cases),
            "known_total_tokens": sum(c["known_total_tokens"] for c in cases), "unknown_usage_calls": sum(c["unknown_usage_calls"] for c in cases),
            "retries": sum(max(0, c["attempts"] - 1) for c in cases),
            "task_latency_ms": {"sample_size": len(cases), "p50": quantile([c["duration_ms"] for c in cases], .5), "p95": quantile([c["duration_ms"] for c in cases], .95)}}


def summarize(study):
    validate(study)
    results = []
    for pair in study["rounds"]:
        value = {mode: mode_summary(report["cases"]) for mode, report in pair.items()}
        if "full" in pair:
            rules = {c["id"]: c for c in pair["rules_only"]["cases"]}
            supported = [c for c in pair["full"]["cases"] if c["category"] in ("common", "unfamiliar")]
            value["rescued_ids"] = [c["id"] for c in supported if not rules[c["id"]]["passed"] and c["passed"] and c["model_calls"] > 0]
            value["regressed_ids"] = [c["id"] for c in supported if rules[c["id"]]["passed"] and not c["passed"]]
            value["accuracy_gain"] = value["full"]["supported"]["rate"] - value["rules_only"]["supported"]["rate"] if supported else None
        results.append(value)
    mode = "full" if study["mode"] == "paired_live" else "rules_only"
    passes = {case_id: [next(c["passed"] for c in pair[mode]["cases"] if c["id"] == case_id) for pair in study["rounds"]] for case_id in study["identity"]["case_ids"]}
    return {"rounds": results, "stability": {"rounds": len(results), "all_passed": sum(all(v) for v in passes.values()),
            "flaky_ids": [k for k, v in passes.items() if len(set(v)) > 1]},
            "human_reviewed": study["identity"]["annotation_status"] == "human_reviewed",
            "live_acceptance_complete": study["mode"] == "paired_live" and len(results) == 3 and study["identity"]["split"] in ("acceptance", "all") and study["identity"]["annotation_status"] == "human_reviewed"}


def save(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
