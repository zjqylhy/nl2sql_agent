"""按完整意图组切分，标注仍须人工复核后才能作为正式验收准确率。"""
import json
from pathlib import Path

ROOT = Path(__file__).parent


def load():
    data = json.loads((ROOT / "cases.json").read_text(encoding="utf-8"))
    groups = {}
    ids = set()
    for case in data["cases"]:
        if case["id"] in ids:
            raise ValueError("duplicate_case")
        ids.add(case["id"])
        groups.setdefault(case["group"], set()).add(case["split"])
        if case["split"] not in {"development", "acceptance"} or not case["question"]:
            raise ValueError("invalid_case")
    if any(len(splits) != 1 for splits in groups.values()):
        raise ValueError("intent_group_leakage")
    if data["annotation_status"] not in {"draft", "human_reviewed"}:
        raise ValueError("invalid_annotation_status")
    if data["annotation_status"] == "human_reviewed" and not data.get("reviewer"):
        raise ValueError("reviewer_required")
    return data
