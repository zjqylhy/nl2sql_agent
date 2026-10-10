# master 分支保护规格与方案

## 已确认需求

- 仅保护 `master`，现有 `main` 不改名。
- 修改必须在新分支提交并 push，再通过 PR/MR 合入 `master`。
- 禁止直接 push、强推或删除 `master`，管理员也不配置绕过权限。
- 不额外要求审批人数或 CI 检查；此处的 PR 对应 GitHub 的合并请求。

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

推送后在 GitHub 创建 PR，base 选择 `master`，检查通过后通过 GitHub 合入。
目前仓库只有 `main`，需先按项目决定创建 `master`，才可选择它作为 PR 的 base；本方案不创建或重命名主分支。

## 验收标准

本地验证命令：`.\.venv\Scripts\python.exe scripts/test_master_protection.py`。
脚本仅使用 `.cache` 下的临时仓库，保留夹具方便排查，不访问项目远端。

- 本地真实 Git 推送至临时仓库：`master` 的普通推送、目标映射、强推、删除、混合引用均失败。
- 新功能分支、`main` 和 `master` tag 推送成功。
- 远端启用后读取 ruleset，确认 Active、无绕过权限且三项规则齐全。
- 远端验收需确认直接推送被拒绝、功能分支 PR 可正常合入。
