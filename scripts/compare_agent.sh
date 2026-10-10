#!/usr/bin/env bash
# 在同一隔离夹具和 Go 平台上比较两个 Python agent，模型实测重复三轮。
set -euo pipefail
baseline=${1:?baseline checkout required}
mode=${2:-deterministic}
split=${3:-development}
output=${4:-.cache/evaluation}
mkdir -p "$output"
candidate_commit=$(git rev-parse HEAD)
if [[ -n $(git status --porcelain) ]]; then candidate_commit="worktree-$candidate_commit"; fi
baseline_commit=$(git -C "$baseline" rev-parse HEAD)
policy="$baseline/evals/policy.json"
if [[ -f "$policy" ]]; then
  # 不能在同一个实现 MR 里降低门槛、删题或修改评分算法。
  for file in policy.json cases.py oracle.py fixture.csv; do
    cmp "evals/$file" "$baseline/evals/$file" || { echo "Evaluation contract changed: separate review required"; exit 1; }
  done
else
  policy="$output/bootstrap-policy.json"
  printf '%s\n' '{"minimum_accuracy":0.85,"maximum_accuracy_drop":0.02}' > "$policy"
  echo "Initial baseline: using fixed bootstrap policy (85%, 2 percentage points)"
fi
go build -o .cache/evalfixture ./cmd/evalfixture
rounds=1
if [[ "$mode" == live ]]; then rounds=3; fi
for ((round=1;round<=rounds;round++)); do
  python scripts/evaluate_agent.py --agent-root "$baseline/agent" --fixture-server .cache/evalfixture --source-commit "$baseline_commit" --mode "$mode" --split "$split" --max-model-calls 45 --output "$output/baseline-$round.json"
  python scripts/evaluate_agent.py --fixture-server .cache/evalfixture --source-commit "$candidate_commit" --mode "$mode" --split "$split" --max-model-calls 45 --output "$output/candidate-$round.json"
  python scripts/quality_gate.py --candidate "$output/candidate-$round.json" --baseline "$output/baseline-$round.json" --policy "$policy" > "$output/gate-$round.json"
  cat "$output/gate-$round.json"
done
