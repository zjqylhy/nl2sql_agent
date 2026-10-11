import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from evals.cases import cases
from scripts import release
from scripts.package_release import package


def reports(folder, commit="a" * 40):
    base = {"report_version": 1, "status": "complete", "mode": "live", "split": "acceptance", "source_commit": commit,
        "configuration_hash": "c" * 64, "model": "fixture-model", "model_endpoint": "https://model.example", "sse_verified": True, "execution_scope": "release_images",
        "images": {name: "registry.example/" + name + "@sha256:" + "d" * 64 for name in ("api", "agent", "web")},
        **{key: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for key, name in (("dataset_hash", "evals/cases.py"), ("fixture_hash", "evals/fixture.csv"), ("scorer_version", "evals/oracle.py"))},
        "cases": [{"id": c["id"], "category": c["category"], "critical": c["critical"], "passed": True, "model_calls": int(c["category"] == "paraphrase"), "sql_calls": int(c["category"] in ("rules", "paraphrase"))} for c in cases() if c["split"] == "acceptance"]}
    for number in range(1, 4):
        for role in ("baseline", "candidate"):
            value = copy.deepcopy(base)
            value["round"] = number
            (folder / f"{role}-{number}.json").write_text(json.dumps(value), encoding="utf-8")
    return package(folder)


def test_bundle_requires_actual_images_three_rounds_and_content_identity(tmp_path):
    path = reports(tmp_path)
    manifest = release.admit(path)
    tampered = tmp_path / manifest["rounds"][0]["candidate"]
    tampered.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="report_digest_mismatch"):
        release.admit(path)


