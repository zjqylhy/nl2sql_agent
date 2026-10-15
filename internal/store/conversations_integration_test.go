package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
)

func conversationMeta() ContextMetadata {
	return ContextMetadata{Binding: strings.Repeat("a", 64), CatalogHash: strings.Repeat("b", 64), AnalysisDate: "2018-08-31"}
}
func requestID(n int) string { return fmt.Sprintf("%08x-0000-4000-8000-%012x", n, n) }
func completedPlan(meta ContextMetadata, metric string) json.RawMessage {
	body, _ := json.Marshal(map[string]any{"status": "completed", "result_version": 2, "context_binding": meta.Binding, "metrics": []map[string]string{{"id": metric}}, "plan": map[string]any{"version": 1, "metrics": []string{metric}, "dimensions": []string{"state"}, "time_range": map[string]string{"start": "2018-08-01", "end_exclusive": "2018-09-01"}, "filters": []any{}, "order_by": []any{}, "top_n": nil}})
	return body
}

func TestConversationIdempotencyOwnershipAndConcurrentAdmission(t *testing.T) {
	s := integrationStore(t)
	ctx := context.Background()
	u := testUser(t, s, "alpha", "one")
	other := testUser(t, s, "alpha", "two")
	other.Role = "admin"
	c, err := s.NewConversation(ctx, u, conversationMeta())
	if err != nil {
		t.Fatal(err)
	}
	var group sync.WaitGroup
	responses := make(chan Task, 2)
	failures := make(chan error, 2)
	for range 2 {
		group.Add(1)
		go func() {
			defer group.Done()
			task, err := s.CreateTurn(ctx, u, c.ID, requestID(1), 0, Input{Question: "本月 GMV"}, c.ContextMetadata)
			if err != nil {
				failures <- err
			} else {
				responses <- task
			}
		}()
	}
	group.Wait()
	close(responses)
	close(failures)
	for err := range failures {
		t.Fatal(err)
	}
	var first string
	count := 0
	for task := range responses {
		count++
		if first == "" {
			first = task.ID
		}
		if task.ID != first {
			t.Fatal("duplicate request created two tasks")
		}
	}
	if count != 2 {
		t.Fatal("lost idempotent response")
	}
	c, _ = s.Conversation(ctx, u, c.ID)
	if c.Version != 1 || c.TurnCount != 1 || c.ActiveTaskID == nil {
		t.Fatal("duplicate advanced conversation")
	}
	if _, err = s.CreateTurn(ctx, u, c.ID, requestID(1), 0, Input{Question: "上月 GMV"}, c.ContextMetadata); !errors.Is(err, ErrConflict) {
		t.Fatal("reused request ID changed input", err)
	}
	if _, err = s.CreateTurn(ctx, u, c.ID, requestID(2), 1, Input{Question: "改成按州"}, c.ContextMetadata); !errors.Is(err, ErrConflict) {
		t.Fatal("admitted parallel turn", err)
	}
	if _, err = s.Task(ctx, other, first); !errors.Is(err, ErrMissing) {
		t.Fatal("same-tenant admin bypassed conversation ownership", err)
	}
	if _, err = s.Conversation(ctx, other, c.ID); !errors.Is(err, ErrMissing) {
		t.Fatal("other user read conversation", err)
	}
	_, err = s.Pool.Exec(ctx, `INSERT INTO tasks(id,tenant_id,user_id,input,conversation_id,conversation_turn,request_id,conversation_context,admission_version) VALUES($1,$2,$3,'{}',$4,2,$5,'{}',2)`, ID(), u.TenantID, other.ID, c.ID, requestID(3))
	if err == nil {
		t.Fatal("database allowed cross-owner conversation task")
	}
	_, err = s.Pool.Exec(ctx, `INSERT INTO tasks(id,tenant_id,user_id,input,conversation_id) VALUES($1,$2,$3,'{}',$4)`, ID(), u.TenantID, u.ID, c.ID)
	if err == nil {
		t.Fatal("database allowed partial conversation columns")
	}
}

