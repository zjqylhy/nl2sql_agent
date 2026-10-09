"""下载官方公开数据；只提取预期 CSV，不接受 HTML 登录页或任意压缩包路径。"""
import argparse
import hashlib
import json
import shutil
import urllib.request
import zipfile
from pathlib import Path

from import_olist import TABLES

URL = "https://www.kaggle.com/api/v1/datasets/download/olistbr/brazilian-ecommerce"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("data/olist"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    archive = args.output / "olist.zip"
    with urllib.request.urlopen(URL, timeout=60) as response, archive.open("wb") as target:
        shutil.copyfileobj(response, target)
    if not zipfile.is_zipfile(archive):
        raise SystemExit("官方下载未返回 ZIP，可能需要 Kaggle 登录；请手动下载并解压 CSV。")
    names = [filename for filename, _, _ in TABLES.values()]
    with zipfile.ZipFile(archive) as source:
        for name in names:
            info = source.getinfo(name)
            if info.file_size > 512 * 1024 * 1024:
                raise SystemExit("CSV 体积超出预期")
            with source.open(info) as raw, (args.output / name).open("wb") as out:
                shutil.copyfileobj(raw, out)
    with archive.open("rb") as raw:
        digest = hashlib.file_digest(raw, "sha256").hexdigest()
    manifest = {"source": URL, "sha256": digest, "files": names}
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已下载官方数据并记录校验值：", args.output)


if __name__ == "__main__":
    main()
