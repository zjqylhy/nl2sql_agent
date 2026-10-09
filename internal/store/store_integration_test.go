package store

import (
	"context"
	"encoding/json"
	"errors"
	"net/url"
	"os"
	"sync"
	"testing"
)

func integrationStore(t *testing.T) *Store {
	t.Helper()
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("TEST_DATABASE_URL not configured")
	}
	ctx := context.Background()
	admin, err := Open(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	schema := "test_" + ID()
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
	s, err := Open(ctx, parsed.String())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		s.Pool.Close()
		_, _ = admin.Pool.Exec(ctx, "DROP SCHEMA "+schema+" CASCADE")
		admin.Pool.Close()
	})
	if err = s.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	return s
}
func testUser(t *testing.T, s *Store, tenant, username string) User {
	t.Helper()
	_, err := s.Pool.Exec(context.Background(), `INSERT INTO tenants VALUES($1,$1) ON CONFLICT DO NOTHING`, tenant)
	if err != nil {
		t.Fatal(err)
	}
	u, err := s.AddUser(context.Background(), tenant, username, "test-password-123", "ops", Metrics)
	if err != nil {
		t.Fatal(err)
	}
	return u
}
func TestMigrationIdempotenceAndChecksum(t *testing.T) {
	s := integrationStore(t)
	ctx := context.Background()
	if err := s.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	var count int
	if err := s.Pool.QueryRow(ctx, "SELECT COUNT(*) FROM schema_migrations").Scan(&count); err != nil || count != 1 {
		t.Fatal("migration applied more than once", err)
	}
	if _, err := s.Pool.Exec(ctx, "UPDATE schema_migrations SET checksum='changed'"); err != nil {
		t.Fatal(err)
	}
	if err := s.Migrate(ctx); err == nil {
		t.Fatal("changed historical migration was accepted")
	}
}
func TestTenantIsolationAndSessionRevocation(t *testing.T) {
	s := integrationStore(t)
	ctx := context.Background()
	a := testUser(t, s, "alpha", "operator")
	b := testUser(t, s, "beta", "operator")
	task, err := s.CreateTask(ctx, a, Input{Question: "本月 GMV"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = s.Task(ctx, b, task.ID); !errors.Is(err, ErrMissing) {
		t.Fatalf("cross-tenant task readable: %v", err)
	}
	b.Role = "admin"
	if _, err = s.Task(ctx, b, task.ID); !errors.Is(err, ErrMissing) {
		t.Fatal("cross-tenant administrator bypassed isolation")
	}
	events, err := s.Events(ctx, task.ID, 0)
	if err != nil || len(events) != 1 || events[0].Kind != "accepted" {
		t.Fatal("task/event transaction invalid", err)
	}
	token, err := s.NewSession(ctx, a)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = s.SessionUser(ctx, token); err != nil {
		t.Fatal(err)
	}
	_, err = s.Pool.Exec(ctx, `UPDATE users SET enabled=FALSE WHERE id=$1`, a.ID)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = s.SessionUser(ctx, token); err == nil {
		t.Fatal("disabled user retained session")
	}
}
func TestClaimStopAndStaleCompletion(t *testing.T) {
	s := integrationStore(t)
	ctx := context.Background()
	u := testUser(t, s, "alpha", "operator")
	task, err := s.CreateTask(ctx, u, Input{Question: "本月 GMV"})
	if err != nil {
		t.Fatal(err)
	}
	first, err := s.Claim(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = s.Claim(ctx); !errors.Is(err, ErrMissing) {
		t.Fatal("task was delivered concurrently")
	}
	if err = s.Stop(ctx, u, task.ID); err != nil {
		t.Fatal(err)
	}
	if err = s.Finish(ctx, first, "completed", json.RawMessage(`{"metric":"gmv","value":1}`), ""); err != nil {
		t.Fatal(err)
	}
	final, err := s.Task(ctx, u, task.ID)
	if err != nil || final.Status != "canceled" || len(final.Result) != 0 {
		t.Fatal("stale result overwrote canceled state")
	}
}
func TestPerUserConcurrencyAndRecovery(t *testing.T) {
	s := integrationStore(t)
	ctx := context.Background()
	u := testUser(t, s, "alpha", "operator")
	_, err := s.CreateTask(ctx, u, Input{Question: "one"})
	if err != nil {
		t.Fatal(err)
	}
	_, err = s.CreateTask(ctx, u, Input{Question: "two"})
	if err != nil {
		t.Fatal(err)
	}
	first, err := s.Claim(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = s.Claim(ctx); !errors.Is(err, ErrMissing) {
		t.Fatal("same user received parallel tasks")
	}
	_, err = s.Pool.Exec(ctx, `UPDATE tasks SET lease_until=NOW()-INTERVAL '1 second' WHERE id=$1`, first.ID)
	if err != nil {
		t.Fatal(err)
	}
	second, err := s.Claim(ctx)
	if err != nil || second.ID != first.ID || second.Version <= first.Version {
		t.Fatal("expired lease was not recovered", err)
	}
	if err = s.Finish(ctx, first, "completed", json.RawMessage(`{"metric":"gmv","old":true}`), ""); err != nil {
		t.Fatal(err)
	}
	got, err := s.Task(ctx, u, first.ID)
	if err != nil || got.Status != "querying" || len(got.Result) > 0 {
		t.Fatal("old worker overwrote recovered execution")
	}
}
func TestConcurrentClaimIsSingleDelivery(t *testing.T) {
	s := integrationStore(t)
	ctx := context.Background()
	u := testUser(t, s, "alpha", "operator")
	_, err := s.CreateTask(ctx, u, Input{Question: "one"})
	if err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	var successes int
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, err := s.Claim(ctx)
			if err == nil {
				mu.Lock()
				successes++
				mu.Unlock()
			} else if !errors.Is(err, ErrMissing) {
				t.Error(err)
			}
		}()
	}
	wg.Wait()
	if successes != 1 {
		t.Fatalf("expected one delivery, got %d", successes)
	}
}
func TestRetryBudgetAndEventContinuation(t *testing.T) {
	s := integrationStore(t)
	ctx := context.Background()
	u := testUser(t, s, "alpha", "operator")
	task, err := s.CreateTask(ctx, u, Input{Question: "one"})
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 3; i++ {
		attempt, err := s.Claim(ctx)
		if err != nil {
			t.Fatal(err)
		}
		if err = s.Retry(ctx, attempt, "transient"); err != nil {
			t.Fatal(err)
		}
	}
	got, err := s.Task(ctx, u, task.ID)
	if err != nil || got.Status != "failed" || got.Attempts != 3 {
		t.Fatal("retry budget not enforced")
	}
	all, err := s.Events(ctx, task.ID, 0)
	if err != nil {
		t.Fatal(err)
	}
	tail, err := s.Events(ctx, task.ID, all[2].Seq)
	if err != nil {
		t.Fatal(err)
	}
	if len(tail) != len(all)-3 || tail[0].Seq != all[3].Seq {
		t.Fatal("event continuation duplicated or lost data")
	}
}
