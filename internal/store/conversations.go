package store

import (
	"context"
	"encoding/json"
	"errors"
	"slices"
	"time"

	"github.com/jackc/pgx/v5"
)

var ErrConflict = errors.New("conversation changed or busy")
var ErrPermission = errors.New("conversation permissions changed")
var ErrTurnLimit = errors.New("conversation turn limit reached")

type ContextMetadata struct {
	Binding      string `json:"binding"`
	AnalysisDate string `json:"analysis_date"`
	CatalogHash  string `json:"catalog_hash"`
}

type Conversation struct {
	Title    string `json:"title"`
	ID       string `json:"id"`
	TenantID string `json:"-"`
	UserID   string `json:"-"`
	Role     string `json:"-"`
	ContextMetadata
	Version      int             `json:"version"`
	TurnCount    int             `json:"turn_count"`
	Plan         json.RawMessage `json:"plan"`
	Pending      json.RawMessage `json:"pending"`
	ActiveTaskID *string         `json:"active_task_id"`
	CreatedAt    time.Time       `json:"created_at"`
	UpdatedAt    time.Time       `json:"updated_at"`
}

const conversationColumns = `id,tenant_id,user_id,role,binding,analysis_date::text,catalog_hash,version,turn_count,plan,pending,active_task_id,created_at,updated_at`

func scanConversation(row pgx.Row) (Conversation, error) {
	var c Conversation
	err := row.Scan(&c.ID, &c.TenantID, &c.UserID, &c.Role, &c.Binding, &c.AnalysisDate, &c.CatalogHash, &c.Version, &c.TurnCount, &c.Plan, &c.Pending, &c.ActiveTaskID, &c.CreatedAt, &c.UpdatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		err = ErrMissing
	}
	return c, err
}

// 仅递归读取协议的指标字段，草稿操作与初始草稿同样受当前权限约束。
func MetricsAllowed(raw json.RawMessage, allowed []string) bool {
	if len(raw) == 0 || string(raw) == "null" {
		return true
	}
	var value any
	if json.Unmarshal(raw, &value) != nil {
		return false
	}
	var visit func(any) bool
	visit = func(v any) bool {
		switch item := v.(type) {
		case map[string]any:
			if item["op"] == "set_metrics" {
				values, ok := item["value"].([]any)
				if !ok {
					return false
				}
				for _, id := range values {
					key, ok := id.(string)
					if !ok || !slices.Contains(Metrics, key) || !slices.Contains(allowed, key) {
						return false
					}
				}
			}
			for key, next := range item {
				if key == "metrics" {
					values, ok := next.([]any)
					if !ok {
						return false
					}
					for _, id := range values {
						key, ok := id.(string)
						if metadata, isMetadata := id.(map[string]any); isMetadata {
							key, ok = metadata["id"].(string)
						}
						if !ok || !slices.Contains(Metrics, key) || !slices.Contains(allowed, key) {
							return false
						}
					}
				} else if !visit(next) {
					return false
				}
			}
		case []any:
			for _, next := range item {
				if !visit(next) {
					return false
				}
			}
		}
		return true
	}
	return visit(value)
}

func ConversationAllowed(u User, c Conversation) bool {
	return c.TenantID == u.TenantID && c.UserID == u.ID && c.Role == u.Role && MetricsAllowed(c.Plan, u.AllowedMetrics) && MetricsAllowed(c.Pending, u.AllowedMetrics)
}

func (s *Store) NewConversation(ctx context.Context, u User, meta ContextMetadata) (Conversation, error) {
	id := ID()
	_, err := s.Pool.Exec(ctx, `INSERT INTO conversations(id,tenant_id,user_id,role,binding,analysis_date,catalog_hash) VALUES($1,$2,$3,$4,$5,$6,$7)`, id, u.TenantID, u.ID, u.Role, meta.Binding, meta.AnalysisDate, meta.CatalogHash)
	if err != nil {
		return Conversation{}, err
	}
	return s.Conversation(ctx, u, id)
}

func (s *Store) Conversation(ctx context.Context, u User, id string) (Conversation, error) {
	return scanConversation(s.Pool.QueryRow(ctx, `SELECT `+conversationColumns+` FROM conversations WHERE id=$1 AND tenant_id=$2 AND user_id=$3`, id, u.TenantID, u.ID))
}

func (s *Store) Conversations(ctx context.Context, u User, page int) ([]Conversation, error) {
	rows, err := s.Pool.Query(ctx, `SELECT `+conversationColumns+` FROM conversations WHERE tenant_id=$1 AND user_id=$2 ORDER BY updated_at DESC,id LIMIT 20 OFFSET $3`, u.TenantID, u.ID, page*20)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	values := []Conversation{}
	for rows.Next() {
		c, err := scanConversation(rows)
		if err != nil {
			return nil, err
		}
		values = append(values, c)
	}
	return values, rows.Err()
}

