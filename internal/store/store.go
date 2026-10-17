package store

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"golang.org/x/crypto/bcrypt"
)

//go:embed migrations/*.sql
var migrations embed.FS

var ErrMissing = errors.New("resource not found")
var ErrQueueFull = errors.New("queue is full")
var Metrics = []string{"gmv", "orders", "aov", "cancellation_rate", "delivery_days", "review_score"}

type User struct {
	ID             string   `json:"id"`
	TenantID       string   `json:"tenant_id"`
	Username       string   `json:"username"`
	Role           string   `json:"role"`
	AllowedMetrics []string `json:"allowed_metrics"`
	Hash           []byte   `json:"-"`
}

type Input struct {
	Question string `json:"question"`
	Metric   string `json:"metric,omitempty"`
	Period   string `json:"period,omitempty"`
	GroupBy  string `json:"group_by,omitempty"`
	Start    string `json:"start,omitempty"`
	End      string `json:"end,omitempty"`
}

type Task struct {
	ID               string          `json:"id"`
	TenantID         string          `json:"tenant_id"`
	UserID           string          `json:"user_id"`
	Input            Input           `json:"input"`
	Status           string          `json:"status"`
	Result           json.RawMessage `json:"result"`
	Error            string          `json:"error,omitempty"`
	Attempts         int             `json:"attempts"`
	Version          int             `json:"execution_version"`
	CreatedAt        time.Time       `json:"created_at"`
	StartedAt        *time.Time      `json:"-"`
	ConversationID   string          `json:"conversation_id,omitempty"`
	ConversationTurn int             `json:"conversation_turn,omitempty"`
	Context          json.RawMessage `json:"-"`
	AdmissionVersion int             `json:"-"`
	RequestID        string          `json:"request_id,omitempty"`
	Kind             string          `json:"kind"`
	AnalysisMetrics  []string        `json:"-"`
}

type Event struct {
	Seq  int             `json:"seq"`
	Kind string          `json:"kind"`
	Data json.RawMessage `json:"data"`
}

type Store struct{ Pool *pgxpool.Pool }

func ID() string {
	b := make([]byte, 24)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return hex.EncodeToString(b)
}

func TokenHash(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}

func Open(ctx context.Context, dsn string) (*Store, error) {
	config, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, err
	}
	config.ConnConfig.ConnectTimeout = 5 * time.Second
	config.ConnConfig.RuntimeParams["statement_timeout"] = "5000"
	p, err := pgxpool.NewWithConfig(ctx, config)
	if err != nil {
		return nil, err
	}
	if err = p.Ping(ctx); err != nil {
		p.Close()
		return nil, err
	}
	return &Store{Pool: p}, nil
}

