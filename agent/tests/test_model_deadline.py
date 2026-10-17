"""响应分段超时之外必须有整体截止时间，避免 Agent 一次调用无限延长。"""
import asyncio
import ssl
import time

import httpx
import pytest
from agent.runtime.model_transport import DeadlineClient


def test_model_total_deadline_and_response_contract(monkeypatch):
    original = httpx.AsyncClient
    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(slow), **kw))
    started = time.monotonic()
    with pytest.raises(httpx.ReadTimeout):
        DeadlineClient(ssl.create_default_context(), timeout=0.05).post("https://fixture.invalid")
    assert time.monotonic()-started < 0.8
    async def response(request):
        return httpx.Response(200, json={"choices": []})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(response), **kw))
    with DeadlineClient(ssl.create_default_context()) as client:
        result = client.post("https://fixture.invalid", json={"model": "fixture"})
    assert result.json() == {"choices": []}
