"""计算实际导入 CSV 的版本摘要；摘要需在只读数据发布时写入可信数据源配置。"""
import argparse
import hashlib
import json
from pathlib import Path

from .import_olist import TABLES


def revision(folder: Path) -> str:
    files = {}
    for filename, _, _ in TABLES.values():
        with (folder / filename).open("rb") as source:
            files[filename] = hashlib.file_digest(source, "sha256").hexdigest()
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="输出四份实际导入 CSV 的不可变版本摘要")
    parser.add_argument("--csv-dir", type=Path, required=True)
    print(revision(parser.parse_args().csv_dir))