func (s *Store) ConversationTasks(ctx context.Context, u User, id string) ([]Task, error) {
	rows, err := s.Pool.Query(ctx, `SELECT `+taskColumns+` FROM tasks t WHERE t.conversation_id=$1 AND t.tenant_id=$2 AND t.user_id=$3 ORDER BY t.conversation_turn`, id, u.TenantID, u.ID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	values := []Task{}
	for rows.Next() {
		task, err := scanTask(rows)
		if err != nil {
			return nil, err
		}
		values = append(values, task)
	}
	return values, rows.Err()
}

func (s *Store) CreateTurn(ctx context.Context, u User, id, requestID string, version int, input Input, meta ContextMetadata) (Task, error) {
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return Task{}, err
	}
	defer tx.Rollback(ctx)
	// 接纳与终态统一先锁会话；重复请求先查重，允许网络重发旧版本。
	c, err := scanConversation(tx.QueryRow(ctx, `SELECT `+conversationColumns+` FROM conversations WHERE id=$1 AND tenant_id=$2 AND user_id=$3 FOR UPDATE`, id, u.TenantID, u.ID))
	if err != nil {
		return Task{}, err
	}
	if !ConversationAllowed(u, c) {
		return Task{}, ErrPermission
	}
	if c.Binding != meta.Binding {
		return Task{}, ErrConflict
	}
	old, err := scanTask(tx.QueryRow(ctx, `SELECT `+taskColumns+` FROM tasks t WHERE t.conversation_id=$1 AND t.request_id=$2 AND t.tenant_id=$3`, id, requestID, u.TenantID))
	if err == nil {
		if old.Input != input {
			return Task{}, ErrConflict
		}
		return old, nil
	}
	if !errors.Is(err, ErrMissing) {
		return Task{}, err
	}
	if c.Version != version || c.ActiveTaskID != nil {
		return Task{}, ErrConflict
	}
	if c.TurnCount >= 30 {
		return Task{}, ErrTurnLimit
	}
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(710091)`); err != nil {
		return Task{}, err
	}
	var count int
	if err = tx.QueryRow(ctx, `SELECT COUNT(*) FROM tasks WHERE status='pending'`).Scan(&count); err != nil {
		return Task{}, err
	}
	if count >= 100 {
		return Task{}, ErrQueueFull
	}
	c.Version++
	c.TurnCount++
	taskID := ID()
	contextBody, _ := json.Marshal(map[string]any{"binding": c.Binding, "version": c.Version, "plan": c.Plan, "pending": c.Pending})
	inputBody, _ := json.Marshal(input)
	_, err = tx.Exec(ctx, `INSERT INTO tasks(id,tenant_id,user_id,input,conversation_id,conversation_turn,request_id,conversation_context,admission_version) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)`, taskID, u.TenantID, u.ID, inputBody, id, c.TurnCount, requestID, contextBody, c.Version)
	if err != nil {
		return Task{}, err
	}
	if _, err = tx.Exec(ctx, `UPDATE conversations SET version=$2,turn_count=$3,active_task_id=$4,updated_at=NOW() WHERE id=$1`, id, c.Version, c.TurnCount, taskID); err != nil {
		return Task{}, err
	}
	if _, err = tx.Exec(ctx, `INSERT INTO task_outbox(task_id) VALUES($1)`, taskID); err != nil {
		return Task{}, err
	}
	if err = event(ctx, tx, taskID, "accepted", map[string]string{"task_id": taskID}); err != nil {
		return Task{}, err
	}
	if err = tx.Commit(ctx); err != nil {
		return Task{}, err
	}
	return s.Task(ctx, u, taskID)
}

func (s *Store) DiscardPending(ctx context.Context, u User, id string, version int) error {
	tag, err := s.Pool.Exec(ctx, `UPDATE conversations SET pending=NULL,version=version+1,updated_at=NOW() WHERE id=$1 AND tenant_id=$2 AND user_id=$3 AND role=$4 AND version=$5 AND active_task_id IS NULL`, id, u.TenantID, u.ID, u.Role, version)
	if err != nil {
		return err
	}
	if tag.RowsAffected() == 0 {
		return ErrConflict
	}
	return nil
}

func lockTaskConversation(ctx context.Context, tx pgx.Tx, t Task) error {
	if t.ConversationID == "" {
		return nil
	}
	var id string
	return tx.QueryRow(ctx, `SELECT id FROM conversations WHERE id=$1 AND tenant_id=$2 FOR UPDATE`, t.ConversationID, t.TenantID).Scan(&id)
}

func completeConversation(ctx context.Context, tx pgx.Tx, t Task, status string, result json.RawMessage) error {
	if t.ConversationID == "" {
		return nil
	}
	var plan, pending json.RawMessage
	if status == "completed" || status == "needs_clarification" {
		var body struct {
			Plan    json.RawMessage `json:"plan"`
			Binding string          `json:"context_binding"`
			Turn    struct {
				Pending json.RawMessage `json:"pending"`
			} `json:"multiturn"`
		}
		if json.Unmarshal(result, &body) != nil {
			return ErrConflict
		}
		var snapshot struct {
			Binding string `json:"binding"`
		}
		if json.Unmarshal(t.Context, &snapshot) != nil || body.Binding != snapshot.Binding {
			return ErrConflict
		}
		if status == "completed" {
			if len(body.Plan) == 0 || string(body.Plan) == "null" {
				return ErrConflict
			}
			plan = body.Plan
		}
		pending = body.Turn.Pending
	}
	tag, err := tx.Exec(ctx, `UPDATE conversations SET plan=CASE WHEN $4='completed' THEN $5 ELSE plan END,pending=CASE WHEN $4='completed' THEN NULL WHEN $4='needs_clarification' AND $6::jsonb IS NOT NULL THEN $6 ELSE pending END,active_task_id=NULL,version=version+1,updated_at=NOW() WHERE id=$1 AND active_task_id=$2 AND version=$3`, t.ConversationID, t.ID, t.AdmissionVersion, status, plan, pending)
	if err != nil {
		return err
	}
	if tag.RowsAffected() != 1 {
		return ErrConflict
	}
	return nil
}
