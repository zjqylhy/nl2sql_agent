import copy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.quality_gate import decide, release_identity, validate_suite
from evals.cases import cases
from evals.oracle import facts

POLICY = {"minimum_accuracy": .85, "maximum_accuracy_drop": .02}


def report():
    return {"report_version": 1, "status": "complete", "dataset_hash": "d", "fixture_hash": "f", "scorer_version": "s", "mode": "deterministic", "split": "development", "source_commit": "a" * 40,
            "cases": [{"id": str(i), "category": "rules", "passed": True, "critical": False, "model_calls": 0} for i in range(100)] + [{"id": "p", "category": "permission", "passed": True, "critical": True}]}


def test_absolute_and_relative_gates_and_fixed_denominator():
    old, new = report(), report()
    assert decide(new, old, POLICY)["passed"]
    for case in new["cases"][:3]: case["passed"] = False
    result = decide(new, old, POLICY)
    assert not result["passed"] and result["accuracy"] == .97
    assert "accuracy_regression" in result["failures"]
    new["cases"].pop()
    assert not decide(new, old, POLICY)["passed"]


def test_boundaries_critical_calls_and_incomplete_reports():
    old = report()
    for change in ("boundary", "critical", "calls", "incomplete", "identity"):
        new = copy.deepcopy(old)
        if change == "boundary": new["cases"][-1]["passed"] = False
        if change == "critical":
            old["cases"][0]["critical"] = new["cases"][0]["critical"] = True
            new["cases"][0]["passed"] = False
        if change == "calls": new["cases"][0]["model_calls"] = 1
        if change == "incomplete": new["status"] = "incomplete"
        if change == "identity": new["fixture_hash"] = "changed"
        assert not decide(new, old, POLICY)["passed"]


def test_release_cannot_use_offline_or_different_images():
    import pytest
    new = report()
    with pytest.raises(ValueError): release_identity(new, {})
    new.update(mode="live", split="acceptance", configuration_hash="c", images={k: "registry.test/" + k + "@sha256:" + "a" * 64 for k in ("api", "agent", "web")})
    manifest = {k: new[k] for k in ("source_commit", "configuration_hash", "images")}
    release_identity(new, manifest)
    manifest["images"] = {**manifest["images"], "api": "registry.test/api:latest"}
    with pytest.raises(ValueError): release_identity(new, manifest)


def test_dataset_partition_does_not_split_paraphrase_groups():
    suite = cases()
    assert len(suite) == 120
    assert {s: sum(c["split"] == s for c in suite) for s in ("development", "acceptance")} == {"development": 60, "acceptance": 60}
    for group in {c["group"] for c in suite}:
        assert len({c["split"] for c in suite if c["group"] == group}) == 1


def test_missing_suite_and_diagnostic_are_not_release_evidence():
    import pytest
    with pytest.raises(ValueError): validate_suite(report())
    diagnostic = report()
    diagnostic["split"] = "diagnostic"
    assert not decide(diagnostic, diagnostic, POLICY)["passed"]


def test_live_comparison_requires_same_model():
    old, new = report(), report()
    old.update(mode="live", model="old-model")
    new.update(mode="live", model="new-model")
    assert not decide(new, old, POLICY)["passed"]


def test_oracle_independent_fixture_values():
    # 手工核对原始八月有效订单，避免测试仅重复 SQL 实现。
    from evals.oracle import calculate
    rows = facts(ROOT / "evals/fixture.csv")
    assert any(r["amount"] is None for r in rows)
    august = [r for r in rows if "2018-08-01" <= r["purchased_at"] < "2018-09-01"]
    values = calculate(august)
    assert values["gmv"] == (330, 5)
    assert values["orders"] == (5, 5)
    assert values["aov"] == (66, 5)
    assert values["review_score"][1] == 3
    july = [r for r in rows if "2018-07-01" <= r["purchased_at"] < "2018-08-01"]
    assert calculate(july)["gmv"][0] is None
