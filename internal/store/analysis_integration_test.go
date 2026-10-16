package store

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"
)

func analysisFixture(t *testing.T) (*Store, User, AnalysisRun) {
	t.Helper()
	s := integrationStore(t)
	u := testUser(t, s, "alpha", "analyst")
	r, err := s.CreateAnalysis(context.Background(), u, "request-one", "本月 GMV 前三州合计", nil, AnalysisMetadata{ContextMetadata: ContextMetadata{Binding: strings.Repeat("b", 64), AnalysisDate: "2018-08-31", CatalogHash: strings.Repeat("c", 64)}, DatasetRevision: strings.Repeat("d", 64), RuntimeVersion: "analysis-1", Model: "fixture"}, AnalysisPolicy{Currency: "TEST", PriceVersion: "fixture", InputPrice: 1000, OutputPrice: 2000, MaxCost: 10000, Model: "fixture", Tokenizer: "byte_bpe_upper_bound"})
	if err != nil {
		t.Fatal(err)
	}
	return s, u, r
}
func claimAnalysis(t *testing.T, s *Store) Task {
	t.Helper()
	task, err := s.ClaimWithAnalysis(context.Background(), true)
	if err != nil {
		t.Fatal(err)
	}
	return task
}
func beginAnalysis(t *testing.T, s *Store, task Task) (AnalysisRun, AnalysisStep) {
	t.Helper()
	r, step, err := s.BeginAnalysisStep(context.Background(), task)
	if err != nil {
		t.Fatal(err)
	}
	return r, step
}
func modelChoice(action string, args json.RawMessage) AnalysisOutcome {
	var out AnalysisOutcome
	_ = json.Unmarshal(marshal(map[string]any{"status": "completed", "model_sent": true, "usage": map[string]int{"prompt_tokens": 100, "completion_tokens": 20}, "decision": map[string]any{"spec": map[string]any{"goal": "top_n_total", "current": map[string]string{"start": "2018-07-01", "end_exclusive": "2018-08-01"}, "n": 3}, "action": AnalysisAction{Action: action, Arguments: args}}}), &out)
	return out
}
func completeChoice(t *testing.T, s *Store, task Task, step AnalysisStep, action string) {
	t.Helper()
	if err := s.CompleteAnalysisStep(context.Background(), task, step, modelChoice(action, json.RawMessage(`{"plan":{}}`)), time.Millisecond); err != nil {
		t.Fatal(err)
	}
}
func artifactOutcome(kind, ref string) AnalysisOutcome {
	return AnalysisOutcome{Status: "completed", Artifact: marshal(map[string]any{"kind": kind, "result_ref": strings.Repeat(ref, 64), "binding": strings.Repeat("b", 64), "metrics_used": []string{"gmv"}, "data": map[string]any{"complete": true, "value": "240"}})}
}

func TestAnalysisProviderUsageBeyondReservationStops(t *testing.T) {
	s, u, r := analysisFixture(t)
	task := claimAnalysis(t, s)
	_, step := beginAnalysis(t, s, task)
	out := modelChoice("query_metrics", json.RawMessage(`{"plan":{}}`))
	tooMany := int64(6001)
	out.Usage.Input = &tooMany
	if err := s.CompleteAnalysisStep(context.Background(), task, step, out, time.Millisecond); err != nil {
		t.Fatal(err)
	}
	run, err := s.Analysis(context.Background(), u, r.ID)
	if err != nil || run.Status != "failed" || run.State.Tokens != 7024 || run.Usage.UnknownCalls != 1 {
		t.Fatal("untrusted usage released reservation or continued", run.State, err)
	}
}

