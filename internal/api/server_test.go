package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"nl2sql_agent/internal/store"
	"strings"
	"testing"
)

func TestOriginBoundary(t *testing.T) {
	for _, origin := range []string{"", "https://evil.example", "https://app.example.evil", "http://app.example", "https://user@app.example", "https://app.example/path", "https://app.example?x=1"} {
		if sameOrigin(origin, "https://app.example") {
			t.Fatalf("accepted %q", origin)
		}
	}
	if !sameOrigin("https://app.example", "https://app.example") {
		t.Fatal("rejected same origin")
	}
}
func TestUnknownTenantFieldsRejected(t *testing.T) {
	r := httptest.NewRequest(http.MethodPost, "/api/tasks", strings.NewReader(`{"question":"本月 GMV","tenant_id":"victim"}`))
	w := httptest.NewRecorder()
	var in store.Input
	if decode(w, r, &in) || w.Code != 400 {
		t.Fatal("accepted caller-supplied tenant")
	}
}
func TestRevokedMetricHidesHistoricalResult(t *testing.T) {
	task := store.Task{Input: store.Input{Question: "本月 GMV"}, Result: json.RawMessage(`{"metric":"gmv","rows":[{"value":123}]}`)}
	if allowed(store.User{AllowedMetrics: []string{"orders"}}, task) {
		t.Fatal("exposed revoked metric")
	}
	if !allowed(store.User{AllowedMetrics: []string{"gmv"}}, task) {
		t.Fatal("hid allowed result")
	}
}
func TestLoginLimit(t *testing.T) {
	var limiter loginLimiter
	for i := 0; i < 10; i++ {
		if !limiter.Allow("one") {
			t.Fatal("early throttle")
		}
	}
	if limiter.Allow("one") {
		t.Fatal("missing throttle")
	}
	if !limiter.Allow("two") {
		t.Fatal("different IP throttled")
	}
}
func TestRequestBodyLimits(t *testing.T) {
	for _, body := range []string{`{"question":"ok"}{}`, strings.Repeat("x", 17000)} {
		r := httptest.NewRequest("POST", "/", strings.NewReader(body))
		w := httptest.NewRecorder()
		var in store.Input
		if decode(w, r, &in) {
			t.Fatal("accepted invalid request")
		}
	}
}
