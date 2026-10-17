"""保留配置边界测试：特殊字符不解释，秘密文件权限受限且不允许覆盖。"""
import os

import pytest

from scripts.private_env import dotenv, read_env, write_private


def test_private_configuration_round_trip_and_no_overwrite(tmp_path):
    path = tmp_path / '.env.private'
    values = {'API_KEY': "key'$value", 'DATASOURCES': '{"password":"test=only"}'}
    write_private(path, dotenv(values))
    assert read_env(path) == values
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        write_private(path, 'replacement')
    assert path.read_bytes() == before


@pytest.mark.parametrize('value', ['line\nbreak', 'line\rbreak', 'null\0byte'])
def test_environment_values_cannot_inject_lines(value):
    with pytest.raises(ValueError, match='invalid_environment_value'):
        dotenv({'KEY': value})


def test_environment_rejects_unquoted_files_and_public_permissions(tmp_path):
    path = tmp_path / '.env.private'
    write_private(path, 'KEY=unquoted\n')
    with pytest.raises(ValueError, match='generated_environment_required'):
        read_env(path)
    if os.name == 'posix':
        path.chmod(0o644)
        with pytest.raises(ValueError, match='environment_must_be_private'):
            read_env(path)