func TestConversationRetryStopAndStaleCompletionAreFenced(t *testing.T) {
	s := integrationStore(t)
	ctx := context.Background()
	u := testUser(t, s, "alpha", "one")
	c, err := s.NewConversation(ctx, u, conversationMeta())
	if err != nil {
		t.Fatal(err)
	}
	_, err = s.CreateTurn(ctx, u, c.ID, requestID(1), 0, Input{Question: "本月 GMV"}, c.ContextMetadata)
	if err != nil {
		t.Fatal(err)
	}
	first, err := s.Claim(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if err = s.Finish(ctx, first, "completed", completedPlan(c.ContextMetadata, "gmv"), ""); err != nil {
		t.Fatal(err)
	}
	c, _ = s.Conversation(ctx, u, c.ID)
	original := string(c.Plan)
	if c.Version != 2 || c.ActiveTaskID != nil || c.Plan == nil {
		t.Fatal("completion failed to commit context")
	}
	if err = s.Finish(ctx, first, "completed", completedPlan(c.ContextMetadata, "orders"), ""); err != nil {
		t.Fatal(err)
	}
	c, _ = s.Conversation(ctx, u, c.ID)
	if c.Version != 2 || string(c.Plan) != original {
		t.Fatal("duplicate completion changed context")
	}
	_, err = s.CreateTurn(ctx, u, c.ID, requestID(2), 2, Input{Question: "改成订单量"}, c.ContextMetadata)
	if err != nil {
		t.Fatal(err)
	}
	old, _ := s.Claim(ctx)
	if err = s.Retry(ctx, old, "transient"); err != nil {
		t.Fatal(err)
	}
	latest, err := s.Claim(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if latest.Version != old.Version+1 {
		t.Fatal("attempt did not advance")
	}
	if err = s.Finish(ctx, old, "completed", completedPlan(c.ContextMetadata, "orders"), ""); err != nil {
		t.Fatal(err)
	}
	c, _ = s.Conversation(ctx, u, c.ID)
	if c.Version != 3 || string(c.Plan) != original {
		t.Fatal("stale attempt wrote context")
	}
	if err = s.Stop(ctx, u, latest.ID); err != nil {
		t.Fatal(err)
	}
	if err = s.Finish(ctx, latest, "completed", completedPlan(c.ContextMetadata, "orders"), ""); err != nil {
		t.Fatal(err)
	}
	c, _ = s.Conversation(ctx, u, c.ID)
	if c.Version != 4 || c.ActiveTaskID != nil || string(c.Plan) != original {
		t.Fatal("stop or late result changed plan")
	}
}

func TestConversationClarificationAndBudgetExhaustionReleaseSlot(t *testing.T) {
	s := integrationStore(t)
	ctx := context.Background()
	u := testUser(t, s, "alpha", "one")
	c, _ := s.NewConversation(ctx, u, conversationMeta())
	_, err := s.CreateTurn(ctx, u, c.ID, requestID(1), 0, Input{Question: "GMV"}, c.ContextMetadata)
	if err != nil {
		t.Fatal(err)
	}
	first, err := s.Claim(ctx)
	if err != nil {
		t.Fatal(err)
	}
	body, _ := json.Marshal(map[string]any{"status": "needs_clarification", "context_binding": c.Binding, "multiturn": map[string]any{"pending": map[string]any{"missing": []string{"time_range"}, "last_task_id": first.ID, "initial": map[string]any{"metrics": []string{"gmv"}}}}})
	if err = s.Finish(ctx, first, "needs_clarification", body, ""); err != nil {
		t.Fatal(err)
	}
	c, _ = s.Conversation(ctx, u, c.ID)
	if c.Plan != nil || c.Pending == nil || c.ActiveTaskID != nil || c.Version != 2 {
		t.Fatal("clarification wrote active plan or blocked slot")
	}
	if err = s.DiscardPending(ctx, u, c.ID, 1); !errors.Is(err, ErrConflict) {
		t.Fatal("discard accepted stale version")
	}
	if err = s.DiscardPending(ctx, u, c.ID, 2); err != nil {
		t.Fatal(err)
	}
	_, err = s.CreateTurn(ctx, u, c.ID, requestID(2), 3, Input{Question: "本月 GMV"}, c.ContextMetadata)
	if err != nil {
		t.Fatal(err)
	}
	last, err := s.Claim(ctx)
	if err != nil {
		t.Fatal(err)
	}
	_, err = s.Pool.Exec(ctx, `UPDATE tasks SET attempts=3,lease_until=NOW()-INTERVAL '1 second' WHERE id=$1`, last.ID)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = s.Claim(ctx); !errors.Is(err, ErrMissing) {
		t.Fatal("exhausted task reclaimed", err)
	}
	c, _ = s.Conversation(ctx, u, c.ID)
	if c.ActiveTaskID != nil || c.Version != 5 || c.Plan != nil {
		t.Fatal("exhaustion left active slot or changed context")
	}
}

func TestConversationRevocationBeforeCommitPreservesPlan(t *testing.T) {
	s := integrationStore(t)
	ctx := context.Background()
	u := testUser(t, s, "alpha", "one")
	c, _ := s.NewConversation(ctx, u, conversationMeta())
	_, err := s.CreateTurn(ctx, u, c.ID, requestID(1), 0, Input{Question: "本月 GMV"}, c.ContextMetadata)
	if err != nil {
		t.Fatal(err)
	}
	task, err := s.Claim(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = s.Pool.Exec(ctx, `UPDATE users SET allowed_metrics=ARRAY['orders'] WHERE id=$1`, u.ID); err != nil {
		t.Fatal(err)
	}
	if err = s.Finish(ctx, task, "completed", completedPlan(c.ContextMetadata, "gmv"), ""); err != nil {
		t.Fatal(err)
	}
	c, _ = s.Conversation(ctx, u, c.ID)
	if c.Plan != nil || c.ActiveTaskID != nil {
		t.Fatal("revocation committed model result")
	}
	task, _ = s.Task(ctx, u, task.ID)
	if task.Status != "failed" || task.Result != nil {
		t.Fatal("revoked task kept successful data")
	}
}
