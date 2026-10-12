import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from evals.understanding.dataset import DATASET, load
from evals.understanding.report import digest, summarize, validate
from scripts.report_understanding import markdown


def study():
    data = load()
    selected = [c for c in data.cases if c.split == "development"]
    identity = {"source_commit": "a" * 40, "dataset_hash": digest(DATASET), "annotation_status": "draft", "reviewer": None,
                "fixture_hash": digest(ROOT / "evals/fixture.csv"), "fixture_server_hash": "b" * 64, "fixture_kind": "synthetic_order_facts",
                "scorer_hash": "s", "runtime_hash": "r", "executor_hash": "e", "model_configuration_hash": "m",
                "analysis_date": data.analysis_date, "split": "development", "case_ids": [c.id for c in selected]}
    cases = []
    for c in selected:
        supported = c.category in ("common", "unfamiliar")
        cases.append({"id": c.id, "category": c.category, "passed": True, "intent_correct": True if supported else None,
                      "result_correct": True if supported else None, "boundary_correct": None if supported else True,
                      "conditions_complete": True if supported else None, "model_calls": 0, "sql_calls": int(supported), "attempts": 1,
                      "known_input_tokens": 0, "known_output_tokens": 0, "known_total_tokens": 0, "unknown_usage_calls": 0,
                      "duration_ms": 100, "parser": "rules", "differences": [], "task_status": "completed", "error_code": ""})
    pair = {mode: {"status": "complete", "mode": mode, "round": 1, "identity": copy.deepcopy(identity), "cases": copy.deepcopy(cases), "issued_calls": 0} for mode in ("rules_only", "full")}
    return {"report_version": 1, "status": "complete", "mode": "paired_live", "identity": identity, "created_at": "2026-10-12", "rounds": [pair]}


def test_fixed_denominator_rescue_actual_calls_and_unknown_usage():
    value = study()
    old = next(c for c in value["rounds"][0]["rules_only"]["cases"] if c["category"] == "unfamiliar")
    new = next(c for c in value["rounds"][0]["full"]["cases"] if c["id"] == old["id"])
    old.update(passed=False, intent_correct=None, result_correct=None)
    new.update(model_calls=2, parser="llm", attempts=2, unknown_usage_calls=1, known_total_tokens=12)
    value["rounds"][0]["full"]["issued_calls"] = 2
    summary = summarize(value)["rounds"][0]
    assert summary["rescued_ids"] == [old["id"]]
    assert summary["full"]["supported"]["total"] == summary["rules_only"]["supported"]["total"]
    assert summary["full"]["known_total_tokens"] == 12
    assert summary["full"]["unknown_usage_calls"] == 1 and summary["full"]["retries"] == 1
    new.update(model_calls=0, unknown_usage_calls=0)
    value["rounds"][0]["full"]["issued_calls"] = 0
    assert not summarize(value)["rounds"][0]["rescued_ids"]


@pytest.mark.parametrize("change", ["missing", "duplicate", "identity", "version", "incomplete", "budget", "round", "selection", "unknown_usage", "false_answer"])
def test_invalid_or_incomplete_reports_cannot_generate_evidence(change):
    value = study()
    report = value["rounds"][0]["full"]
    if change == "missing": report["cases"].pop()
    if change == "duplicate": report["cases"][-1] = copy.deepcopy(report["cases"][0])
    if change == "identity": report["identity"]["runtime_hash"] = "different"
    if change == "version": value["report_version"] = 2
    if change == "incomplete": value["status"] = "incomplete"
    if change == "budget": report["issued_calls"] = 121
    if change == "round": report["round"] = 3
    if change == "selection": value["identity"]["case_ids"].pop()
    if change == "unknown_usage": report["cases"][0]["unknown_usage_calls"] = 1
    if change == "false_answer": report["cases"][0]["intent_correct"] = False
    with pytest.raises(ValueError):
        validate(value)


def test_flaky_results_and_no_human_review_acceptance_claim():
    value = study()
    for index in (2, 3):
        pair = copy.deepcopy(value["rounds"][0])
        for report in pair.values():
            report["round"] = index
        value["rounds"].append(pair)
    case = value["rounds"][1]["full"]["cases"][0]
    case.update(passed=False, intent_correct=False)
    summary = summarize(value)
    assert summary["stability"]["flaky_ids"] == [case["id"]]
    assert not summary["live_acceptance_complete"] and not summary["human_reviewed"]
    text = markdown(value, "r" * 64)
    assert "待人工复核" in text and "失败分布" in text
    for c in load().cases:
        if c.split == "acceptance":
            assert c.question not in text