@pytest.mark.parametrize("change", ["source", "image", "sse", "scope", "boundary", "threshold", "cases"])
def test_no_release_on_wrong_evidence_or_failed_quality(tmp_path, change):
    reports(tmp_path)
    path = tmp_path / "candidate-1.json"
    report = release.read(path)
    if change == "source": report["source_commit"] = "worktree-a"
    if change == "image": report["images"]["api"] = "registry.example/api:latest"
    if change == "sse": report["sse_verified"] = False
    if change == "scope": report["execution_scope"] = "local_images"
    if change == "boundary": next(c for c in report["cases"] if c["category"] == "permission")["passed"] = False
    if change == "threshold":
        for case in report["cases"]:
            if case["category"] == "rules": case["passed"] = False
    if change == "cases": report["cases"].pop()
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError): package(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="Linux atomic state symlinks")
def test_record_failure_preserves_both_successful_versions(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    manifests = []
    for marker in ("a", "b", "c"):
        folder = tmp_path / marker
        folder.mkdir()
        path = reports(folder, marker * 40)
        manifests.append((release.admit(path), folder))
    release.save_success(state, *manifests[0])
    release.save_success(state, *manifests[1])
    replace = release.os.replace
    def fail_pointer(source, target):
        if Path(target).name == "active": raise OSError("fixture disk failure")
        return replace(source, target)
    monkeypatch.setattr(release.os, "replace", fail_pointer)
    with pytest.raises(OSError): release.save_success(state, *manifests[2])
    assert release.admit(state / "current.json")["source_commit"] == "b" * 40
    assert release.admit(state / "previous.json")["source_commit"] == "a" * 40


def test_private_config_rejects_tls_public_ports_or_model_mismatch(tmp_path, monkeypatch):
    pg, my, env = tmp_path / "pg.pem", tmp_path / "my.pem", tmp_path / "cloud.env"
    for file in (pg, my, env):
        file.write_text("fixture")
        file.chmod(0o600)
    api_env = {"PUBLIC_ORIGIN": "https://app.example", "COOKIE_SECURE": "true", "SERVICE_KEY": "s" * 40, "TRUSTED_PROXY_CIDRS": "172.30.0.10/32", "DATABASE_URL": "postgres://a:p@pg/platform?sslmode=verify-full&sslrootcert=/run/secrets/postgres-ca.pem"}
    mount = lambda host, target: {"source": str(host), "target": target, "read_only": True}
    config = {"services": {
        "api": {"environment": api_env, "volumes": [mount(pg, "/run/secrets/postgres-ca.pem")]},
        "migrate": {"environment": {"DATABASE_URL": api_env["DATABASE_URL"]}, "volumes": [mount(pg, "/run/secrets/postgres-ca.pem")]},
        "agent": {"environment": {"TENANT_DATASOURCES": json.dumps({"alpha:admin": {"ssl_ca": "/run/secrets/mysql-ca.pem"}}), "LLM_BASE_URL": "https://model.example", "LLM_MODEL": "fixture-model", "LLM_API_KEY": "fixture"}, "volumes": [mount(my, "/run/secrets/mysql-ca.pem")]},
        "web": {"ports": [{"host_ip": "127.0.0.1"}]}}}
    from types import SimpleNamespace
    monkeypatch.setattr(release.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(config)))
    manifest = {"model": "fixture-model", "model_endpoint": "https://model.example", "images": {s: "registry.example/" + s + "@sha256:" + "d"*64 for s in ("api", "agent", "web")}}
    for service, value in config["services"].items(): value["image"] = manifest["images"]["api" if service == "migrate" else service]
    assert release.preflight(env, tmp_path / "override", manifest) == "https://app.example"
    config["services"]["web"]["ports"][0]["host_ip"] = "0.0.0.0"
    with pytest.raises(ValueError): release.preflight(env, tmp_path / "override", manifest)
    config["services"]["web"]["ports"][0]["host_ip"] = "127.0.0.1"
    api_env["DATABASE_URL"] = api_env["DATABASE_URL"].replace("verify-full", "disable")
    with pytest.raises(ValueError): release.preflight(env, tmp_path / "override", manifest)
    api_env["DATABASE_URL"] = api_env["DATABASE_URL"].replace("disable", "verify-full")
    config["services"]["agent"]["environment"]["LLM_MODEL"] = "unevaluated-model"
    with pytest.raises(ValueError): release.preflight(env, tmp_path / "override", manifest)
    config["services"]["agent"]["environment"]["LLM_MODEL"] = "fixture-model"
    config["services"]["migrate"]["image"] = "registry.example/api:wrong-migration"
    with pytest.raises(ValueError): release.preflight(env, tmp_path / "override", manifest)