func (s *Store) Migrate(ctx context.Context) error {
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	// 迁移事务串行执行并记录校验值，防止多实例部署重复迁移或改写旧版本。
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(710090); CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())`); err != nil {
		return err
	}
	names, err := fs.Glob(migrations, "migrations/*.sql")
	if err != nil {
		return err
	}
	for _, name := range names {
		sql, readErr := migrations.ReadFile(name)
		if readErr != nil {
			return readErr
		}
		sum := sha256.Sum256(sql)
		checksum := hex.EncodeToString(sum[:])
		var recorded string
		err = tx.QueryRow(ctx, `SELECT checksum FROM schema_migrations WHERE version=$1`, name).Scan(&recorded)
		if err == nil {
			if recorded != checksum {
				return fmt.Errorf("migration checksum changed: %s", name)
			}
			continue
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		if _, err = tx.Exec(ctx, string(sql)); err != nil {
			return err
		}
		if _, err = tx.Exec(ctx, `INSERT INTO schema_migrations(version,checksum) VALUES($1,$2)`, name, checksum); err != nil {
			return err
		}
	}
	return tx.Commit(ctx)
}

func (s *Store) AddUser(ctx context.Context, tenant, username, password, role string, metrics []string) (User, error) {
	hash, err := bcrypt.GenerateFromPassword([]byte(password), bcrypt.DefaultCost)
	if err != nil {
		return User{}, err
	}
	u := User{ID: ID(), TenantID: tenant, Username: username, Role: role, AllowedMetrics: metrics}
	_, err = s.Pool.Exec(ctx, `INSERT INTO users(id,tenant_id,username,password_hash,role,allowed_metrics) VALUES($1,$2,$3,$4,$5,$6)`, u.ID, tenant, username, hash, role, metrics)
	return u, err
}

func (s *Store) LoginUser(ctx context.Context, tenant, username string) (User, error) {
	var u User
	err := s.Pool.QueryRow(ctx, `SELECT id,tenant_id,username,role,allowed_metrics,password_hash FROM users WHERE tenant_id=$1 AND username=$2 AND enabled`, tenant, username).Scan(&u.ID, &u.TenantID, &u.Username, &u.Role, &u.AllowedMetrics, &u.Hash)
	return u, err
}

func (s *Store) SessionUser(ctx context.Context, token string) (User, error) {
	var u User
	err := s.Pool.QueryRow(ctx, `SELECT u.id,u.tenant_id,u.username,u.role,u.allowed_metrics FROM sessions s JOIN users u ON u.id=s.user_id AND u.tenant_id=s.tenant_id WHERE s.token_hash=$1 AND s.expires_at>NOW() AND u.enabled`, TokenHash(token)).Scan(&u.ID, &u.TenantID, &u.Username, &u.Role, &u.AllowedMetrics)
	return u, err
}

func (s *Store) NewSession(ctx context.Context, u User) (string, error) {
	token := ID()
	_, err := s.Pool.Exec(ctx, `INSERT INTO sessions VALUES($1,$2,$3,NOW()+INTERVAL '12 hours')`, TokenHash(token), u.TenantID, u.ID)
	return token, err
}

const taskColumns = `t.id,t.tenant_id,t.user_id,t.input,t.status,t.result,t.error,t.attempts,t.execution_version,t.created_at,t.started_at,COALESCE(t.conversation_id,''),COALESCE(t.conversation_turn,0),t.conversation_context,COALESCE(t.admission_version,0),COALESCE(t.request_id,''),t.kind,COALESCE((SELECT required_metrics FROM analysis_runs r WHERE r.id=t.id),'{}'::text[])`

func scanTask(row pgx.Row) (Task, error) {
	var t Task
	var raw []byte
	err := row.Scan(&t.ID, &t.TenantID, &t.UserID, &raw, &t.Status, &t.Result, &t.Error, &t.Attempts, &t.Version, &t.CreatedAt, &t.StartedAt, &t.ConversationID, &t.ConversationTurn, &t.Context, &t.AdmissionVersion, &t.RequestID, &t.Kind, &t.AnalysisMetrics)
	if err == nil {
		err = json.Unmarshal(raw, &t.Input)
	}
	if errors.Is(err, pgx.ErrNoRows) {
		err = ErrMissing
	}
	return t, err
}

func (s *Store) Task(ctx context.Context, u User, id string) (Task, error) {
	// 任务可见性包含租户和人员，管理员可读取本租户轨迹。
	return scanTask(s.Pool.QueryRow(ctx, `SELECT `+taskColumns+` FROM tasks t WHERE t.id=$1 AND t.tenant_id=$2 AND (t.user_id=$3 OR (t.kind='query' AND t.conversation_id IS NULL AND $4 IN ('admin','data')))`, id, u.TenantID, u.ID, u.Role))
}

func (s *Store) Tasks(ctx context.Context, u User) ([]Task, error) {
	rows, err := s.Pool.Query(ctx, `SELECT `+taskColumns+` FROM tasks t WHERE t.tenant_id=$1 AND (t.user_id=$2 OR (t.kind='query' AND t.conversation_id IS NULL AND $3 IN ('admin','data'))) ORDER BY t.created_at DESC LIMIT 100`, u.TenantID, u.ID, u.Role)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	list := []Task{}
	for rows.Next() {
		t, err := scanTask(rows)
		if err != nil {
			return nil, err
		}
		list = append(list, t)
	}
	return list, rows.Err()
}

func event(ctx context.Context, tx pgx.Tx, id, kind string, data any) error {
	var seq int
	if err := tx.QueryRow(ctx, `UPDATE tasks SET next_seq=next_seq+1 WHERE id=$1 RETURNING next_seq`, id).Scan(&seq); err != nil {
		return err
	}
	raw, err := json.Marshal(data)
	if err != nil {
		return err
	}
	_, err = tx.Exec(ctx, `INSERT INTO task_events(task_id,seq,kind,data) VALUES($1,$2,$3,$4)`, id, seq, kind, raw)
	return err
}

func (s *Store) CreateTask(ctx context.Context, u User, input Input) (Task, error) {
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return Task{}, err
	}
	defer tx.Rollback(ctx)
	// 同一事务内串行控制队列容量，避免多个 API 实例超卖名额。
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
	id := ID()
	raw, _ := json.Marshal(input)
	if _, err = tx.Exec(ctx, `INSERT INTO tasks(id,tenant_id,user_id,input) VALUES($1,$2,$3,$4)`, id, u.TenantID, u.ID, raw); err != nil {
		return Task{}, err
	}
	if _, err = tx.Exec(ctx, `INSERT INTO task_outbox(task_id) VALUES($1)`, id); err != nil {
		return Task{}, err
	}
	if err = event(ctx, tx, id, "accepted", map[string]string{"task_id": id}); err != nil {
		return Task{}, err
	}
	if err = tx.Commit(ctx); err != nil {
		return Task{}, err
	}
	return s.Task(ctx, u, id)
}

func (s *Store) Stop(ctx context.Context, u User, id string) error {
	t, err := s.Task(ctx, u, id)
	if err != nil {
		return err
	}
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if err = lockTaskConversation(ctx, tx, t); err != nil {
		return err
	}
	tag, err := tx.Exec(ctx, `UPDATE tasks SET status='canceled',execution_version=execution_version+1,lease_until=NULL,updated_at=NOW() WHERE id=$1 AND tenant_id=$2 AND status IN ('pending','querying','needs_clarification')`, t.ID, u.TenantID)
	if err != nil {
		return err
	}
	if tag.RowsAffected() > 0 {
		if t.Kind == "analysis" {
			if _, err = tx.Exec(ctx, `UPDATE analysis_runs SET state=jsonb_set(jsonb_set(state,'{phase}','"canceled"'),'{message}','"任务已停止"'),version=version+1,updated_at=NOW() WHERE id=$1`, t.ID); err != nil {
				return err
			}
			if _, err = tx.Exec(ctx, `UPDATE analysis_steps SET status='canceled',ended_at=NOW() WHERE run_id=$1 AND status='running'`, t.ID); err != nil {
				return err
			}
			if _, err = tx.Exec(ctx, `UPDATE analysis_budget_ledger SET status='unknown' WHERE run_id=$1 AND status='reserved'`, t.ID); err != nil {
				return err
			}
			if _, err = tx.Exec(ctx, `UPDATE task_outbox SET delivered_at=NOW() WHERE task_id=$1`, t.ID); err != nil {
				return err
			}
		}
		if t.ConversationID != "" {
			if t.Status == "needs_clarification" {
				_, err = tx.Exec(ctx, `UPDATE conversations SET pending=NULL,version=version+1,updated_at=NOW() WHERE id=$1 AND pending->>'last_task_id'=$2 AND active_task_id IS NULL`, t.ConversationID, t.ID)
			} else {
				err = completeConversation(ctx, tx, t, "canceled", nil)
			}
			if err != nil {
				return err
			}
		}
		if _, err = tx.Exec(ctx, `UPDATE task_attempts SET state='canceled',ended_at=NOW(),error_code='canceled' WHERE task_id=$1 AND state='running'`, t.ID); err != nil {
			return err
		}
		if err = event(ctx, tx, id, "canceled", map[string]string{"message": "任务已停止"}); err != nil {
			return err
		}
	}
	return tx.Commit(ctx)
}

func (s *Store) Claim(ctx context.Context) (Task, error) {
	return s.ClaimWithAnalysis(ctx, false)
}

func (s *Store) ClaimWithAnalysis(ctx context.Context, analysisEnabled bool) (Task, error) {
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return Task{}, err
	}
	defer tx.Rollback(ctx)
	// 先读取候选并锁其会话，再取队列容量锁和任务行，避免与接纳/停止锁反序。
	candidate, err := scanTask(tx.QueryRow(ctx, `SELECT `+taskColumns+` FROM tasks t JOIN task_outbox o ON o.task_id=t.id WHERE o.delivered_at IS NULL AND (t.kind='query' OR ($1 AND EXISTS(SELECT 1 FROM analysis_runs r WHERE r.id=t.id AND r.ready_at<=NOW()) AND (SELECT COUNT(*) FROM tasks a WHERE a.kind='analysis' AND a.id<>t.id AND a.status='querying' AND a.lease_until>NOW())<2)) AND (t.status='pending' OR (t.status='querying' AND t.lease_until<NOW())) AND NOT EXISTS(SELECT 1 FROM tasks a WHERE a.user_id=t.user_id AND a.id<>t.id AND a.status='querying' AND a.lease_until>NOW()) ORDER BY t.created_at LIMIT 1`, analysisEnabled))
	if err != nil {
		return Task{}, err
	}
	if err = lockTaskConversation(ctx, tx, candidate); err != nil {
		return Task{}, err
	}
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(710091)`); err != nil {
		return Task{}, err
	}
	var active int
	if err = tx.QueryRow(ctx, `SELECT COUNT(*) FROM tasks WHERE status='querying' AND lease_until>NOW()`).Scan(&active); err != nil {
		return Task{}, err
	}
	if active >= 50 {
		return Task{}, ErrMissing
	}
	t, err := scanTask(tx.QueryRow(ctx, `SELECT `+taskColumns+` FROM tasks t JOIN task_outbox o ON o.task_id=t.id WHERE t.id=$1 AND o.delivered_at IS NULL AND (t.kind='query' OR ($2 AND EXISTS(SELECT 1 FROM analysis_runs r WHERE r.id=t.id AND r.ready_at<=NOW()) AND (SELECT COUNT(*) FROM tasks a WHERE a.kind='analysis' AND a.id<>t.id AND a.status='querying' AND a.lease_until>NOW())<2)) AND (t.status='pending' OR (t.status='querying' AND t.lease_until<NOW())) AND NOT EXISTS(SELECT 1 FROM tasks a WHERE a.user_id=t.user_id AND a.id<>t.id AND a.status='querying' AND a.lease_until>NOW()) FOR UPDATE OF t SKIP LOCKED`, candidate.ID, analysisEnabled))
	if err != nil {
		return Task{}, err
	}
	if t.Kind != "analysis" && (t.Attempts >= 3 || (t.StartedAt != nil && time.Since(*t.StartedAt) > 5*time.Minute)) {
		if _, err = tx.Exec(ctx, `UPDATE tasks SET status='failed',error='执行预算已耗尽',lease_until=NULL,updated_at=NOW() WHERE id=$1`, t.ID); err != nil {
			return Task{}, err
		}
		if err = completeConversation(ctx, tx, t, "failed", nil); err != nil {
			return Task{}, err
		}
		if _, err = tx.Exec(ctx, `UPDATE task_attempts SET state='lease_expired',ended_at=NOW(),error_code='lease_expired' WHERE task_id=$1 AND state='running'`, t.ID); err != nil {
			return Task{}, err
		}
		if err = event(ctx, tx, t.ID, "error", map[string]string{"message": "执行预算已耗尽"}); err != nil {
			return Task{}, err
		}
		if _, err = tx.Exec(ctx, `UPDATE task_outbox SET delivered_at=NOW() WHERE task_id=$1`, t.ID); err != nil {
			return Task{}, err
		}
		if err = tx.Commit(ctx); err != nil {
			return Task{}, err
		}
		return Task{}, ErrMissing
	}
	// 封存旧租约并在同一事务创建执行记录，遥测不能改变任务版本隔离。
	if _, err = tx.Exec(ctx, `UPDATE task_attempts SET state='lease_expired',ended_at=NOW(),error_code='lease_expired' WHERE task_id=$1 AND state='running'`, t.ID); err != nil {
		return Task{}, err
	}
	t.Version++
	t.Attempts++
	t.Status = "querying"
	if _, err = tx.Exec(ctx, `INSERT INTO task_attempts(tenant_id,task_id,execution_version,queue_ms) SELECT tenant_id,id,$2,GREATEST(0,EXTRACT(EPOCH FROM (NOW()-updated_at))*1000) FROM tasks WHERE id=$1`, t.ID, t.Version); err != nil {
		return Task{}, err
	}
	if _, err = tx.Exec(ctx, `UPDATE tasks SET status='querying',attempts=$2,execution_version=$3,started_at=COALESCE(started_at,NOW()),lease_until=NOW()+INTERVAL '60 seconds',updated_at=NOW() WHERE id=$1`, t.ID, t.Attempts, t.Version); err != nil {
		return Task{}, err
	}
	if err = event(ctx, tx, t.ID, "tool_start", map[string]string{"tool": "metric_query"}); err != nil {
		return Task{}, err
	}
	return t, tx.Commit(ctx)
}

