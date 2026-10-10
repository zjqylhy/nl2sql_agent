# master 分支保护规格与方案

## 已确认需求

- 默认主分支为 `master`，原 `main` 重命名为 `master`，同步本地分支引用。
- 修改必须在新分支提交并 push，再通过 PR/MR 合入 `master`。
- 禁止直接 push、强推或删除 `master`，管理员也不配置绕过权限。
- 合入至少需要 1 个 approve，新增提交会使旧 approve 失效；此处的 PR 对应 GitHub 的合并请求。
- 必需 CI 为 GitHub Actions 的 `checks`（M1 validation）和 `agent-quality`（Agent quality），两项全部通过，且分支需与最新主分支同步。

## 远端强制规则

`.github/rulesets/master-pr-only.json` 是可导入 GitHub 的分支 ruleset，精确匹配
`refs/heads/master`，适用于未来创建的同名分支。

仓库文件本身不会启用 GitHub 保护。仓库管理员需在 Settings → Rules → Rulesets
导入该 JSON，保存后确认状态为 Active、目标为 `master`、Bypass list 为空。
也可将该 JSON 提交至 GitHub REST API 的
`POST /repos/zjqylhy/nl2sql_agent/rulesets`；已有同名规则时应更新该规则，避免重复创建。
GitHub 账号方案必须支持该仓库的 rulesets 功能。

## 本地提前拦截

`.githooks/pre-push` 按远端目标引用拦截推送，覆盖普通推送、
`feature:master`、强推、删除和一次推送多个引用。其他分支及同名 tag 可正常推送。

每个新克隆需在仓库根目录启用：

```powershell
git config --local core.hooksPath .githooks
```

如果已配置其他 hooks，应先合并现有 hook，避免覆盖原有检查。
本地 hook 可以被 `--no-verify` 或配置修改跳过，必须结合远端规则才能强制约束所有客户端。

## 开发流程

```powershell
git switch -c feature/my-change
# 完成修改并验证。
git add <修改文件>
git commit -m "描述修改"
git push -u origin feature/my-change
```

推送后在 GitHub 创建 PR，base 选择 `master`。获得至少 1 个 approve，且必需 CI 全部通过后，通过 GitHub 合入。
手动触发的 Live agent acceptance 不属于每个 PR 的必需 CI；它依赖独立基线和模型环境。

## 验收标准

本地验证命令：`.\.venv\Scripts\python.exe scripts/test_master_protection.py`。
脚本仅使用 `.cache` 下的临时仓库，保留夹具方便排查，不访问项目远端。

- 本地真实 Git 推送至临时仓库：`master` 的普通推送、目标映射、强推、删除、混合引用均失败。
- 新功能分支、其他开发分支和 `master` tag 推送成功。
- 远端默认分支为 `master`，不存在旧的 `main` 分支。
- 远端读取 ruleset，确认 Active、无绕过权限，包含 PR、删除保护、强推保护和必需状态检查。
- PR 规则要求至少 1 个 approve，新增提交使旧 approve 失效；必需检查绑定 GitHub Actions，且启用最新主分支验证。
- 远端验收需确认直接推送被拒绝，缺少 approve 或任一必需 CI 未通过时禁止合入。
