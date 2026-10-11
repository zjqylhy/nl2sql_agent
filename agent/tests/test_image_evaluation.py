import json
from types import SimpleNamespace

from scripts import evaluate_images


def test_local_override_with_digest_still_cannot_claim_release_execution(tmp_path, monkeypatch):
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps({"source_commit":"a"*40,"images":{s:"registry.example/"+s+"@sha256:"+"b"*64 for s in ("api","agent","web")}}))
    stack = SimpleNamespace(start=lambda rows: None, verify_images=lambda: None, url="http://fixture", sessions=[None, None, None], observe=lambda value: {}, runtime_hash=lambda: "c"*64, verify_sse=lambda: True, close=lambda: None)
    monkeypatch.setattr(evaluate_images, "Stack", lambda *args: stack)
    monkeypatch.setattr(evaluate_images.httpx, "get", lambda *args, **kwargs: SimpleNamespace(status_code=200))
    monkeypatch.setattr(evaluate_images, "execute_case", lambda *args, **kwargs: (True, {"sql_calls":0,"model_calls":0}))
    args = SimpleNamespace(identity=identity, output=tmp_path/"report.json", local_images=True, mode="deterministic", max_model_calls=45)
    evaluate_images.run(args)
    report = json.loads(args.output.read_text())
    assert report["execution_scope"] == "local_images"
    assert report["source_commit"].startswith("worktree-")