func (s *Store) Retry(ctx context.Context, t Task, message string) error {
	if t.Attempts >= 3 {
		return s.Finish(ctx, t, "failed", nil, message)
	}
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if err = lockTaskConversation(ctx, tx, t); err != nil {
		return err
	}
	tag, err := tx.Exec(ctx, `UPDATE tasks SET status='pending',lease_until=NULL,updated_at=NOW() WHERE id=$1 AND execution_version=$2 AND status='querying'`, t.ID, t.Version)
	if err != nil {
		return err
	}
	if tag.RowsAffected() == 0 {
		return nil
	}
	if _, err = tx.Exec(ctx, `UPDATE task_attempts SET state='retry',ended_at=NOW() WHERE task_id=$1 AND execution_version=$2 AND state='running'`, t.ID, t.Version); err != nil {
		return err
	}
	if err = event(ctx, tx, t.ID, "retry", map[string]any{"message": message, "retry_count": t.Attempts}); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (s *Store) Finish(ctx context.Context, t Task, status string, result json.RawMessage, message string) error {
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if err = lockTaskConversation(ctx, tx, t); err != nil {
		return err
	}
	if t.ConversationID != "" && (status == "completed" || status == "needs_clarification") {
		var current User
		err = tx.QueryRow(ctx, `SELECT id,tenant_id,role,allowed_metrics FROM users WHERE id=$1 AND tenant_id=$2 AND enabled FOR SHARE`, t.UserID, t.TenantID).Scan(&current.ID, &current.TenantID, &current.Role, &current.AllowedMetrics)
		var role string
		roleErr := tx.QueryRow(ctx, `SELECT role FROM conversations WHERE id=$1`, t.ConversationID).Scan(&role)
		if err != nil || roleErr != nil || current.Role != role || !MetricsAllowed(t.Context, current.AllowedMetrics) || !MetricsAllowed(result, current.AllowedMetrics) {
			status, result, message = "failed", nil, "指标权限或账号已变化"
		}
	}
	tag, err := tx.Exec(ctx, `UPDATE tasks SET status=$3,result=$4,error=$5,lease_until=NULL,updated_at=NOW() WHERE id=$1 AND execution_version=$2 AND status='querying'`, t.ID, t.Version, status, result, message)
	if err != nil {
		return err
	}
	if tag.RowsAffected() == 0 {
		return nil
	}
	if err = completeConversation(ctx, tx, t, status, result); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `UPDATE task_attempts SET state=$3,ended_at=NOW() WHERE task_id=$1 AND execution_version=$2 AND state='running'`, t.ID, t.Version, status); err != nil {
		return err
	}
	kind := "done"
	if status == "failed" {
		kind = "error"
	}
	if status == "needs_clarification" {
		kind = "clarification"
	}
	if err = event(ctx, tx, t.ID, kind, map[string]any{"status": status, "result": result, "message": message}); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `UPDATE task_outbox SET delivered_at=NOW() WHERE task_id=$1`, t.ID); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (s *Store) Events(ctx context.Context, id string, after int) ([]Event, error) {
	rows, err := s.Pool.Query(ctx, `SELECT seq,kind,data FROM task_events WHERE task_id=$1 AND seq>$2 ORDER BY seq LIMIT 100`, id, after)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	events := []Event{}
	for rows.Next() {
		var e Event
		if err = rows.Scan(&e.Seq, &e.Kind, &e.Data); err != nil {
			return nil, err
		}
		events = append(events, e)
	}
	return events, rows.Err()
}
