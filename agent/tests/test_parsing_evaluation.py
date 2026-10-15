"""补充题单的错误直出与未完成分别计数，未配置模型不能冒充真实模型验收。"""
import logging
import json

import httpx
import pytest
from scripts import evaluate_query_parsing as evaluation
from scripts.evaluate_query_parsing import evaluate, load_cases
from runtime import telemetry
from runtime.query_grammar import recognize


def test_independent_cases_preserve_failures_and_check_csv(monkeypatch):
    monkeypatch.setattr(telemetry.logger, "level", logging.WARNING)
    data = load_cases()
    report = evaluate()
    assert len(data["cases"]) == 28 and data["annotation_status"] == "draft"
    assert report["status"] == "complete" and report["scope"] == "python_parser_sqlite_csv"
    assert len(report["records"]) == 28 and report["model_calls"] == 0
    assert report["summary"] == {"cases": 28, "passed": 21, "supported": 20,
        "supported_completed_correctly": 13, "wrong_rule_direct": 0, "supported_clarifications": 7, "model_failures": 0}
    assert all(r["result_correct"] for r in report["records"] if r["task_status"] == "completed")
    assert all(r["sql_calls"] == 0 for r in report["records"] if r["task_status"] != "completed")


def test_live_evaluation_requires_real_configuration(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(ValueError, match="configuration missing"):
        evaluate(live=True, rounds=3)
    with pytest.raises(ValueError, match="budget invalid"):
        evaluate(max_model_calls=121)


def test_original_common_queries_keep_full_plan_and_csv_results(monkeypatch):
    from evals.understanding.dataset import load
    monkeypatch.setattr(telemetry.logger, "level", logging.WARNING)
    data = {"annotation_status": "draft", "cases": [{"case": c.model_dump(), "base": None}
        for c in load().cases if c.category == "common"]}
    monkeypatch.setattr(evaluation, "load_cases", lambda: data)
    report = evaluate()
    assert report["summary"]["cases"] == report["summary"]["passed"] == 30
    assert report["summary"]["wrong_rule_direct"] == report["model_calls"] == 0


def test_live_call_budget_counts_requests_and_keeps_incomplete_result(monkeypatch):
    from runtime.model_transport import ModelTransport
    data = load_cases()
    data["cases"] = [item for item in data["cases"] if item["case"]["category"] == "unfamiliar"][:2]
    monkeypatch.setattr(evaluation, "load_cases", lambda: data)
    for key, value in (("LLM_API_KEY", "fixture-key"), ("LLM_MODEL", "fixture"), ("LLM_BASE_URL", "https://provider.example")):
        monkeypatch.setenv(key, value)
    calls = []
    def respond(transport, client, prompt, question, limit):
        calls.append(question)
        return httpx.Response(200, request=httpx.Request("POST", "https://provider.example/chat/completions"),
            json={"choices": [{"message": {"content": json.dumps(data["cases"][0]["case"]["expected"])}}]})
    monkeypatch.setattr(ModelTransport, "post", respond)
    report = evaluate(live=True, rounds=3, max_model_calls=1)
    assert report["status"] == "incomplete" and report["reason"] == "model_budget"
    assert report["model_calls"] == len(calls) == len(report["records"]) == 1


def test_rule_diagnostics_do_not_expose_question_or_values(monkeypatch):
    messages = []
    monkeypatch.setattr(telemetry.logger, "info", messages.append)
    parsed = recognize("本月 GMV，不包含 sp，私有查询内容")
    telemetry.rule_decision(parsed, "initial")
    assert len(messages) == 1 and "unknown_clause" in messages[0]
    assert '"sp"' not in messages[0] and '"SP"' not in messages[0] and "GMV" not in messages[0]
    assert "私有查询内容" not in messages[0]
    state = next(c for c in parsed.clauses if c.kind == "state")
    assert state.text == "不包含 SP" and state.start == 7
