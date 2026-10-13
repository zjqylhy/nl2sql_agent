#!/usr/bin/env python3
"""发布后以专用验收身份检查 HTTPS、规则/模型问数与 SSE。"""
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.request
from urllib.parse import urlparse


def run(origin, *, allow_private=False):
    parsed = urlparse(origin)
    # 私有部署仅允许固定回环 origin，正式发布仍强制 HTTPS。
    private = allow_private and origin == "http://localhost:8088"
    if not private and (parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("https_origin_required")
    if not all(os.getenv(key) for key in ("SMOKE_TENANT", "SMOKE_USER", "SMOKE_PASSWORD")):
        raise ValueError("explicit_smoke_identity_required")
    client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def call(path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(origin + path, data, {"Origin": origin, "Content-Type": "application/json", **(headers or {})})
        with client.open(request, timeout=15) as response:
            return response.read(2 * 1024 * 1024), response.headers
    call("/healthz")
    call("/api/login", {"tenant": os.environ["SMOKE_TENANT"], "username": os.environ["SMOKE_USER"], "password": os.environ["SMOKE_PASSWORD"]})
    try:
        for parser, question in (("rules", "本月 GMV"), ("llm", os.getenv("SMOKE_MODEL_QUESTION", "本月 GMV，只看圣保罗州"))):
            data, _ = call("/api/tasks", {"question": question})
            task_id = json.loads(data)["id"]
            if not re.fullmatch(r"[a-f0-9]{48}", task_id): raise ValueError("task_identity_invalid")
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                data, _ = call("/api/tasks/" + task_id)
                task = json.loads(data)
                if task["status"] not in ("pending", "querying"): break
                time.sleep(0.5)
            if task["status"] != "completed" or task["result"]["usage"]["parser"] != parser:
                raise ValueError("query_path_failed")
            events, headers = call("/api/tasks/" + task_id + "/events")
            ids = [int(value) for value in re.findall(rb"^id: (\d+)$", events, re.MULTILINE)]
            if not ids or "text/event-stream" not in headers.get("Content-Type", ""):
                raise ValueError("sse_failed")
            resumed, _ = call("/api/tasks/" + task_id + "/events", headers={"Last-Event-ID": str(ids[0])})
            if [int(value) for value in re.findall(rb"^id: (\d+)$", resumed, re.MULTILINE)] != [value for value in ids if value > ids[0]]:
                raise ValueError("sse_resume_failed")
    finally:
        call("/api/logout", {})


if __name__ == "__main__":
    try:
        run(sys.argv[1])
        print("HTTPS, rules, model and SSE smoke passed")
    except Exception:
        # 不打印响应或底层异常，验收凭据由环境注入。
        print("Cloud smoke failed; inspect the acceptance identity and service health", file=sys.stderr)
        raise SystemExit(1)
