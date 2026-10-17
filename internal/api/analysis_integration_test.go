package api

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	"nl2sql_agent/internal/store"
)

func TestAnalysisHTTPWorkflowBudgetAndRevocation(t *testing.T) {
	db := conversationDatabase(t)
	ctx := context.Background()
	if _, err := db.Pool.Exec(ctx, `INSERT INTO tenants VALUES('alpha','test')`); err != nil {
		t.Fatal(err)
	}
	user, err := db.AddUser(ctx, "alpha", "analyst", "test-password-123", "ops", []string{"gmv"})
	if err != nil {
		t.Fatal(err)
	}
	other, err := db.AddUser(ctx, "alpha", "other", "test-password-123", "admin", store.Metrics)
	if err != nil {
		t.Fatal(err)
	}
	token, _ := db.NewSession(ctx, user)
	otherToken, _ := db.NewSession(ctx, other)
	meta := store.AnalysisMetadata{ContextMetadata: store.ContextMetadata{Binding: strings.Repeat("b", 64), CatalogHash: strings.Repeat("c", 64), AnalysisDate: "2018-08-31"}, DatasetRevision: strings.Repeat("d", 64), RuntimeVersion: "analysis-1", Model: "fixture", ModelReady: true}
	policy := &store.AnalysisPolicy{Currency: "TEST", PriceVersion: "fixture", Model: "fixture", Tokenizer: "byte_bpe_upper_bound", InputPrice: 1000, OutputPrice: 2000, MaxCost: 10000}
	var decisions atomic.Int32
	var changed atomic.Bool
	agent := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Service-Key") != "fixture-service" {
			w.WriteHeader(401)
			return
		}
		if r.URL.Path == "/internal/analysis/context" {
			value := meta
			if changed.Load() {
				value.Binding = strings.Repeat("e", 64)
			}
			_ = json.NewEncoder(w).Encode(value)
			return
		}
		if r.URL.Path == "/internal/analysis/decide" {
			var reservations int
			if e := db.Pool.QueryRow(ctx, `SELECT COUNT(*) FROM analysis_budget_ledger WHERE status='reserved'`).Scan(&reservations); e != nil || reservations != 1 {
				t.Error("model called without durable reservation", reservations, e)
			}
			count := decisions.Add(1)
			action := "query_metrics"
			if count == 2 {
				action = "calculate"
			}
			if count >= 3 {
				action = "finish"
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"status": "completed", "model_sent": true, "usage": map[string]int{"prompt_tokens": 100, "completion_tokens": 20}, "decision": map[string]any{"spec": map[string]any{"goal": "top_n_total", "current": map[string]string{"start": "2018-07-01", "end_exclusive": "2018-08-01"}, "n": 3}, "action": map[string]any{"action": action, "arguments": map[string]any{}}}})
			return
		}
		if r.URL.Path == "/internal/analysis/tool" {
			var input struct {
				Action store.AnalysisAction `json:"action"`
			}
			if json.NewDecoder(r.Body).Decode(&input) != nil {
				t.Error("bad tool input")
			}
			kind, ref := "query", "a"
			if input.Action.Action == "calculate" {
				kind, ref = "scalar", "f"
			}
			if input.Action.Action == "finish" {
				kind, ref = "report", "c"
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"status": "completed", "artifact": map[string]any{"kind": kind, "result_ref": strings.Repeat(ref, 64), "binding": meta.Binding, "metrics_used": []string{"gmv"}, "data": map[string]any{"complete": true, "summary": "PRIVATE_RESULT", "value": "240"}}})
			return
		}
		w.WriteHeader(404)
	}))
	defer agent.Close()
	server := New(db, Config{Origin: "http://app.example", AgentURL: agent.URL, ServiceKey: "fixture-service", Analysis: policy})
	handler := server.Handler()
	request := func(method, path, body, cookie string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(method, path, strings.NewReader(body))
		req.Header.Set("Origin", "http://app.example")
		req.Header.Set("Content-Type", "application/json")
		req.AddCookie(&http.Cookie{Name: "session", Value: cookie})
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, req)
		return w
	}
	body := `{"question":"2018年7月 GMV 前三州合计","request_id":"11111111-1111-1111-1111-111111111111"}`
	response := request("POST", "/api/analysis-runs", body, token)
	if response.Code != 202 {
		t.Fatal(response.Code, response.Body.String())
	}
	var run store.AnalysisRun
	if json.Unmarshal(response.Body.Bytes(), &run) != nil {
		t.Fatal("bad response")
	}
	for i := 0; i < 6; i++ {
		task, e := db.ClaimWithAnalysis(ctx, true)
		if e != nil {
			t.Fatal(e)
		}
		server.execute(ctx, task)
	}
	complete := request("GET", "/api/analysis-runs/"+run.ID, "", token)
	if complete.Code != 200 || !strings.Contains(complete.Body.String(), `"status":"completed"`) || decisions.Load() != 3 {
		t.Fatal(complete.Code, complete.Body.String(), decisions.Load())
	}
	var observed int
	var tokens int64
	if err := db.Pool.QueryRow(ctx, `SELECT COUNT(*) FILTER(WHERE telemetry IS NOT NULL),SUM((telemetry->>'total_tokens')::bigint) FROM task_attempts WHERE task_id=$1`, run.ID).Scan(&observed, &tokens); err != nil || observed != 6 || tokens != 360 {
		t.Fatal("analysis attempts missing from operations telemetry", observed, tokens, err)
	}
	if response = request("POST", "/api/analysis-runs", body, token); response.Code != 202 || !strings.Contains(response.Body.String(), run.ID) {
		t.Fatal("idempotence failed", response.Body.String())
	}
	for _, path := range []string{"/api/analysis-runs/" + run.ID, "/api/tasks/" + run.ID, "/api/tasks/" + run.ID + "/events"} {
		w := request("GET", path, "", otherToken)
		if w.Code == 200 || strings.Contains(w.Body.String(), "PRIVATE_RESULT") {
			t.Fatal("private result leaked", path, w.Code)
		}
	}
	if _, err = db.Pool.Exec(ctx, `UPDATE users SET allowed_metrics='{}' WHERE id=$1`, user.ID); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{"/api/analysis-runs/" + run.ID, "/api/tasks/" + run.ID, "/api/tasks/" + run.ID + "/events"} {
		w := request("GET", path, "", token)
		if w.Code == 200 || strings.Contains(w.Body.String(), "PRIVATE_RESULT") {
			t.Fatal("revoked result leaked", path, w.Code)
		}
	}
	var unknown int
	if err = db.Pool.QueryRow(ctx, `SELECT COUNT(*) FROM analysis_budget_ledger WHERE status<>'settled'`).Scan(&unknown); err != nil || unknown != 0 {
		t.Fatal("known usage not settled", err, unknown)
	}
}

func TestAnalysisDisabledDoesNotContactAgent(t *testing.T) {
	server := New(nil, Config{})
	if server.Config.Analysis != nil {
		t.Fatal("analysis enabled without policy")
	}
	for _, p := range []store.AnalysisPolicy{{}, {Currency: "TEST", PriceVersion: "fixture", Model: "fixture", InputPrice: 1, OutputPrice: 1, MaxCost: 1, Tokenizer: "unknown"}} {
		if p.Valid() {
			t.Fatal("invalid budget policy accepted")
		}
	}
}
