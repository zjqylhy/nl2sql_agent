"""受限部署配置的解析和写入；凭据文件不允许覆盖或放宽权限。"""
import os
import re


def dotenv(values):
    lines = []
    for key, value in values.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or any(c in value for c in "\r\n\0"):
            raise ValueError("invalid_environment_value")
        lines.append(key + "='" + value.replace("'", "\\'") + "'")
    return "\n".join(lines) + "\n"


def read_env(path):
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise ValueError("environment_must_be_private")
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or not (value.startswith("'") and value.endswith("'")):
            raise ValueError("generated_environment_required")
        values[key] = value[1:-1].replace("\\'", "'")
    return values


def write_private(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(data)