@pytest.mark.skipif(os.name == "nt", reason="Linux Bash/permissions release harness")
@pytest.mark.parametrize("failure", ["migration", "readiness", "smoke", "lock", "none", "rollback"])
def test_deployment_failure_state_and_command_order(tmp_path, failure):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    manifest = reports(bundle)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    env_file = tmp_path / "cloud.env"
    env_file.write_text("fixture")
    env_file.chmod(0o600)
    pg, my = tmp_path / "pg.pem", tmp_path / "my.pem"
    pg.write_text("fixture")
    my.write_text("fixture")
    ds = "postgres://a:p@pg/platform?sslmode=verify-full&sslrootcert=/run/secrets/postgres-ca.pem"
    volumes = lambda p, target: [{"source": str(p), "target": target, "read_only": True}]
    config = {"services": {
        "api": {"environment": {"DATABASE_URL": ds, "PUBLIC_ORIGIN": "https://app.example", "COOKIE_SECURE": "true", "SERVICE_KEY": "s"*40, "TRUSTED_PROXY_CIDRS": "172.30.0.10/32"}, "volumes": volumes(pg, "/run/secrets/postgres-ca.pem")},
        "migrate": {"environment": {"DATABASE_URL": ds}, "volumes": volumes(pg, "/run/secrets/postgres-ca.pem")},
        "agent": {"environment": {"TENANT_DATASOURCES": json.dumps({"alpha:admin": {"ssl_ca": "/run/secrets/mysql-ca.pem"}}), "LLM_API_KEY": "fixture", "LLM_BASE_URL": "https://model.example", "LLM_MODEL": "fixture-model"}, "volumes": volumes(my, "/run/secrets/mysql-ca.pem")},
        "web": {"ports": [{"host_ip": "127.0.0.1"}]}}}
    config_path = tmp_path / "config.json"
    for service, value in config["services"].items(): value["image"] = "registry.example/" + ("api" if service == "migrate" else service) + "@sha256:" + "d"*64
    config_path.write_text(json.dumps(config))
    record = tmp_path / "record.json"
    record.write_text(json.dumps({"backup_reference": "fixture-backup", "target_commit": "a"*40, "previous_commit": None, "old_app_compatible": True}))
    binary = tmp_path / "bin"
    binary.mkdir()
    docker = binary / "docker"
    docker.write_text("#!/usr/bin/env python3\nimport os,sys,pathlib\nargs=sys.argv[1:]\nif 'config' in args: print(pathlib.Path(os.environ['CONFIG_PATH']).read_text()); sys.exit(0)\nstep=next(v for v in ('pull','run','up') if v in args)\nwith open(os.environ['COMMAND_LOG'],'a') as f: f.write(step+'\\n')\nsys.exit(1 if os.environ.get('FAIL_STEP')==step else 0)\n")
    docker.chmod(0o700)
    smoke = binary / "smoke"
    smoke.write_text("#!/usr/bin/env bash\n[[ ${FAIL_STEP:-} != smoke ]]\n")
    smoke.chmod(0o700)
    curl = binary / "curl"
    curl.write_text("#!/usr/bin/env bash\nexit 0\n")
    curl.chmod(0o700)
    log = tmp_path / "commands.log"
    environment = {**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"], "CONFIG_PATH": str(config_path), "COMMAND_LOG": str(log), "FAIL_STEP": {"migration":"run", "readiness":"up"}.get(failure, failure)}
    lock = None
    if failure == "lock":
        import fcntl
        lock = (state / "deploy.lock").open("w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = subprocess.run(["bash", str(ROOT / "scripts/deploy_cloud.sh"), "deploy", str(env_file), str(manifest), str(state), str(record), str(smoke)], env=environment, capture_output=True, text=True)
    finally:
        if lock: lock.close()
    if failure in ("none", "rollback"):
        assert result.returncode == 0, result.stderr
        assert release.admit(state / "current.json")["source_commit"] == "a" * 40
        assert log.read_text().splitlines() == ["pull", "run", "up"]
        if failure == "rollback":
            upgraded = tmp_path / "upgraded"
            upgraded.mkdir()
            new_manifest = reports(upgraded, "b" * 40)
            record.write_text(json.dumps({"backup_reference": "fixture-backup", "target_commit": "b"*40, "previous_commit": "a"*40, "old_app_compatible": True}))
            command = ["bash", str(ROOT / "scripts/deploy_cloud.sh"), "deploy", str(env_file), str(new_manifest), str(state), str(record), str(smoke)]
            update = subprocess.run(command, env=environment, capture_output=True, text=True)
            assert update.returncode == 0, update.stderr
            record.write_text(json.dumps({"current_commit": "b"*40, "target_commit": "a"*40, "schema_compatible": True}))
            log.write_text("")
            command[2], command[4] = "rollback", str(state / "previous.json")
            reverted = subprocess.run(command, env=environment, capture_output=True, text=True)
            assert reverted.returncode == 0, reverted.stderr
            assert log.read_text().splitlines() == ["pull", "up"]
            assert release.admit(state / "current.json")["source_commit"] == "a"*40
            assert release.read(state / "previous.json")["source_commit"] == "b"*40
    else:
        assert result.returncode != 0
        assert not (state / "current.json").exists()
        if failure == "migration": assert log.read_text().splitlines() == ["pull", "run"]
        if failure == "lock": assert not log.exists()