func TestAnalysisFixedDecisionAndHistoricalMetricDependencies(t *testing.T) {
	s, u, r := analysisFixture(t)
	ctx := context.Background()
	if _, err := s.Pool.Exec(ctx, `UPDATE analysis_runs SET policy=jsonb_set(policy,'{strategy}','"fixed"'),required_metrics=ARRAY['gmv','orders'] WHERE id=$1`, r.ID); err != nil {
		t.Fatal(err)
	}
	task := claimAnalysis(t, s)
	_, step := beginAnalysis(t, s, task)
	completeChoice(t, s, task, step, "query_metrics")
	run, err := s.Analysis(ctx, u, r.ID)
	if err != nil || len(run.Metrics) != 2 || run.State.ModelCalls != 1 || run.State.Phase != "decide" {
		t.Fatal("decision discarded historical dependency", run, err)
	}
	task = claimAnalysis(t, s)
	run, step = beginAnalysis(t, s, task)
	if step.Phase != "fixed" || run.State.ModelCalls != 1 || run.State.Tokens != 120 {
		t.Fatal("fixed phase reserved model budget", run.State, step.Phase)
	}
	if _, err = s.Pool.Exec(ctx, `UPDATE users SET allowed_metrics=ARRAY['gmv'] WHERE id=$1`, u.ID); err != nil {
		t.Fatal(err)
	}
	u.AllowedMetrics = []string{"gmv"}
	if _, err = s.Analysis(ctx, u, r.ID); !errors.Is(err, ErrPermission) {
		t.Fatal("historical metric revocation was bypassed", err)
	}
}

func TestAnalysisRetentionPreservesReportAndUsage(t *testing.T) {
	s, u, r := analysisFixture(t)
	ctx := context.Background()
	task := claimAnalysis(t, s)
	_, step := beginAnalysis(t, s, task)
	completeChoice(t, s, task, step, "finish")
	task = claimAnalysis(t, s)
	_, step = beginAnalysis(t, s, task)
	if err := s.CompleteAnalysisStep(ctx, task, step, artifactOutcome("report", "c"), time.Millisecond); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Pool.Exec(ctx, `UPDATE analysis_runs SET updated_at=NOW()-INTERVAL '31 days' WHERE id=$1`, r.ID); err != nil {
		t.Fatal(err)
	}
	if count, err := s.PruneAnalysis(ctx); err != nil || count != 1 {
		t.Fatal(count, err)
	}
	run, err := s.Analysis(ctx, u, r.ID)
	if err != nil || len(run.Steps) != 0 || len(run.Artifacts) != 1 || run.Usage.KnownTokens != 120 || run.State.ArchivedUsage == nil {
		t.Fatal("retention lost final result or cost evidence", run, err)
	}
}

func TestAnalysisCreationIdempotenceOwnershipAndFeatureGate(t *testing.T) {
	s, u, r := analysisFixture(t)
	ctx := context.Background()
	again, err := s.CreateAnalysis(ctx, u, "request-one", r.Question, nil, r.Metadata, r.Policy)
	if err != nil || again.ID != r.ID {
		t.Fatal("identical request was not idempotent", err)
	}
	if _, err = s.CreateAnalysis(ctx, u, "request-one", "changed", nil, r.Metadata, r.Policy); !errors.Is(err, ErrConflict) {
		t.Fatal("changed request reused ID", err)
	}
	other := testUser(t, s, "alpha", "other")
	if _, err = s.Analysis(ctx, other, r.ID); !errors.Is(err, ErrMissing) {
		t.Fatal("analysis shared with other user", err)
	}
	if _, err = s.Task(ctx, User{ID: other.ID, TenantID: "alpha", Role: "admin"}, r.ID); !errors.Is(err, ErrMissing) {
		t.Fatal("old task endpoint exposed private run", err)
	}
	if _, err = s.Claim(ctx); !errors.Is(err, ErrMissing) {
		t.Fatal("disabled analysis was claimed", err)
	}
	task := claimAnalysis(t, s)
	if task.Kind != "analysis" {
		t.Fatal(task.Kind)
	}
}

