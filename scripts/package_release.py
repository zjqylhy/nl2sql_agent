"""将三轮实际镜像评测封装为内容摘要绑定的发布包。"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.release import admit, atomic_json, read


def package(folder):
    candidate = read(folder / "candidate-1.json")
    manifest = {"manifest_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        **{k: candidate[k] for k in ("source_commit", "images", "configuration_hash", "model", "model_endpoint")}, "rounds": [], "reports": {}}
    for number in range(1, 4):
        pair = {}
        for role in ("candidate", "baseline"):
            content = (folder / f"{role}-{number}.json").read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            name = role + "-" + digest + ".json"
            (folder / name).write_bytes(content)
            manifest["reports"][name] = digest
            pair[role] = name
        manifest["rounds"].append(pair)
    path = folder / "release-manifest.json"
    atomic_json(path, manifest)
    # 打包与验收共用一条门禁，避免两种准入标准。
    admit(path)
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    try:
        package(args.folder)
    except Exception:
        (args.folder / "release-manifest.json").unlink(missing_ok=True)
        print("Release bundle rejected", file=sys.stderr)
        raise SystemExit(1)
