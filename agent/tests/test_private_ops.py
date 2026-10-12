import json
import pytest

from scripts import cloud_smoke, private_ops


def test_generated_configuration_is_literal_and_cannot_overwrite(tmp_path):
    path = tmp_path / ".env.private"
    private_ops.init_config(path, "a" * 40, {"gateway_key": "key'$value", "postgres_image": "pg@sha256:a", "mysql_image": "my@sha256:b"})
    values = private_ops.read_env(path)
    assert values["NL2SQL_AGENT_API_KEY"] == "key'$value"
    source = json.loads(values["TENANT_DATASOURCES"])
    assert source["alpha:admin"]["password"] == values["MYSQL_READONLY_PASSWORD"]
    old = path.read_bytes()
    with pytest.raises(FileExistsError):
        private_ops.init_config(path, "a" * 40, {"gateway_key": "new", "postgres_image": "pg", "mysql_image": "my"})
    assert path.read_bytes() == old


@pytest.mark.parametrize("origin", ["http://example.com", "http://127.0.0.1:8088", "http://localhost:8088/path", "http://localhost:8088@evil.example", "http://localhost:8088?x=1"])
def test_private_smoke_rejects_every_other_http_origin(origin):
    with pytest.raises(ValueError, match="https_origin_required"):
        cloud_smoke.run(origin, allow_private=True)


def test_failed_backup_does_not_rotate_valid_backups(tmp_path):
    root = tmp_path / "backups"
    root.mkdir()
    for day in range(1, 9):
        folder = root / f"202610{day:02d}T030000Z-aaaaaaaa"
        folder.mkdir()
        (folder / "manifest.json").write_text("{}")
    stack = object.__new__(private_ops.Stack)
    stack.env_file = tmp_path / ".env.private"
    def fail(*args, **kwargs):
        raise RuntimeError("database_unavailable")
    stack.call = fail
    with pytest.raises(RuntimeError, match="database_unavailable"):
        stack.backup()
    assert len(list(root.glob("*/manifest.json"))) == 8


def test_existing_analysis_database_blocks_initialize(tmp_path):
    (tmp_path / "state").mkdir()
    stack = object.__new__(private_ops.Stack)
    stack.env_file = tmp_path / ".env.private"
    calls = []
    stack.call = lambda *args, **kwargs: calls.append(args) or b""
    stack.mysql = lambda query: [["olist_alpha"]]
    with pytest.raises(ValueError, match="no_overwrite"):
        stack.initialize()
    assert not any(call[0] == "run" for call in calls)


def test_backup_accepts_mysql_single_column_account_definition(tmp_path):
    stack = object.__new__(private_ops.Stack)
    stack.env_file = tmp_path / ".env.private"
    def call(*args, output=None, **kwargs):
        if output:
            output.write(b"fixture database dump")
        return b""
    stack.call = call
    stack.dump_counts = lambda dump: "1|2"
    def mysql(query):
        if query.startswith("SHOW CREATE USER"):
            return [["CREATE USER 'alpha_ro'@'%' IDENTIFIED BY 'fixture-password'"]]
        if query.startswith("SHOW GRANTS"):
            return [["GRANT SELECT ON olist_alpha.metric_orders TO 'alpha_ro'@'%'"]]
        return [[99441]]
    stack.mysql = mysql
    folder = stack.backup()
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["orders"] == 99441 and manifest["postgres_counts"] == "1|2"
    assert "CREATE USER" in (folder / "mysql-accounts.sql").read_text()
    assert set(manifest["sha256"]) == {"postgres.dump", "postgres-roles.sql", "mysql.sql", "mysql-accounts.sql"}


def test_snapshot_counts_escaped_rows_and_empty_tables():
    lines = [b"COPY public.users (id,username) FROM stdin;\n", b"1\tname\\nline\n", b"\\.\n",
             b"COPY public.tasks (id,input) FROM stdin;\n", b"\\.\n"]
    assert private_ops.copy_counts(lines) == "1|0"


def test_snapshot_counts_require_complete_copy_sections():
    with pytest.raises(ValueError, match="incomplete_postgres_snapshot"):
        private_ops.copy_counts([b"COPY public.users (id) FROM stdin;\n", b"1\n"])