func TestAnalysisCheckpointCacheAndOldWorkerFence(t *testing.T) {
	s, u, r := analysisFixture(t)
	ctx := context.Background()
	task := claimAnalysis(t, s)
	_, step := beginAnalysis(t, s, task)
	completeChoice(t, s, task, step, "query_metrics")
	run, err := s.Analysis(ctx, u, r.ID)
	if err != nil || run.State.Tokens != 120 || run.State.Phase != "tool" {
		t.Fatal(run.State, err)
	}
	old := task
	task = claimAnalysis(t, s)
	_, step = beginAnalysis(t, s, task)
	if err = s.CompleteAnalysisStep(ctx, old, step, artifactOutcome("query", "a"), time.Millisecond); !errors.Is(err, ErrConflict) {
		t.Fatal("old worker was accepted", err)
	}
	if err = s.CompleteAnalysisStep(ctx, task, step, artifactOutcome("query", "a"), time.Millisecond); err != nil {
		t.Fatal(err)
	}
	task = claimAnalysis(t, s)
	_, step = beginAnalysis(t, s, task)
	completeChoice(t, s, task, step, "query_metrics")
	task = claimAnalysis(t, s)
	run, step = beginAnalysis(t, s, task)
	if len(step.Cached) == 0 || run.State.SQLCalls != 1 {
		t.Fatal("completed query repeated", run.State.SQLCalls)
	}
	var cached AnalysisOutcome
	if json.Unmarshal(step.Cached, &cached) != nil {
		t.Fatal("invalid cache")
	}
	if err = s.CompleteAnalysisStep(ctx, task, step, cached, time.Millisecond); err != nil {
		t.Fatal(err)
	}
	run, _ = s.Analysis(ctx, u, r.ID)
	if len(run.Artifacts) != 1 {
		t.Fatal("duplicate result artifact")
	}
}

func TestUnknownModelStopsWithoutReissuingAndRetainsReservation(t *testing.T) {
	s, u, r := analysisFixture(t)
	ctx := context.Background()
	task := claimAnalysis(t, s)
	_, step := beginAnalysis(t, s, task)
	if _, err := s.Pool.Exec(ctx, `UPDATE tasks SET lease_until=NOW()-INTERVAL '1 second' WHERE id=$1`, r.ID); err != nil {
		t.Fatal(err)
	}
	newer := claimAnalysis(t, s)
	if _, _, err := s.BeginAnalysisStep(ctx, newer); !errors.Is(err, ErrAnalysisBudget) {
		t.Fatal("unknown model was retried", err)
	}
	run, _ := s.Analysis(ctx, u, r.ID)
	if run.Status != "failed" || run.State.ModelCalls != 1 || run.State.Tokens != 7024 {
		t.Fatal(run.State, run.Status)
	}
	var state string
	if err := s.Pool.QueryRow(ctx, `SELECT status FROM analysis_budget_ledger WHERE step_id=$1`, step.ID).Scan(&state); err != nil || state != "unknown" {
		t.Fatal(state, err)
	}
	if err := s.CompleteAnalysisStep(ctx, task, step, modelChoice("lookup_metrics", json.RawMessage(`{"metrics":["gmv"]}`)), time.Millisecond); !errors.Is(err, ErrConflict) {
		t.Fatal("late model advanced failed task", err)
	}
}

func TestSQLRetryBoundAndPartialFailure(t *testing.T) {
	s, u, r := analysisFixture(t)
	ctx := context.Background()
	task := claimAnalysis(t, s)
	_, step := beginAnalysis(t, s, task)
	completeChoice(t, s, task, step, "query_metrics")
	for i := 0; i < 2; i++ {
		task = claimAnalysis(t, s)
		_, step = beginAnalysis(t, s, task)
		if err := s.CompleteAnalysisStep(ctx, task, step, AnalysisOutcome{Status: "error", ErrorCode: "sql_timeout", Message: "查询超时"}, time.Second); err != nil {
			t.Fatal(err)
		}
		if _, err := s.Pool.Exec(ctx, `UPDATE analysis_runs SET ready_at=NOW() WHERE id=$1`, r.ID); err != nil {
			t.Fatal(err)
		}
	}
	run, _ := s.Analysis(ctx, u, r.ID)
	if run.Status != "failed" || run.State.SQLRetries != 1 || run.State.SQLCalls != 2 {
		t.Fatal(run.Status, run.State)
	}
	task, err := s.Task(ctx, u, r.ID)
	if err != nil || !strings.Contains(string(task.Result), `"partial": true`) {
		t.Fatal(string(task.Result), err)
	}
}

