package store

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func TestDecodeTelemetryRejectsUntrustedOrUnknownFields(t *testing.T) {
	task := Task{ID: strings.Repeat("a", 48), Version: 2}
	valid := `{"version":1,"task_id":"` + task.ID + `","execution_version":2,"parser":"llm","model_calls":1,"total_tokens":null,"stages_ms":{"model":10},"error_code":"model_timeout"}`
	if DecodeTelemetry(json.RawMessage(valid), task) == nil {
		t.Fatal("unknown provider usage rejected")
	}
	for _, bad := range []string{strings.Replace(valid, `"execution_version":2`, `"execution_version":1`, 1), strings.Replace(valid, `"model_calls":1`, `"model_calls":2`, 1), strings.Replace(valid, `"model":10`, `"question":10`, 1), strings.Replace(valid, `"total_tokens":null`, `"total_tokens":-1`, 1), strings.Replace(valid, `"stages_ms":{"model":10}`, `"stages_ms":null`, 1), strings.Replace(valid, `"version":1`, `"question":"secret","version":1`, 1)} {
		if DecodeTelemetry(json.RawMessage(bad), task) != nil {
			t.Fatal("unsafe telemetry accepted")
		}
	}
}

func TestAttemptsKeepRetryCancelAndTenantBoundaries(t *testing.T) {
	s := integrationStore(t)
	ctx := context.Background()
	a := testUser(t, s, "alpha", "operator")
	b := testUser(t, s, "beta", "operator")
	task, err := s.CreateTask(ctx, a, Input{Question: "本月 GMV"})
	if err != nil {
		t.Fatal(err)
	}
	first, err := s.Claim(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if err = s.Retry(ctx, first, "retry"); err != nil {
		t.Fatal(err)
	}
	if err = s.RecordAttempt(ctx, first, nil, 10*time.Millisecond, 0, "agent_network"); err != nil {
		t.Fatal(err)
	}
	second, err := s.Claim(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if err = s.Stop(ctx, a, task.ID); err != nil {
		t.Fatal(err)
	}
	if err = s.Finish(ctx, second, "completed", json.RawMessage(`{}`), ""); err != nil {
		t.Fatal(err)
	}
	if err = s.RecordAttempt(ctx, second, nil, 20*time.Millisecond, 100, ""); err != nil {
		t.Fatal(err)
	}
	if err = s.RecordAttempt(ctx, second, nil, 99*time.Millisecond, 999, "internal"); err != nil {
		t.Fatal(err)
	}
	var state, code string
	var ms float64
	if err = s.Pool.QueryRow(ctx, `SELECT state,error_code,call_ms FROM task_attempts WHERE task_id=$1 AND execution_version=$2`, second.ID, second.Version).Scan(&state, &code, &ms); err != nil {
		t.Fatal(err)
	}
	if state != "canceled" || code != "canceled" || ms != 20 {
		t.Fatalf("late observation changed terminal attempt: %s %s %v", state, code, ms)
	}
	result, err := s.Operations(ctx, a.TenantID, 7)
	if err != nil {
		t.Fatal(err)
	}
	var summary struct {
		Attempts int `json:"attempts"`
		Retries  int `json:"retries"`
		Unknown  int `json:"unknown_usage_attempts"`
		Latency  map[string]struct {
			Samples int     `json:"samples"`
			P95     float64 `json:"p95"`
		} `json:"latency_ms"`
	}
	if err = json.Unmarshal(result, &summary); err != nil {
		t.Fatal(err)
	}
	if summary.Attempts != 2 || summary.Retries != 1 || summary.Unknown != 2 || summary.Latency["call"].Samples != 2 || summary.Latency["call"].P95 != 19.5 {
		t.Fatalf("incorrect aggregation: %s", result)
	}
	other, err := s.Operations(ctx, b.TenantID, 7)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(other), task.ID) || !strings.Contains(string(other), `"attempts": 0`) {
		t.Fatalf("cross tenant data: %s", other)
	}
	_, err = s.Pool.Exec(ctx, `UPDATE task_attempts SET ended_at=NOW()-INTERVAL '31 days' WHERE task_id=$1`, task.ID)
	if err != nil {
		t.Fatal(err)
	}
	n, err := s.PruneAttempts(ctx)
	if err != nil || n != 2 {
		t.Fatal(n, err)
	}
	if _, err = s.Task(ctx, a, task.ID); err != nil {
		t.Fatal("pruning deleted business history", err)
	}
}

func TestExpiredLeasePreservesOldAttempt(t *testing.T) {
	s := integrationStore(t)
	ctx := context.Background()
	u := testUser(t, s, "alpha", "operator")
	if _, err := s.CreateTask(ctx, u, Input{Question: "本月 GMV"}); err != nil {
		t.Fatal(err)
	}
	old, err := s.Claim(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = s.Pool.Exec(ctx, `UPDATE tasks SET lease_until=NOW()-INTERVAL '1 second' WHERE id=$1`, old.ID); err != nil {
		t.Fatal(err)
	}
	newer, err := s.Claim(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if err = s.Finish(ctx, old, "completed", json.RawMessage(`{}`), ""); err != nil {
		t.Fatal(err)
	}
	if err = s.RecordAttempt(ctx, old, nil, time.Millisecond, 0, ""); err != nil {
		t.Fatal(err)
	}
	var states []string
	rows, err := s.Pool.Query(ctx, `SELECT state FROM task_attempts WHERE task_id=$1 ORDER BY execution_version`, old.ID)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	for rows.Next() {
		var v string
		if err = rows.Scan(&v); err != nil {
			t.Fatal(err)
		}
		states = append(states, v)
	}
	if len(states) != 2 || states[0] != "lease_expired" || states[1] != "running" {
		t.Fatal(states)
	}
	task, err := s.Task(ctx, u, old.ID)
	if err != nil || task.Version != newer.Version || task.Status != "querying" {
		t.Fatal("late result changed reclaimed task", err)
	}
}
