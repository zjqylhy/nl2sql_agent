package api

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"sync/atomic"
	"testing"

	"nl2sql_agent/internal/store"
)

func conversationDatabase(t *testing.T) *store.Store {
	t.Helper()
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("TEST_DATABASE_URL not configured")
	}
	ctx := context.Background()
	admin, err := store.Open(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	schema := "test_" + store.ID()
	if _, err = admin.Pool.Exec(ctx, "CREATE SCHEMA "+schema); err != nil {
		t.Fatal(err)
	}
	u, err := url.Parse(dsn)
	if err != nil {
		t.Fatal(err)
	}
	q := u.Query()
	q.Set("search_path", schema)
	u.RawQuery = q.Encode()
	db, err := store.Open(ctx, u.String())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		db.Pool.Close()
		_, _ = admin.Pool.Exec(ctx, "DROP SCHEMA "+schema+" CASCADE")
		admin.Pool.Close()
	})
	if err = db.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	return db
}

func TestConversationRoutesProtectContextAndOldTaskEndpoints(t *testing.T) {
	db := conversationDatabase(t)
	ctx := context.Background()
	_, err := db.Pool.Exec(ctx, `INSERT INTO tenants VALUES('alpha','test')`)
	if err != nil {
		t.Fatal(err)
	}
	one, err := db.AddUser(ctx, "alpha", "one", "test-password-123", "ops", []string{"gmv"})
	if err != nil {
		t.Fatal(err)
	}
	other, err := db.AddUser(ctx, "alpha", "other", "test-password-123", "admin", []string{"gmv"})
	if err != nil {
		t.Fatal(err)
	}
	token, _ := db.NewSession(ctx, one)
	otherToken, _ := db.NewSession(ctx, other)
	var changed atomic.Bool
	var queries atomic.Int32
	agent := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Service-Key") != "fixture-service" {
			w.WriteHeader(401)
			return
		}
		binding := strings.Repeat("a", 64)
		if changed.Load() {
			binding = strings.Repeat("b", 64)
		}
		if r.URL.Path == "/internal/context" {
			_ = json.NewEncoder(w).Encode(store.ContextMetadata{Binding: binding, CatalogHash: strings.Repeat("c", 64), AnalysisDate: "2018-08-31"})
			return
		}
		var body struct {
			Context struct {
				Plan json.RawMessage `json:"plan"`
			} `json:"context"`
		}
		if json.NewDecoder(r.Body).Decode(&body) != nil {
			t.Error("invalid trusted request")
		}
		if queries.Add(1) == 1 && string(body.Context.Plan) != "null" {
			t.Error("first round had fabricated context")
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "completed", "result_version": 2, "context_binding": binding, "metrics": []map[string]string{{"id": "gmv"}}, "plan": map[string]any{"version": 1, "metrics": []string{"gmv"}, "dimensions": []string{"state"}, "time_range": map[string]string{"start": "2018-08-01", "end_exclusive": "2018-09-01"}, "filters": []any{}, "order_by": []any{}, "top_n": nil}, "rows": []map[string]string{{"value": "PRIVATE_VALUE"}}})
	}))
	defer agent.Close()
	server := New(db, Config{Origin: "http://app.example", AgentURL: agent.URL, ServiceKey: "fixture-service"})
	handler := server.Handler()
	call := func(method, path, body, session string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		r.AddCookie(&http.Cookie{Name: "session", Value: session})
		r.Header.Set("Origin", "http://app.example")
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	w := call("POST", "/api/conversations", "{}", token)
	if w.Code != 201 {
		t.Fatal(w.Code, w.Body.String())
	}
	var c store.Conversation
	if json.Unmarshal(w.Body.Bytes(), &c) != nil {
		t.Fatal("invalid conversation")
	}
	for _, field := range []string{`"context":{}`, `"plan":{}`, `"tenant_id":"other"`} {
		w = call("POST", "/api/conversations/"+c.ID+"/turns", `{"question":"本月 GMV","request_id":"00000001-0000-4000-8000-000000000001","expected_version":0,`+field+`}`, token)
		if w.Code != 400 {
			t.Fatal("accepted caller context", w.Code)
		}
	}
	input := `{"question":"本月 GMV","request_id":"00000001-0000-4000-8000-000000000001","expected_version":0}`
	w = call("POST", "/api/conversations/"+c.ID+"/turns", input, token)
	if w.Code != 202 {
		t.Fatal(w.Code, w.Body.String())
	}
	var task store.Task
	_ = json.Unmarshal(w.Body.Bytes(), &task)
	w = call("POST", "/api/conversations/"+c.ID+"/turns", input, token)
	if w.Code != 202 || !strings.Contains(w.Body.String(), task.ID) {
		t.Fatal("network replay failed")
	}
	for _, endpoint := range []struct{ method, path string }{{"GET", "/api/conversations/" + c.ID}, {"GET", "/api/tasks/" + task.ID}, {"GET", "/api/tasks/" + task.ID + "/events"}, {"POST", "/api/tasks/" + task.ID + "/stop"}} {
		w = call(endpoint.method, endpoint.path, "{}", otherToken)
		if w.Code != 404 {
			t.Fatal("owner bypass", endpoint.path, w.Code)
		}
	}
	claimed, err := db.Claim(ctx)
	if err != nil {
		t.Fatal(err)
	}
	server.execute(ctx, claimed)
	c, err = db.Conversation(ctx, one, c.ID)
	if err != nil || c.Version != 2 || c.Plan == nil || c.ActiveTaskID != nil {
		t.Fatal("trusted result did not advance context", err)
	}
	w = call("GET", "/api/conversations/"+c.ID, "{}", token)
	if w.Code != 200 || !strings.Contains(w.Body.String(), "PRIVATE_VALUE") {
		t.Fatal("authorized result missing")
	}
	changed.Store(true)
	for _, path := range []string{"/api/conversations/" + c.ID, "/api/tasks/" + task.ID, "/api/tasks/" + task.ID + "/events"} {
		w = call("GET", path, "{}", token)
		if w.Code == 200 || strings.Contains(w.Body.String(), "PRIVATE_VALUE") {
			t.Fatal("changed source exposed result", path, w.Code)
		}
	}
	legacy, err := db.CreateTask(ctx, one, store.Input{Question: "本月 GMV"})
	if err != nil {
		t.Fatal(err)
	}
	w = call("GET", "/api/tasks/"+legacy.ID, "{}", token)
	if w.Code != 200 {
		t.Fatal("legacy task depends on conversation binding")
	}
	changed.Store(false)
	if _, err = db.Pool.Exec(ctx, `UPDATE users SET allowed_metrics=ARRAY['orders'] WHERE id=$1`, one.ID); err != nil {
		t.Fatal(err)
	}
	w = call("GET", "/api/conversations/"+c.ID, "{}", token)
	if w.Code != 403 || strings.Contains(w.Body.String(), "PRIVATE_VALUE") {
		t.Fatal("revocation leaked context")
	}
}
