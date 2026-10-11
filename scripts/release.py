"""发布清单与运行配置验证；仅输出可公开诊断，完整配置留在内存中。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.quality_gate import decide, release_identity, validate_suite

IMAGE = re.compile(r"[^\s]+@sha256:[a-f0-9]{64}")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def atomic_json(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".release-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_success(state, manifest, bundle):
    """Linux 上以单次指针替换提交 current/previous，避免半写入状态。"""
    current = state / "current.json"
    old = read(current) if current.exists() else None
    previous = old if old != manifest else (read(state / "previous.json") if (state / "previous.json").exists() else None)
    for name in ("current.json", "previous.json"):
        path = state / name
        if path.is_symlink():
            if os.readlink(path) != "active/" + name:
                raise ValueError("state_pointer_invalid")
        elif path.exists():
            raise ValueError("unsupported_state_layout")
    for name, digest in manifest["reports"].items():
        target = state / name
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError("saved_report_conflict")
        if not target.exists():
            content = (bundle / name).read_bytes()
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
    snapshot = Path(tempfile.mkdtemp(dir=state, prefix=".state-"))
    atomic_json(snapshot / "current.json", manifest)
    if previous is not None:
        atomic_json(snapshot / "previous.json", previous)
    for name in ("current.json", "previous.json"):
        path = state / name
        if not path.is_symlink():
            os.symlink("active/" + name, path)
    pointer = state / (snapshot.name + "-pointer")
    os.symlink(snapshot.name, pointer)
    try:
        os.replace(pointer, state / "active")
    finally:
        pointer.unlink(missing_ok=True)


def admit(path):
    manifest = read(path)
    if manifest.get("manifest_version") != 1 or not re.fullmatch(r"[a-f0-9]{40}", manifest.get("source_commit", "")):
        raise ValueError("manifest_identity_invalid")
    images = manifest.get("images")
    if not isinstance(images, dict) or set(images) != {"api", "agent", "web"} or any(not isinstance(v, str) or not IMAGE.fullmatch(v) for v in images.values()):
        raise ValueError("manifest_requires_digests")
    if not re.fullmatch(r"[a-f0-9]{64}", manifest.get("configuration_hash", "")):
        raise ValueError("configuration_identity_missing")
    rounds, hashes = manifest.get("rounds"), manifest.get("reports")
    if not isinstance(rounds, list) or len(rounds) != 3 or not isinstance(hashes, dict):
        raise ValueError("three_live_rounds_required")
    seen, baseline_identity = set(), None
    for pair in rounds:
        reports = []
        for role in ("candidate", "baseline"):
            name = pair.get(role)
            if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+\.json", name) or name in seen:
                raise ValueError("report_path_invalid")
            seen.add(name)
            report_path = Path(path).parent / name
            if hashlib.sha256(report_path.read_bytes()).hexdigest() != hashes.get(name):
                raise ValueError("report_digest_mismatch")
            report = read(report_path)
            validate_suite(report)
            if report.get("execution_scope") != "release_images" or not report.get("sse_verified"):
                raise ValueError("actual_images_required")
            reports.append(report)
        candidate, baseline = reports
        release_identity(candidate, manifest)
        identity = (baseline.get("source_commit"), baseline.get("configuration_hash"), json.dumps(baseline.get("images"), sort_keys=True))
        release_identity(baseline, {k: baseline.get(k) for k in ("source_commit", "configuration_hash", "images")})
        if baseline_identity is not None and baseline_identity != identity:
            raise ValueError("baseline_changed_between_rounds")
        baseline_identity = identity
        # 使用部署代码中的冻结策略，不接受发布清单自带的更低阈值。
        if not decide(candidate, baseline, read(ROOT / "evals/policy.json"))["passed"]:
            raise ValueError("quality_gate_failed")
    if set(hashes) != seen:
        raise ValueError("report_bundle_invalid")
    return manifest


def preflight(env_file, override, manifest):
    if os.name != "nt" and (Path(env_file).stat().st_mode & 0o077):
        raise ValueError("private_environment_file_required")
    result = subprocess.run(["docker", "compose", "--profile", "maintenance", "--env-file", str(env_file), "-f", str(ROOT / "deploy/compose.cloud.yaml"), "-f", str(override), "config", "--format", "json"], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError("compose_configuration_invalid")
    config = json.loads(result.stdout)
    services = config["services"]
    for service in ("api", "agent", "web", "migrate"):
        expected = manifest["images"]["api" if service == "migrate" else service]
        if services[service].get("image") != expected:
            raise ValueError("effective_image_identity_mismatch")
    env = services["api"]["environment"]
    origin = env["PUBLIC_ORIGIN"]
    parsed = urlparse(origin)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("https_origin_required")
    if len(env["SERVICE_KEY"]) < 32 or "replace" in env["SERVICE_KEY"].lower() or str(env["COOKIE_SECURE"]).lower() != "true":
        raise ValueError("production_credentials_required")
    import ipaddress
    proxies = [ipaddress.ip_network(v.strip()) for v in env["TRUSTED_PROXY_CIDRS"].split(",")]
    if not proxies or any(p.prefixlen != p.max_prefixlen for p in proxies):
        raise ValueError("exact_proxy_addresses_required")
    for service in ("api", "migrate"):
        url = urlparse(services[service]["environment"]["DATABASE_URL"])
        query = parse_qs(url.query)
        if url.scheme not in ("postgres", "postgresql") or not url.hostname or query.get("sslmode") != ["verify-full"] or query.get("sslrootcert") != ["/run/secrets/postgres-ca.pem"]:
            raise ValueError("postgres_tls_required")
    for service in ("api", "migrate", "agent"):
        expected = "/run/secrets/mysql-ca.pem" if service == "agent" else "/run/secrets/postgres-ca.pem"
        mounts = [v for v in services[service].get("volumes", []) if v.get("target") == expected]
        if len(mounts) != 1 or not mounts[0].get("read_only") or not Path(mounts[0]["source"]).is_file():
            raise ValueError("database_ca_unavailable")
    sources = json.loads(services["agent"]["environment"]["TENANT_DATASOURCES"])
    if not sources or any(v.get("ssl_ca") != "/run/secrets/mysql-ca.pem" for v in sources.values()):
        raise ValueError("mysql_tls_required")
    agent_env = services["agent"]["environment"]
    if not agent_env.get("LLM_API_KEY") or agent_env.get("LLM_BASE_URL") != manifest.get("model_endpoint") or agent_env.get("LLM_MODEL") != manifest.get("model"):
        raise ValueError("evaluated_model_configuration_required")
    if any(services[s].get("ports") for s in ("api", "agent", "migrate")):
        raise ValueError("private_services_must_not_publish_ports")
    ports = services["web"].get("ports", [])
    if len(ports) != 1 or ports[0].get("host_ip") != "127.0.0.1":
        raise ValueError("loopback_entry_required")
    return origin


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "record", "rollback-check"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--override", type=Path)
    parser.add_argument("--record", type=Path, help="运维备份/数据库兼容性确认记录")
    args = parser.parse_args()
    try:
        manifest = admit(args.manifest)
        record = read(args.record) if args.record else {}
        if args.command == "prepare":
            current = read(args.state / "current.json") if (args.state / "current.json").exists() else None
            if not record.get("backup_reference") or record.get("target_commit") != manifest["source_commit"] or record.get("previous_commit") != (current or {}).get("source_commit") or record.get("old_app_compatible") is not True:
                raise ValueError("backup_and_compatibility_record_required")
            atomic_json(args.override, {"services": {**{s: {"image": image} for s, image in manifest["images"].items()}, "migrate": {"image": manifest["images"]["api"]}}})
            print(preflight(args.env_file, args.override, manifest))
        elif args.command == "rollback-check":
            current = read(args.state / "current.json")
            previous = read(args.state / "previous.json") if (args.state / "previous.json").exists() else None
            if manifest not in (current, previous) or record.get("current_commit") != current["source_commit"] or record.get("target_commit") != manifest["source_commit"] or record.get("schema_compatible") is not True:
                raise ValueError("rollback_compatibility_record_required")
            atomic_json(args.override, {"services": {**{s: {"image": image} for s, image in manifest["images"].items()}, "migrate": {"image": manifest["images"]["api"]}}})
            print(preflight(args.env_file, args.override, manifest))
        else:
            save_success(args.state, manifest, args.manifest.parent)
        return 0
    except Exception:
        # 不输出底层异常；配置解析错误可能携带原始密钥行。
        print("Release validation failed; inspect the documented prerequisites", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
