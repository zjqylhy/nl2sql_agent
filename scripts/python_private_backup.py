"""服务器定时备份入口；使用独立验收镜像中的官方 PG 客户端，不导出到本地。"""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import secrets
import subprocess
import tempfile

from scripts.private_ops import read_env


def run(env_file, source):
    values = read_env(env_file)
    source = source.resolve(strict=True)
    if not (source / "scripts/backup_python_platform.py").is_file():
        raise ValueError("完整发布源码不存在")
    root = env_file.resolve().parent
    backups = root / "backups"
    backups.mkdir(mode=0o700, exist_ok=True)
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(4)
    # Docker env-file 中不使用 Compose 的单引号语法，密码不出现在进程参数里。
    password = values["POSTGRES_ADMIN_PASSWORD"]
    if not password.isalnum():
        raise ValueError("数据库密码必须采用部署时生成的随机字母数字值")
    with tempfile.TemporaryDirectory(prefix="python-backup-", dir=root) as temporary:
        config = Path(temporary) / "database.env"
        descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write("BACKUP_DATABASE_URL=postgresql://platform_migrator:" + password + "@postgres:5432/platform?sslmode=disable\n")
        subprocess.run(["docker", "run", "--rm", "--network", "olist-python-agent_backend",
            "--env-file", str(config), "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "-v", str(source) + ":/workspace:ro",
            "-v", str(backups) + ":/backups", "olist-python-validation:" + values["APP_TAG"],
            "scripts/backup_python_platform.py", "backup", "/backups/" + name],
            check=True, timeout=360)
    return backups / name


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path("/srv/olist/python/.env.private"))
    parser.add_argument("--source", type=Path, default=Path("/srv/olist/python/current"))
    args = parser.parse_args()
    # Python 文件直接执行时补入仓库根目录的方式由 -m 保证。
    print(run(args.env_file, args.source))
