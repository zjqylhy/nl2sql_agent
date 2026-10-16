"""在部署服务器执行真实 HTTP/模型验收；凭据仅从环境读取，报告留在服务器。"""
import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import re
import time
import urllib.request
from uuid import uuid4


def run(base, origin, *, analysis=False):
    client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(path, body=None, headers=None):
        request = urllib.request.Request(base + path,
            json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
            {"Origin": origin, "Content-Type": "application/json", **(headers or {})})
        with client.open(request, timeout=25) as response:
            raw = response.read(2 * 1024 * 1024)
            return raw if "text/event-stream" in response.headers.get("Content-Type", "") else json.loads(raw)

    def wait(path):
        deadline = time.monotonic() + 210
        while time.monotonic() < deadline:
            value = call(path)
            if value["status"] not in {"pending", "querying"}:
                return value
            time.sleep(0.5)
        raise RuntimeError("server_task_wait_timeout")

    def events(run_id):
        path = "/api/tasks/" + run_id + "/events"
        data = call(path)
        ids = [int(value) for value in re.findall(rb"^id: (\d+)$", data, re.MULTILINE)]
        assert ids and b"event: done" in data, "SSE missing terminal event"
        resumed = call(path, headers={"Last-Event-ID": str(ids[0])})
        assert [int(value) for value in re.findall(rb"^id: (\d+)$", resumed, re.MULTILINE)] == [i for i in ids if i > ids[0]]

    assert call("/healthz")["runtime"] == "python-langgraph"
    call("/api/login", {"tenant": os.environ.get("SMOKE_TENANT", "alpha"),
        "username": os.environ.get("SMOKE_USER", "admin"), "password": os.environ["SMOKE_PASSWORD"]})
    report = {"runtime": "python-langgraph", "queries": [], "analysis": [], "cost_notice": "TEST_UNIT is a user-approved test budget unit, not actual money"}
    try:
        assert call("/api/me")["role"] == "admin"
        for parser, question in [("rules", "2018年7月 GMV 按州统计"), ("llm", "2018年7月的销售额是多少，仅看圣保罗州")]:
            task = call("/api/tasks", {"question": question})
            task = wait("/api/tasks/" + task["id"])
            report["queries"].append(task)
            assert task["status"] == "completed", "single query failed"
            assert task["result"]["usage"]["parser"] == parser
            events(task["id"])
        if analysis:
            assert call("/api/analysis-capabilities")["enabled"]
            for question in [
                "统计2018年7月各州GMV排名前3的州，计算这3个州的GMV合计，同时核查订单总量、加权客单价及占全国GMV比例。",
                "比较2018年6月与2018年7月各州的GMV，以6月为基期、7月为当期，按下降金额找出下降最大的三个州，核查订单量和客单价变化，并给出数据依据。",
            ]:
                body = {"question": question, "request_id": str(uuid4())}
                accepted = call("/api/analysis-runs", body)
                assert call("/api/analysis-runs", body)["id"] == accepted["id"]
                value = wait("/api/analysis-runs/" + accepted["id"])
                report["analysis"].append(value)
                assert value["status"] == "completed", "analysis failed or requires clarification"
                assert any(a["kind"] == "report" for a in value["artifacts"])
                assert value["usage"]["unknown_calls"] == 0
                events(value["id"])
        report["operations"] = call("/api/operations/summary?days=1")
        report["passed"] = True
        return report
    finally:
        # 每次运行均释放验收会话，不把 token 写入报告。
        call("/api/logout", {})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://localhost:8088")
    parser.add_argument("--origin", default="http://localhost:8088")
    parser.add_argument("--analysis", action="store_true")
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    # 验收默认只访问服务器回环入口，防止误把凭据发送到其他站点。
    from urllib.parse import urlsplit
    assert urlsplit(args.base).hostname in {"localhost", "127.0.0.1"}
    result = run(args.base, args.origin, analysis=args.analysis)
    descriptor = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"passed": True, "queries": len(result["queries"]), "analysis": len(result["analysis"]), "report": str(args.report)}))
