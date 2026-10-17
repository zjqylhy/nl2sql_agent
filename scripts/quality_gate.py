"""发布判定与评测执行分离；缺失、不完整或不可比较的报告不能放行。"""
import argparse
import json
import hashlib
import sys
from pathlib import Path


def validate_report(report):
    if report.get("report_version") != 1 or report.get("status") != "complete":
        raise ValueError("report_incomplete")
    for key in ("dataset_hash", "fixture_hash", "scorer_version", "mode", "split"):
        if not report.get(key):
            raise ValueError("report_identity_missing")
    if report["mode"] not in ("deterministic", "live") or report["split"] not in ("development", "acceptance", "all"):
        raise ValueError("report_scope_invalid")
    cases = report.get("cases")
    if not isinstance(cases, list) or not cases or len({c.get("id") for c in cases}) != len(cases):
        raise ValueError("case_identity_invalid")
    for case in cases:
        if not isinstance(case.get("passed"), bool) or not case.get("id") or case.get("category") not in ("rules", "paraphrase", "clarification", "unsupported", "permission") or not isinstance(case.get("critical"), bool):
            raise ValueError("case_result_invalid")
    return {c["id"]: c for c in cases}


def decide(candidate, baseline, policy):
    try:
        new, old = validate_report(candidate), validate_report(baseline)
        for key in ("dataset_hash", "fixture_hash", "scorer_version", "mode", "split"):
            if candidate[key] != baseline[key]:
                raise ValueError("baseline_incomparable")
        if candidate["mode"] == "live" and (not candidate.get("model") or candidate.get("model") != baseline.get("model")):
            raise ValueError("baseline_model_incomparable")
        if set(new) != set(old) or any(new[k]["category"] != old[k]["category"] or new[k]["critical"] != old[k]["critical"] for k in new):
            raise ValueError("baseline_cases_differ")
        if not baseline.get("source_commit") or not candidate.get("source_commit"):
            raise ValueError("source_identity_missing")
        threshold = policy["minimum_accuracy"]
        tolerance = policy["maximum_accuracy_drop"]
        if type(threshold) not in (int, float) or not 0 <= threshold <= 1 or type(tolerance) not in (int, float) or not 0 <= tolerance <= 1:
            raise ValueError("policy_invalid")
        failures = []
        supported = [k for k in new if new[k]["category"] in ("rules", "paraphrase")]
        if not supported:
            raise ValueError("supported_samples_missing")
        accuracy = sum(new[k]["passed"] for k in supported) / len(supported)
        old_accuracy = sum(old[k]["passed"] for k in supported) / len(supported)
        if accuracy < threshold:
            failures.append("accuracy_below_minimum")
        if old_accuracy - accuracy > tolerance + 1e-12:
            failures.append("accuracy_regression")
        for k, case in new.items():
            if case["category"] in ("permission", "clarification", "unsupported") and not case["passed"]:
                failures.append("boundary_failure:" + k)
            if case["category"] in ("clarification", "unsupported") and case.get("sql_calls") != 0:
                failures.append("boundary_sql_call:" + k)
            if case["critical"] and old[k]["passed"] and not case["passed"]:
                failures.append("critical_regression:" + k)
            if case["category"] == "rules" and case.get("model_calls") != 0:
                failures.append("rules_model_call:" + k)
        # 逐类别比较避免新增简单问题掩盖原有类别的退化。
        for category in ("rules", "paraphrase"):
            keys = [k for k in new if new[k]["category"] == category]
            if keys and (sum(old[k]["passed"] for k in keys) - sum(new[k]["passed"] for k in keys)) / len(keys) > tolerance + 1e-12:
                failures.append("category_regression:" + category)
        return {"passed": not failures, "accuracy": accuracy, "baseline_accuracy": old_accuracy, "supported_samples": len(supported), "failures": failures}
    except (ValueError, KeyError, TypeError) as exc:
        return {"passed": False, "failures": [str(exc) if isinstance(exc, ValueError) else "report_or_policy_invalid"]}


def validate_suite(report):
    """检查完整冻结题单，不能只提交几个成功案例来缩小分母。"""
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from evals.cases import cases
    actual = validate_report(report)
    expected = {c["id"]: c for c in cases() if (report["split"] == "all" or c["split"] == report["split"]) and (report["mode"] == "live" or c["category"] != "paraphrase")}
    if set(actual) != set(expected) or any(actual[k]["category"] != expected[k]["category"] or actual[k]["critical"] != expected[k]["critical"] for k in expected):
        raise ValueError("report_suite_incomplete")
    for field, path in (("dataset_hash", "evals/cases.py"), ("fixture_hash", "evals/fixture.csv"), ("scorer_version", "evals/oracle.py")):
        if report[field] != hashlib.sha256((root / path).read_bytes()).hexdigest():
            raise ValueError("report_suite_version_mismatch")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    args = parser.parse_args()
    try:
        candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        validate_suite(candidate)
        validate_suite(baseline)
        outcome = decide(candidate, baseline, json.loads(args.policy.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        outcome = {"passed": False, "failures": ["report_identity_unavailable"]}
    print(json.dumps(outcome, ensure_ascii=False))
    return 0 if outcome["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
