import json
from types import SimpleNamespace

import pytest
from scripts import cloud_smoke


@pytest.mark.parametrize("failed_parser", [False, True])
def test_smoke_requires_both_paths_and_resumes_sse(monkeypatch, failed_parser):
    for name in ("SMOKE_TENANT", "SMOKE_USER", "SMOKE_PASSWORD"):
        monkeypatch.setenv(name, "fixture")
    calls, number = [], [0]
    class Response:
        def __init__(self, data, kind="application/json"):
            self.data, self.headers = data, {"Content-Type": kind}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size): return self.data
    def open_request(request, **options):
        path = request.full_url.removeprefix("https://app.example")
        calls.append(path)
        if path == "/api/tasks":
            number[0] += 1
            data = {"id": "a" * 48}
        elif path.endswith("/events"):
            ids = [2] if request.get_header("Last-event-id") else [1, 2]
            return Response("".join(f"id: {i}\nevent: completed\ndata: {{}}\n\n" for i in ids).encode(), "text/event-stream")
        elif path.startswith("/api/tasks/"):
            data = {"status": "completed", "result": {"usage": {"parser": "rules" if number[0] == 1 or failed_parser else "llm"}}}
        else: data = {}
        return Response(json.dumps(data).encode())
    monkeypatch.setattr(cloud_smoke.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=open_request))
    if failed_parser:
        with pytest.raises(ValueError, match="query_path_failed"): cloud_smoke.run("https://app.example")
    else:
        cloud_smoke.run("https://app.example")
        assert calls.count("/api/tasks/" + "a"*48 + "/events") == 4
    assert calls[-1] == "/api/logout"