func TestAnalysisClarificationIdempotencyAndStopPreventsLateWrite(t *testing.T) {
	s, u, r := analysisFixture(t)
	ctx := context.Background()
	task := claimAnalysis(t, s)
	_, step := beginAnalysis(t, s, task)
	out := modelChoice("request_clarification", json.RawMessage(`{"message":"请选择日期","missing_fields":["current"]}`))
	out.Decision.Spec = json.RawMessage(`null`)
	if err := s.CompleteAnalysisStep(ctx, task, step, out, time.Millisecond); err != nil {
		t.Fatal(err)
	}
	task = claimAnalysis(t, s)
	_, step = beginAnalysis(t, s, task)
	if err := s.CompleteAnalysisStep(ctx, task, step, AnalysisOutcome{Status: "needs_clarification", Message: "请选择日期"}, time.Millisecond); err != nil {
		t.Fatal(err)
	}
	run, _ := s.Analysis(ctx, u, r.ID)
	if run.Status != "needs_clarification" {
		t.Fatal(run.Status)
	}
	if err := s.AnswerAnalysis(ctx, u, r.ID, "answer-id", "2018年7月", run.Version-1); !errors.Is(err, ErrConflict) {
		t.Fatal("stale answer accepted", err)
	}
	for i := 0; i < 2; i++ {
		if err := s.AnswerAnalysis(ctx, u, r.ID, "answer-id", "2018年7月", run.Version); err != nil {
			t.Fatal(err)
		}
	}
	task = claimAnalysis(t, s)
	_, step = beginAnalysis(t, s, task)
	if err := s.Stop(ctx, u, r.ID); err != nil {
		t.Fatal(err)
	}
	if err := s.CompleteAnalysisStep(ctx, task, step, modelChoice("lookup_metrics", json.RawMessage(`{"metrics":["gmv"]}`)), time.Millisecond); !errors.Is(err, ErrConflict) {
		t.Fatal("stopped task accepted late result", err)
	}
	run, _ = s.Analysis(ctx, u, r.ID)
	if run.Status != "canceled" || len(run.State.Answers) != 1 {
		t.Fatal(run.Status, run.State)
	}
}

func TestPersistenceFailureRollsBackEvidenceAndCheckpoint(t *testing.T) {
	s, u, r := analysisFixture(t)
	ctx := context.Background()
	task := claimAnalysis(t, s)
	_, step := beginAnalysis(t, s, task)
	completeChoice(t, s, task, step, "query_metrics")
	task = claimAnalysis(t, s)
	_, step = beginAnalysis(t, s, task)
	_, err := s.Pool.Exec(ctx, `CREATE FUNCTION reject_artifact() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected persistence failure'; END $$; CREATE TRIGGER fail_artifact BEFORE INSERT ON analysis_artifacts FOR EACH ROW EXECUTE FUNCTION reject_artifact()`)
	if err != nil {
		t.Fatal(err)
	}
	if err = s.CompleteAnalysisStep(ctx, task, step, artifactOutcome("query", "a"), time.Millisecond); err == nil {
		t.Fatal("persistence failure hidden")
	}
	run, _ := s.Analysis(ctx, u, r.ID)
	if run.Status != "querying" || run.State.ActiveStep != step.ID || len(run.Artifacts) != 0 {
		t.Fatal("uncommitted result advanced state", run.State)
	}
	if _, err = s.Pool.Exec(ctx, `DROP TRIGGER fail_artifact ON analysis_artifacts`); err != nil {
		t.Fatal(err)
	}
	if _, err = s.Pool.Exec(ctx, `UPDATE tasks SET lease_until=NOW()-INTERVAL '1 second' WHERE id=$1`, r.ID); err != nil {
		t.Fatal(err)
	}
	task = claimAnalysis(t, s)
	run, newStep := beginAnalysis(t, s, task)
	if run.State.SQLRetries != 1 || run.State.SQLCalls != 2 {
		t.Fatal("unknown query recovery did not consume retry", run.State)
	}
	if err = s.CompleteAnalysisStep(ctx, task, newStep, artifactOutcome("query", "a"), time.Millisecond); err != nil {
		t.Fatal(err)
	}
}
