#!/usr/bin/env bash
# 清单通过门禁后才执行迁移；成功验收后更新发布状态。
set -euo pipefail
umask 077
mode=${1:?deploy or rollback}
env_file=${2:?restricted environment file}
manifest=${3:?release manifest}
state=${4:?private state directory}
record=${5:?backup or compatibility record}
smoke=${6:?external smoke executable}
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
[[ "$mode" == deploy || "$mode" == rollback ]] || exit 2
[[ -f "$env_file" && -x "$smoke" && -f "$manifest" && -f "$record" ]] || exit 2
for command in docker python3 curl flock; do command -v "$command" >/dev/null; done
mkdir -p -- "$state"
[[ $(stat -c %u "$state") == "$EUID" && $(stat -c %a "$state") == 700 ]] || { echo 'State directory must be owned by the operator with mode 700' >&2; exit 2; }
exec 9>"$state/deploy.lock"
flock -n 9 || { echo 'Another release is running' >&2; exit 1; }
temporary=$(mktemp -d "$state/.deploy-XXXXXX")
stage=validation
finish() {
  result=$?
  rm -f -- "$temporary/override.json"
  rmdir -- "$temporary"
  if (( result != 0 )); then printf 'Release failed at %s; successful state was preserved\n' "$stage" >&2; fi
}
trap finish EXIT
action=prepare
[[ "$mode" == rollback ]] && action=rollback-check
origin=$(python3 "$root/scripts/release.py" "$action" --manifest "$manifest" --state "$state" --record "$record" --env-file "$env_file" --override "$temporary/override.json")
compose=(docker compose --env-file "$env_file" -f "$root/deploy/compose.cloud.yaml" -f "$temporary/override.json")
stage=pull
"${compose[@]}" --profile maintenance pull api agent web migrate
if [[ "$mode" == deploy ]]; then
  stage=migration
  "${compose[@]}" run --rm --no-deps migrate
fi
stage=readiness
# 不带 --no-deps：更新 agent 后按健康依赖启动 API/web。
"${compose[@]}" up --wait --wait-timeout 120 --force-recreate api agent web
stage=https
curl --fail --silent --show-error --max-time 15 "$origin/healthz" >/dev/null
stage=smoke
# 冒烟脚本负责登录、规则/模型查询及 SSE；返回非零即保留上一成功状态。
"$smoke" "$origin"
stage=record
python3 "$root/scripts/release.py" record --manifest "$manifest" --state "$state"
printf '%s succeeded\n' "$mode"
