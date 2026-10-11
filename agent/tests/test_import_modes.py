from pathlib import Path
from types import SimpleNamespace

import pytest
from tools import import_olist as importer


class Cursor:
    def __init__(self, responses):
        self.responses, self.sql = iter(responses), []
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, sql, params=None): self.sql.append(sql)
    def fetchone(self): return next(self.responses)


@pytest.mark.parametrize("responses", [[(0,)], [(1,), (1,)]])
def test_existing_database_missing_or_nonempty_refuses_before_ddl(responses):
    cursor = Cursor(responses)
    with pytest.raises(ValueError):
        importer.provision(SimpleNamespace(cursor=lambda: cursor), Path("missing"), "fixture", "ro_user", "", [], use_existing_database=True, skip_account_provisioning=True)
    assert all(sql.startswith("SELECT") for sql in cursor.sql)


def test_existing_database_and_account_are_not_recreated(monkeypatch):
    monkeypatch.setattr(importer, "TABLES", {})
    cursor = Cursor([(1,), (0,), (0, 0), (0, None)])
    importer.provision(SimpleNamespace(cursor=lambda: cursor), Path("."), "fixture", "ro_user", "", [], use_existing_database=True, skip_account_provisioning=True)
    assert not any(sql.startswith(("CREATE DATABASE", "CREATE USER", "GRANT", "ALTER USER", "DROP", "TRUNCATE")) for sql in cursor.sql)
    assert any("SQL SECURITY DEFINER VIEW" in sql for sql in cursor.sql)


def test_skip_account_does_not_prompt_for_readonly_password(monkeypatch, tmp_path):
    monkeypatch.setattr(importer, "TABLES", {})
    monkeypatch.setattr("sys.argv", ["import", "--csv-dir", str(tmp_path), "--schema", "fixture", "--readonly-user", "ro_user", "--skip-account-provisioning"])
    monkeypatch.setenv("MYSQL_ADMIN_PASSWORD", "admin-password")
    monkeypatch.setattr(importer.getpass, "getpass", lambda *args: pytest.fail("password prompt should not run"))
    monkeypatch.setattr(importer.pymysql, "connect", lambda **options: SimpleNamespace(close=lambda: None))
    observed = []
    monkeypatch.setattr(importer, "provision", lambda *args, **kwargs: observed.append((args, kwargs)))
    importer.main()
    assert observed[0][0][4] == "" and observed[0][1]["skip_account_provisioning"]
