package api

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"nl2sql_agent/internal/store"
	"os"
	"strings"
	"testing"
)

// 使用独立 PostgreSQL schema 验证撤权后的所有公开读取入口，结束后清理。
func TestSemanticRevocationAcrossHistoryAndEvents(t *testing.T) {
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
	parsed, err := url.Parse(dsn)
	if err != nil {
		t.Fatal(err)
	}
	query := parsed.Query()
	query.Set("search_path", schema)
	parsed.RawQuery = query.Encode()
	db, err := store.Open(ctx, parsed.String())
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
	if _, err = db.Pool.Exec(ctx, `INSERT INTO tenants VALUES('alpha','test')`); err != nil {
		t.Fatal(err)
	}
	u, err := db.AddUser(ctx, "alpha", "test-user", "test-password-123", "ops", []string{"gmv", "review_score"})
	if err != nil {
		t.Fatal(err)
	}
	token, err := db.NewSession(ctx, u)
	if err != nil {
		t.Fatal(err)
	}
	original, err := db.CreateTask(ctx, u, store.Input{Question: "本月 GMV 和平均评分"})
	if err != nil {
		t.Fatal(err)
	}
	claimed, err := db.Claim(ctx)
	if err != nil {
		t.Fatal(err)
	}
	body := json.RawMessage(`{"result_version":2,"metrics":[{"id":"gmv"},{"id":"review_score"}],"plan":{"metrics":["gmv","review_score"]},"rows":[{"value":123}]}`)
	if err = db.Finish(ctx, claimed, "completed", body, ""); err != nil {
		t.Fatal(err)
	}
	server := New(db, Config{Origin: "http://app.example"}).Handler()
	call := func(method, path string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, strings.NewReader("{}"))
		r.AddCookie(&http.Cookie{Name: "session", Value: token})
		r.Header.Set("Origin", "http://app.example")
		w := httptest.NewRecorder()
		server.ServeHTTP(w, r)
		return w
	}
	if w := call("GET", "/api/tasks/"+original.ID); w.Code != 200 {
		t.Fatalf("authorized history status %d", w.Code)
	}
	if _, err = db.Pool.Exec(ctx, `UPDATE users SET allowed_metrics=ARRAY['gmv'] WHERE id=$1`, u.ID); err != nil {
		t.Fatal(err)
	}
	for _, endpoint := range []struct{ method, path string }{{"GET", "/api/tasks/" + original.ID}, {"GET", "/api/tasks/" + original.ID + "/events"}, {"POST", "/api/tasks/" + original.ID + "/stop"}} {
		if w := call(endpoint.method, endpoint.path); w.Code != 404 || strings.Contains(w.Body.String(), "123") {
			t.Fatalf("revoked response: %d %s", w.Code, w.Body.String())
		}
	}
	if w := call("GET", "/api/tasks"); w.Code != 200 || strings.TrimSpace(w.Body.String()) != "[]" {
		t.Fatalf("revoked list: %d %s", w.Code, w.Body.String())
	}
}
