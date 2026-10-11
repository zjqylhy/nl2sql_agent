import json
from http.server import ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace

import httpx
from scripts import eval_model_gateway as gateway


def test_gateway_budget_blocks_outbound_and_keeps_provider_key_private(monkeypatch):
    monkeypatch.setenv("EVAL_MODEL_KEY", "fixture-gateway-key")
    monkeypatch.setenv("EVAL_MAX_MODEL_CALLS", "1")
    monkeypatch.setenv("LLM_API_KEY", "fixture-provider-secret")
    monkeypatch.setenv("LLM_BASE_URL", "https://model.example")
    monkeypatch.setattr(gateway, "calls", 0)
    forwarded = []
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size): return b'{"choices":[]}'
    def open_request(request, **options):
        forwarded.append(request)
        return Response()
    monkeypatch.setattr(gateway.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=open_request))
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway.Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with httpx.Client(base_url="http://127.0.0.1:" + str(server.server_port), trust_env=False) as client:
            assert client.post("/chat/completions", json={"model":"fixture"}).status_code == 401
            headers = {"Authorization": "Bearer fixture-gateway-key"}
            accepted = client.post("/chat/completions", headers=headers, json={"model":"fixture"})
            blocked = client.post("/chat/completions", headers=headers, json={"model":"fixture"})
            assert accepted.status_code == 200 and blocked.status_code == 429
            assert "fixture-provider-secret" not in accepted.text + blocked.text
        assert len(forwarded) == 1
        assert forwarded[0].get_header("Authorization") == "Bearer fixture-provider-secret"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
