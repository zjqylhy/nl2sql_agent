"""在本地临时仓库验证推送拦截，不连接项目远端。"""

import json
from pathlib import Path
import subprocess
import tempfile


def main():
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / ".github/rulesets/master-pr-only.json").read_text())
    assert config["conditions"]["ref_name"] == {
        "include": ["refs/heads/master"], "exclude": []
    }
    assert config["enforcement"] == "active" and config["bypass_actors"] == []
    assert {rule["type"] for rule in config["rules"]} == {
        "pull_request", "deletion", "non_fast_forward", "required_status_checks"
    }
    pr_rule = next(rule for rule in config["rules"] if rule["type"] == "pull_request")
    assert pr_rule["parameters"]["required_approving_review_count"] == 1
    assert pr_rule["parameters"]["dismiss_stale_reviews_on_push"] is True
    ci_rule = next(rule for rule in config["rules"] if rule["type"] == "required_status_checks")
    assert ci_rule["parameters"]["strict_required_status_checks_policy"] is True
    assert {
        (check["context"], check["integration_id"])
        for check in ci_rule["parameters"]["required_status_checks"]
    } == {("checks", 15368), ("agent-quality", 15368)}

    cache = (root / ".cache").resolve()
    cache.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="master-protection-", dir=cache)).resolve()
    assert work.is_relative_to(cache)
    source = work / "source"
    remote = work / "remote.git"
    source.mkdir()

    def git(*args, rejected=False):
        result = subprocess.run(
            ["git", "-C", str(source), *args], text=True, capture_output=True
        )
        if rejected:
            assert result.returncode != 0, "unexpected push success"
            assert "master is protected" in result.stderr, result.stderr
        else:
            assert result.returncode == 0, result.stderr
        return result.stdout

    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    git("init", "-b", "develop")
    git("config", "user.name", "Hook validation")
    git("config", "user.email", "validation@example.invalid")
    git("config", "core.hooksPath", (root / ".githooks").as_posix())
    sample = source / "sample.txt"
    sample.write_text("validation\n")
    git("add", "sample.txt")
    git("commit", "-m", "test fixture")
    git("remote", "add", "origin", str(remote))
    git("push", "origin", "develop")
    git("branch", "feature/check")
    git("push", "origin", "feature/check")
    git("tag", "master")
    git("push", "origin", "refs/tags/master")
    git("branch", "master")
    git("push", "origin", "refs/heads/master", rejected=True)
    git("push", "origin", "feature/check:refs/heads/master", rejected=True)
    git("push", "--force", "origin", "develop:refs/heads/master", rejected=True)
    # 播种临时 master，确保删除和后续更新场景都能真实执行。
    git("-c", "core.hooksPath=", "push", "origin", "refs/heads/master")
    git("push", "origin", "--delete", "refs/heads/master", rejected=True)
    sample.write_text("validation changed\n")
    git("add", "sample.txt")
    git("commit", "-m", "second fixture")
    git("branch", "feature/mixed")
    git("push", "origin", "develop:refs/heads/master", "feature/mixed", rejected=True)
    assert not git("ls-remote", "--heads", "origin", "feature/mixed").strip()
    git("push", "origin", "develop:refs/heads/master", rejected=True)
    print("PASS: ruleset configuration and 9 real Git push cases (3 allowed, 6 rejected).")
    print("Test repositories retained in ignored .cache; project remote was not accessed.")


if __name__ == "__main__":
    main()
