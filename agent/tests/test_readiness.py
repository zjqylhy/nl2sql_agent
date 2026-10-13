from concurrent.futures import Future
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from runtime import app, readiness


def source(**values):
    return app.Datasource(host="private-db", database="fixture", user="ro", password="secret", analysis_date="2018-08-31", ssl_ca="/private/ca.pem", **values)


def test_probe_verifies_tls_view_and_closes(monkeypatch):
    calls = {}
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql): calls["sql"] = sql
    class Connection:
        def cursor(self): return Cursor()
        def close(self): calls["closed"] = True
    def connect(**options):
        calls["options"] = options
        return Connection()
    monkeypatch.setattr(readiness.pymysql, "connect", connect)
    assert readiness.probe(source())
    assert calls["options"]["ssl_verify_cert"] and calls["options"]["ssl_verify_identity"]
    assert calls["options"]["read_timeout"] == 1
    assert calls["sql"].endswith("LIMIT 0") and calls["closed"]


@pytest.mark.parametrize("failure", ["network", "certificate", "missing_view"])
def test_probe_failure_is_private_and_closes(monkeypatch, failure):
    closed = []
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql): raise RuntimeError("missing_view contains password=secret")
    class Connection:
        def cursor(self): return Cursor()
        def close(self): closed.append(True)
    def connect(**options):
        if failure != "missing_view": raise RuntimeError(failure + " secret")
        return Connection()
    monkeypatch.setattr(readiness.pymysql, "connect", connect)
    assert not readiness.probe(source())
    assert bool(closed) == (failure == "missing_view")


def test_checks_deduplicate_and_failure_never_calls_model(monkeypatch):
    calls = []
    monkeypatch.setattr(readiness, "pending", set())
    monkeypatch.setattr(readiness, "probe", lambda value: calls.append(value) or True)
    assert readiness.check([source(), source()])
    assert len(calls) == 1
    monkeypatch.setattr(readiness, "probe", lambda value: False)
    assert not readiness.check([source()])


def test_timeout_does_not_accumulate_jobs(monkeypatch):
    outstanding = Future()
    outstanding.set_running_or_notify_cancel()
    calls = []
    monkeypatch.setattr(readiness, "pending", set())
    monkeypatch.setattr(readiness, "BUDGET", 0.01)
    monkeypatch.setattr(readiness, "pool", SimpleNamespace(submit=lambda *args: calls.append(args) or outstanding))
    assert not readiness.check([source()])
    assert not readiness.check([source()])
    assert len(calls) == 1
    outstanding.set_result(False)


def test_ready_endpoint_is_sanitized(monkeypatch):
    monkeypatch.setenv("SERVICE_KEY", "s" * 40)
    monkeypatch.setenv("TENANT_DATASOURCES", json.dumps({"alpha:admin": source().model_dump(mode="json")}))
    app.sources.cache_clear()
    try:
        with TestClient(app.app) as client:
            monkeypatch.setattr(readiness, "check", lambda values: False)
            response = client.get("/readyz")
            assert response.status_code == 503 and "secret" not in response.text and "private-db" not in response.text
            assert client.get("/healthz").status_code == 200
            monkeypatch.setattr(readiness, "check", lambda values: True)
            assert client.get("/readyz").status_code == 200
    finally:
        app.sources.cache_clear()
