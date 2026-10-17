package store

import (
	"context"
	"encoding/json"
	"errors"
	"slices"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

var ErrAnalysisBudget = errors.New("analysis budget exhausted")

// 价格单位为每百万 Token 的微货币单位；配置与任务一起冻结，恢复不重置预算。
type AnalysisPolicy struct {
	Currency     string `json:"currency"`
	PriceVersion string `json:"price_version"`
	InputPrice   int64  `json:"input_price"`
	OutputPrice  int64  `json:"output_price"`
	MaxCost      int64  `json:"max_cost"`
	Model        string `json:"model"`
	Tokenizer    string `json:"tokenizer"`
	Strategy     string `json:"strategy"`
}

func (p AnalysisPolicy) Valid() bool {
	return (p.Strategy == "" || p.Strategy == "adaptive" || p.Strategy == "fixed") && p.Currency != "" && len(p.Currency) <= 12 && p.PriceVersion != "" && len(p.PriceVersion) <= 100 && p.Model != "" && len(p.Model) <= 100 &&
		p.Tokenizer == "byte_bpe_upper_bound" && p.InputPrice > 0 && p.OutputPrice > 0 && p.InputPrice <= 1000000000 && p.OutputPrice <= 1000000000 && p.MaxCost > 0 && p.MaxCost <= 1000000000
}
func (p AnalysisPolicy) Cost(input, output int64) int64 {
	return (input*p.InputPrice + output*p.OutputPrice + 999999) / 1000000
}

type AnalysisMetadata struct {
	ContextMetadata
	DatasetRevision string `json:"dataset_revision"`
	RuntimeVersion  string `json:"runtime_version"`
	Model           string `json:"model"`
	ModelReady      bool   `json:"model_ready"`
}
type AnalysisAction struct {
	Action    string          `json:"action"`
	Arguments json.RawMessage `json:"arguments"`
}
type AnalysisState struct {
	Phase          string          `json:"phase"`
	Spec           json.RawMessage `json:"spec"`
	Pending        *AnalysisAction `json:"pending_action,omitempty"`
	Feedback       json.RawMessage `json:"feedback"`
	Answers        []string        `json:"answers"`
	ActiveStep     string          `json:"active_step,omitempty"`
	Sequence       int             `json:"sequence"`
	ModelCalls     int             `json:"model_calls"`
	Actions        int             `json:"actions"`
	SQLCalls       int             `json:"sql_calls"`
	Repairs        int             `json:"repairs"`
	SQLRetries     int             `json:"sql_retries"`
	Clarifications int             `json:"clarifications"`
	ActiveMS       int64           `json:"active_ms"`
	Tokens         int64           `json:"tokens"`
	Cost           int64           `json:"cost"`
	Message        string          `json:"message"`
	LastErrorKey   string          `json:"last_error_key,omitempty"`
	NoProgress     int             `json:"no_progress"`
	ArchivedUsage  *AnalysisUsage  `json:"archived_usage,omitempty"`
}
type AnalysisUsage struct {
	KnownTokens  int64 `json:"known_tokens"`
	KnownCost    int64 `json:"known_cost_micros"`
	UnknownCalls int   `json:"unknown_calls"`
}
type AnalysisRun struct {
	ID        string            `json:"id"`
	TenantID  string            `json:"-"`
	UserID    string            `json:"-"`
	Role      string            `json:"-"`
	Metadata  AnalysisMetadata  `json:"metadata"`
	Policy    AnalysisPolicy    `json:"policy"`
	State     AnalysisState     `json:"state"`
	Version   int               `json:"version"`
	Metrics   []string          `json:"metrics_used"`
	Status    string            `json:"status"`
	Question  string            `json:"question"`
	Artifacts []json.RawMessage `json:"artifacts"`
	Steps     []json.RawMessage `json:"steps"`
	Usage     AnalysisUsage     `json:"usage"`
}
type AnalysisStep struct {
	ID     string
	Phase  string
	Cached json.RawMessage
}
type AnalysisOutcome struct {
	Status        string   `json:"status"`
	ErrorCode     string   `json:"error_code"`
	Message       string   `json:"message"`
	MissingFields []string `json:"missing_fields"`
	ModelSent     *bool    `json:"model_sent"`
	Usage         *struct {
		Input  *int64 `json:"prompt_tokens"`
		Output *int64 `json:"completion_tokens"`
	} `json:"usage"`
	Decision *struct {
		Spec   json.RawMessage `json:"spec"`
		Action AnalysisAction  `json:"action"`
	} `json:"decision"`
	Artifact json.RawMessage `json:"artifact"`
}

func marshal(v any) []byte { b, _ := json.Marshal(v); return b }
func canonicalJSON(raw json.RawMessage) string {
	var v any
	if json.Unmarshal(raw, &v) != nil {
		return "!invalid"
	}
	return string(marshal(v))
}

func scanAnalysis(row pgx.Row) (AnalysisRun, error) {
	var r AnalysisRun
	var meta, policy, state []byte
	err := row.Scan(&r.ID, &r.TenantID, &r.UserID, &r.Role, &meta, &policy, &state, &r.Version, &r.Metrics, &r.Status, &r.Question)
	if errors.Is(err, pgx.ErrNoRows) {
		return r, ErrMissing
	}
	if err != nil {
		return r, err
	}
	for _, pair := range []struct {
		raw   []byte
		value any
	}{{meta, &r.Metadata}, {policy, &r.Policy}, {state, &r.State}} {
		if err = json.Unmarshal(pair.raw, pair.value); err != nil {
			return r, err
		}
	}
	r.Artifacts = []json.RawMessage{}
	r.Steps = []json.RawMessage{}
	return r, nil
}

const analysisColumns = `r.id,r.tenant_id,r.user_id,r.role,r.metadata,r.policy,r.state,r.version,r.required_metrics,t.status,t.input->>'question'`

func (s *Store) Analysis(ctx context.Context, u User, id string) (AnalysisRun, error) {
	r, err := scanAnalysis(s.Pool.QueryRow(ctx, `SELECT `+analysisColumns+` FROM analysis_runs r JOIN tasks t ON t.id=r.id WHERE r.id=$1 AND r.tenant_id=$2 AND r.user_id=$3`, id, u.TenantID, u.ID))
	if err != nil {
		return r, err
	}
	if !AnalysisAllowed(u, r) {
		return r, ErrPermission
	}
	rows, err := s.Pool.Query(ctx, `SELECT body FROM analysis_artifacts WHERE run_id=$1 ORDER BY created_at,result_ref`, id)
	if err != nil {
		return r, err
	}
	for rows.Next() {
		var b json.RawMessage
		if err = rows.Scan(&b); err != nil {
			rows.Close()
			return r, err
		}
		r.Artifacts = append(r.Artifacts, b)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return r, err
	}
	if err = s.Pool.QueryRow(ctx, `SELECT COALESCE(SUM(actual_tokens),0),COALESCE(SUM(actual_cost),0),COUNT(*) FILTER(WHERE status IN ('reserved','unknown')) FROM analysis_budget_ledger WHERE run_id=$1`, id).Scan(&r.Usage.KnownTokens, &r.Usage.KnownCost, &r.Usage.UnknownCalls); err != nil {
		return r, err
	}
	if r.State.ArchivedUsage != nil {
		r.Usage = *r.State.ArchivedUsage
	}
	rows, err = s.Pool.Query(ctx, `SELECT jsonb_build_object('id',s.id,'sequence',sequence,'phase',phase,'action',input->>'action','arguments',CASE WHEN phase='tool' THEN input->'arguments' ELSE NULL END,'status',s.status,'started_at',started_at,'ended_at',ended_at,'error_code',output->>'error_code','cached',a.cached) FROM analysis_steps s JOIN analysis_step_attempts a ON a.step_id=s.id WHERE run_id=$1 ORDER BY sequence`, id)
	if err != nil {
		return r, err
	}
	defer rows.Close()
	for rows.Next() {
		var b json.RawMessage
		if err = rows.Scan(&b); err != nil {
			return r, err
		}
		r.Steps = append(r.Steps, b)
	}
	return r, rows.Err()
}
func AnalysisAllowed(u User, r AnalysisRun) bool {
	if r.TenantID != u.TenantID || r.UserID != u.ID || r.Role != u.Role {
		return false
	}
	for _, m := range r.Metrics {
		if !slices.Contains(u.AllowedMetrics, m) || !slices.Contains(Metrics, m) {
			return false
		}
	}
	return true
}

func (s *Store) CreateAnalysis(ctx context.Context, u User, requestID, question string, spec json.RawMessage, meta AnalysisMetadata, policy AnalysisPolicy) (AnalysisRun, error) {
	if !policy.Valid() {
		return AnalysisRun{}, ErrPermission
	}
	requestHash := TokenHash(string(marshal([]any{question, json.RawMessage(spec)})))
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return AnalysisRun{}, err
	}
	defer tx.Rollback(ctx)
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(710091)`); err != nil {
		return AnalysisRun{}, err
	}
	var existing, hash string
	err = tx.QueryRow(ctx, `SELECT id,request_hash FROM analysis_runs WHERE tenant_id=$1 AND user_id=$2 AND request_id=$3`, u.TenantID, u.ID, requestID).Scan(&existing, &hash)
	if err == nil {
		if hash != requestHash {
			return AnalysisRun{}, ErrConflict
		}
		_ = tx.Rollback(ctx)
		return s.Analysis(ctx, u, existing)
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return AnalysisRun{}, err
	}
	var pending, active int
	if err = tx.QueryRow(ctx, `SELECT COUNT(*) FILTER(WHERE status='pending'),COUNT(*) FILTER(WHERE user_id=$1 AND status IN ('pending','querying')) FROM tasks`, u.ID).Scan(&pending, &active); err != nil {
		return AnalysisRun{}, err
	}
	if pending >= 100 || active > 0 {
		return AnalysisRun{}, ErrQueueFull
	}
	id := ID()
	if len(spec) == 0 {
		spec = json.RawMessage(`null`)
	}
	state := AnalysisState{Phase: "decide", Spec: spec, Answers: []string{}, Feedback: json.RawMessage(`{}`)}
	if _, err = tx.Exec(ctx, `INSERT INTO tasks(id,tenant_id,user_id,input,kind) VALUES($1,$2,$3,$4,'analysis')`, id, u.TenantID, u.ID, marshal(Input{Question: question})); err != nil {
		return AnalysisRun{}, err
	}
	if _, err = tx.Exec(ctx, `INSERT INTO analysis_runs(id,tenant_id,user_id,role,binding,metadata,policy,state,request_id,request_hash) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)`, id, u.TenantID, u.ID, u.Role, meta.Binding, marshal(meta), marshal(policy), marshal(state), requestID, requestHash); err != nil {
		return AnalysisRun{}, err
	}
	if _, err = tx.Exec(ctx, `INSERT INTO task_outbox(task_id) VALUES($1)`, id); err != nil {
		return AnalysisRun{}, err
	}
	if err = event(ctx, tx, id, "accepted", map[string]string{"task_id": id, "kind": "analysis"}); err != nil {
		return AnalysisRun{}, err
	}
	if err = tx.Commit(ctx); err != nil {
		return AnalysisRun{}, err
	}
	return s.Analysis(ctx, u, id)
}

// 先锁 task 再锁 run，与停止任务的锁顺序一致；旧租约不能推进状态。
func lockAnalysis(ctx context.Context, tx pgx.Tx, t Task) (AnalysisRun, error) {
	var status string
	var version int
	if err := tx.QueryRow(ctx, `SELECT status,execution_version FROM tasks WHERE id=$1 AND tenant_id=$2 FOR UPDATE`, t.ID, t.TenantID).Scan(&status, &version); err != nil {
		return AnalysisRun{}, err
	}
	if status != "querying" || version != t.Version {
		return AnalysisRun{}, ErrConflict
	}
	r, err := scanAnalysis(tx.QueryRow(ctx, `SELECT `+analysisColumns+` FROM analysis_runs r JOIN tasks t ON t.id=r.id WHERE r.id=$1 FOR UPDATE OF r`, t.ID))
	if err != nil {
		return r, err
	}
	var current User
	err = tx.QueryRow(ctx, `SELECT id,tenant_id,role,allowed_metrics FROM users WHERE id=$1 AND tenant_id=$2 AND enabled FOR SHARE`, r.UserID, r.TenantID).Scan(&current.ID, &current.TenantID, &current.Role, &current.AllowedMetrics)
	if err != nil || !AnalysisAllowed(current, r) {
		return r, ErrPermission
	}
	return r, nil
}

func persistAnalysis(ctx context.Context, tx pgx.Tx, t Task, r AnalysisRun, status string, ready time.Time, result json.RawMessage) error {
	if _, err := tx.Exec(ctx, `UPDATE analysis_runs SET state=$2,required_metrics=$3,version=version+1,ready_at=$4,updated_at=NOW() WHERE id=$1`, r.ID, marshal(r.State), r.Metrics, ready); err != nil {
		return err
	}
	if _, err := tx.Exec(ctx, `UPDATE tasks SET status=$2,result=$3,error=$4,lease_until=NULL,updated_at=NOW() WHERE id=$1`, r.ID, status, result, r.State.Message); err != nil {
		return err
	}
	attemptStatus := status
	if status == "pending" {
		attemptStatus = "completed"
	}
	if _, err := tx.Exec(ctx, `UPDATE task_attempts SET state=$3,ended_at=NOW() WHERE task_id=$1 AND execution_version=$2 AND state='running'`, r.ID, t.Version, attemptStatus); err != nil {
		return err
	}
	kind := "step"
	if status == "completed" {
		kind = "done"
	}
	if status == "failed" {
		kind = "error"
	}
	if status == "needs_clarification" {
		kind = "clarification"
	}
	if err := event(ctx, tx, r.ID, kind, map[string]any{"status": status, "phase": r.State.Phase, "message": r.State.Message, "version": r.Version + 1}); err != nil {
		return err
	}
	if status == "completed" || status == "failed" || status == "canceled" {
		_, err := tx.Exec(ctx, `UPDATE task_outbox SET delivered_at=NOW() WHERE task_id=$1`, r.ID)
		return err
	}
	return nil
}

func (s *Store) BeginAnalysisStep(ctx context.Context, t Task) (AnalysisRun, AnalysisStep, error) {
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return AnalysisRun{}, AnalysisStep{}, err
	}
	defer tx.Rollback(ctx)
	r, err := lockAnalysis(ctx, tx, t)
	if err != nil {
		return r, AnalysisStep{}, err
	}
	st := &r.State
	if st.ActiveStep != "" {
		st.ActiveMS += 35000
		var phase string
		if err = tx.QueryRow(ctx, `SELECT phase FROM analysis_steps WHERE id=$1 AND status='running'`, st.ActiveStep).Scan(&phase); err != nil {
			return r, AnalysisStep{}, err
		}
		if _, err = tx.Exec(ctx, `UPDATE analysis_steps SET status='unknown',ended_at=NOW() WHERE id=$1`, st.ActiveStep); err != nil {
			return r, AnalysisStep{}, err
		}
		if _, err = tx.Exec(ctx, `UPDATE analysis_step_attempts SET status='unknown' WHERE step_id=$1`, st.ActiveStep); err != nil {
			return r, AnalysisStep{}, err
		}
		if phase == "decide" || (st.Pending != nil && st.Pending.Action == "query_metrics" && st.SQLRetries >= 1) {
			st.Message = "上次调用结果未知，任务已停止"
			st.Phase = "failed"
			if _, err = tx.Exec(ctx, `UPDATE analysis_budget_ledger SET status='unknown' WHERE step_id=$1`, st.ActiveStep); err != nil {
				return r, AnalysisStep{}, err
			}
			if err = persistAnalysis(ctx, tx, t, r, "failed", time.Now(), analysisPartial(r)); err != nil {
				return r, AnalysisStep{}, err
			}
			if err = tx.Commit(ctx); err != nil {
				return r, AnalysisStep{}, err
			}
			return r, AnalysisStep{}, ErrAnalysisBudget
		}
		if st.Pending != nil && st.Pending.Action == "query_metrics" {
			st.SQLRetries++
		}
		st.ActiveStep = ""
	}
	phase := st.Phase
	if phase == "decide" && r.Policy.Strategy == "fixed" && len(st.Spec) > 0 && string(st.Spec) != "null" {
		phase = "fixed"
	}
	if st.ActiveMS >= 180000 || st.Actions >= 12 || st.ModelCalls >= 10 && phase == "decide" || st.Sequence >= 40 {
		st.Message = "分析执行预算已耗尽"
		st.Phase = "failed"
		if err = persistAnalysis(ctx, tx, t, r, "failed", time.Now(), analysisPartial(r)); err != nil {
			return r, AnalysisStep{}, err
		}
		if err = tx.Commit(ctx); err != nil {
			return r, AnalysisStep{}, err
		}
		return r, AnalysisStep{}, ErrAnalysisBudget
	}
	input := marshal(st.Pending)
	if phase == "decide" || phase == "fixed" {
		input = marshal(map[string]any{"version": r.Version, "spec": st.Spec, "feedback": st.Feedback, "answers": st.Answers})
	}
	key := TokenHash(r.Metadata.Binding + canonicalJSON(st.Spec) + string(input))
	step := AnalysisStep{ID: ID(), Phase: phase}
	if phase == "tool" {
		if st.Pending == nil {
			return r, step, ErrConflict
		}
		err = tx.QueryRow(ctx, `SELECT output FROM analysis_steps WHERE run_id=$1 AND action_key=$2 AND status='completed' ORDER BY sequence DESC LIMIT 1`, r.ID, key).Scan(&step.Cached)
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return r, step, err
		}
		st.Actions++
		if len(step.Cached) == 0 && st.Pending.Action == "query_metrics" {
			if st.SQLCalls >= 6 {
				return r, step, ErrAnalysisBudget
			}
			st.SQLCalls++
		}
	} else if phase == "decide" {
		reserve := int64(7024)
		cost := r.Policy.Cost(6000, 1024)
		if st.Tokens+reserve > 30000 || st.Cost+cost > r.Policy.MaxCost {
			return r, step, ErrAnalysisBudget
		}
		st.ModelCalls++
		st.Tokens += reserve
		st.Cost += cost
	} else if phase != "fixed" {
		return r, step, ErrConflict
	}
	st.Sequence++
	st.ActiveStep = step.ID
	if _, err = tx.Exec(ctx, `INSERT INTO analysis_steps(id,tenant_id,run_id,sequence,action_key,phase,execution_version,input,status) VALUES($1,$2,$3,$4,$5,$6,$7,$8,'running')`, step.ID, r.TenantID, r.ID, st.Sequence, key, phase, t.Version, input); err != nil {
		return r, step, err
	}
	if _, err = tx.Exec(ctx, `INSERT INTO analysis_step_attempts(step_id,execution_version,cached) VALUES($1,$2,$3)`, step.ID, t.Version, len(step.Cached) > 0); err != nil {
		return r, step, err
	}
	if phase == "decide" {
		if _, err = tx.Exec(ctx, `INSERT INTO analysis_budget_ledger(step_id,run_id,reserved_tokens,reserved_cost) VALUES($1,$2,7024,$3)`, step.ID, r.ID, r.Policy.Cost(6000, 1024)); err != nil {
			return r, step, err
		}
	}
	if _, err = tx.Exec(ctx, `UPDATE analysis_runs SET state=$2 WHERE id=$1`, r.ID, marshal(st)); err != nil {
		return r, step, err
	}
	if err = tx.Commit(ctx); err != nil {
		return r, step, err
	}
	return r, step, nil
}

func analysisPartial(r AnalysisRun) json.RawMessage {
	return marshal(map[string]any{"analysis_result_version": 1, "partial": true, "message": r.State.Message, "metrics_used": r.Metrics})
}

func requiredSpec(raw json.RawMessage) ([]string, error) {
	var v struct {
		Goal   string   `json:"goal"`
		Checks []string `json:"checks"`
	}
	if json.Unmarshal(raw, &v) != nil {
		return nil, ErrPermission
	}
	if v.Goal == "top_n_total" {
		if slices.Contains(v.Checks, "aov") {
			return []string{"gmv", "orders", "aov"}, nil
		}
		if slices.Contains(v.Checks, "orders") {
			return []string{"gmv", "orders"}, nil
		}
		return []string{"gmv"}, nil
	}
	if v.Goal == "compare_declines" {
		return []string{"gmv", "orders", "aov"}, nil
	}
	return nil, ErrPermission
}

func (s *Store) CompleteAnalysisStep(ctx context.Context, t Task, step AnalysisStep, out AnalysisOutcome, elapsed time.Duration) error {
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	r, err := lockAnalysis(ctx, tx, t)
	if err != nil {
		return err
	}
	st := &r.State
	if st.ActiveStep != step.ID {
		return ErrConflict
	}
	st.ActiveMS += elapsed.Milliseconds()
	if st.ActiveMS > 180000 {
		out.Status = "error"
		out.ErrorCode = "budget_exhausted"
		out.Message = "分析活跃执行时间已超预算"
	}
	status := "pending"
	ready := time.Now()
	st.Message = ""
	// 未知用量不返还预留；只有供应商完整报告且不超边界时才结算。
	if step.Phase == "decide" {
		var tokens, cost *int64
		if out.ModelSent != nil && !*out.ModelSent {
			zero := int64(0)
			tokens = &zero
			cost = &zero
		} else if out.Usage != nil && out.Usage.Input != nil && out.Usage.Output != nil && *out.Usage.Input >= 0 && *out.Usage.Input <= 6000 && *out.Usage.Output >= 0 && *out.Usage.Output <= 1024 {
			total := *out.Usage.Input + *out.Usage.Output
			actual := r.Policy.Cost(*out.Usage.Input, *out.Usage.Output)
			tokens = &total
			cost = &actual
		} else if out.Usage != nil && out.Usage.Input != nil && out.Usage.Output != nil {
			// 供应商用量推翻预留上界时，不能继续以该估算保证预算。
			out.Status = "error"
			out.ErrorCode = "budget_exhausted"
			out.Message = "供应商用量超出预留边界，已停止并保留未知费用"
		}
		ledgerStatus := "unknown"
		if tokens != nil {
			st.Tokens -= 7024 - *tokens
			st.Cost -= r.Policy.Cost(6000, 1024) - *cost
			ledgerStatus = "settled"
		}
		if _, err = tx.Exec(ctx, `UPDATE analysis_budget_ledger SET actual_tokens=$2,actual_cost=$3,status=$4 WHERE step_id=$1`, step.ID, tokens, cost, ledgerStatus); err != nil {
			return err
		}
	}
	if out.Status == "completed" && (step.Phase == "decide" || step.Phase == "fixed") {
		if out.Decision == nil {
			return errors.New("invalid analysis decision")
		}
		if len(st.Spec) > 0 && string(st.Spec) != "null" && canonicalJSON(st.Spec) != canonicalJSON(out.Decision.Spec) {
			out.Status = "error"
			out.ErrorCode = "invalid_arguments"
			out.Message = "模型不得改变已确认条件"
		} else {
			if len(out.Decision.Spec) > 0 && string(out.Decision.Spec) != "null" {
				metrics, e := requiredSpec(out.Decision.Spec)
				if e != nil {
					return e
				}
				// 历史证据也需要持续鉴权，不能在下一次决策时丢掉已读取的依赖。
				for _, metric := range metrics {
					if !slices.Contains(r.Metrics, metric) {
						r.Metrics = append(r.Metrics, metric)
					}
				}
				st.Spec = out.Decision.Spec
			}
			st.Pending = &out.Decision.Action
			st.Phase = "tool"
			if r.Policy.Strategy == "fixed" && step.Phase == "decide" && out.Decision.Action.Action != "request_clarification" && string(st.Spec) != "null" {
				st.Phase = "decide"
				st.Pending = nil
			}
		}
	} else if out.Status == "needs_clarification" && step.Phase == "tool" {
		st.Clarifications++
		st.Message = out.Message
		if st.Clarifications > 2 {
			status = "failed"
			st.Message = "已达到澄清轮次上限"
		} else {
			status = "needs_clarification"
			st.Phase = "waiting_input"
		}
	} else if out.Status == "completed" && step.Phase == "tool" {
		var a struct {
			ResultRef string          `json:"result_ref"`
			Kind      string          `json:"kind"`
			Metrics   []string        `json:"metrics_used"`
			Binding   string          `json:"binding"`
			Data      json.RawMessage `json:"data"`
		}
		if json.Unmarshal(out.Artifact, &a) != nil || len(a.ResultRef) != 64 || a.Binding != r.Metadata.Binding || len(a.Metrics) == 0 {
			return errors.New("invalid analysis artifact")
		}
		for _, m := range a.Metrics {
			if !slices.Contains(Metrics, m) {
				return ErrPermission
			}
			if !slices.Contains(r.Metrics, m) {
				r.Metrics = append(r.Metrics, m)
			}
		}
		var inserted pgconn.CommandTag
		if inserted, err = tx.Exec(ctx, `INSERT INTO analysis_artifacts(tenant_id,run_id,result_ref,body) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING`, r.TenantID, r.ID, a.ResultRef, out.Artifact); err != nil {
			return err
		}
		if inserted.RowsAffected() > 0 {
			st.LastErrorKey = ""
			st.NoProgress = 0
		}
		if a.Kind == "report" {
			status = "completed"
			st.Phase = "completed"
		} else {
			st.Phase = "decide"
			st.Pending = nil
		}
	}
	if out.Status == "error" {
		switch out.ErrorCode {
		case "invalid_arguments", "model_format":
			if st.Repairs < 1 {
				st.Repairs++
				st.Phase = "decide"
				st.Pending = nil
			} else {
				status = "failed"
			}
		case "missing_conditions":
			status = "failed"
		case "missing_evidence":
			key := TokenHash(canonicalJSON(marshal(st.Pending)))
			if st.LastErrorKey == key {
				st.NoProgress++
			} else {
				st.LastErrorKey = key
				st.NoProgress = 1
			}
			if st.NoProgress >= 2 {
				status = "failed"
			} else {
				st.Phase = "decide"
				st.Pending = nil
			}
		case "sql_timeout", "sql_connection", "sql_unknown":
			if st.SQLRetries < 1 {
				st.SQLRetries++
				ready = ready.Add(time.Second)
				if out.ErrorCode == "sql_unknown" {
					ready = ready.Add(60 * time.Second)
				}
				st.Phase = "tool"
			} else {
				status = "failed"
			}
		default:
			status = "failed"
		}
		if status == "failed" {
			st.Message = out.Message
			if st.Message == "" {
				st.Message = "分析未完成，请新建任务"
			}
			st.Phase = "failed"
		}
	}
	st.Feedback = marshal(map[string]string{"status": out.Status, "error_code": out.ErrorCode, "message": out.Message})
	stepStatus := "completed"
	if out.Status == "error" {
		stepStatus = "failed"
	}
	if _, err = tx.Exec(ctx, `UPDATE analysis_steps SET status=$2,output=$3,ended_at=NOW() WHERE id=$1 AND execution_version=$4 AND status='running'`, step.ID, stepStatus, marshal(out), t.Version); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `UPDATE analysis_step_attempts SET status=$2,telemetry=$3 WHERE step_id=$1`, step.ID, stepStatus, marshal(map[string]any{"duration_ms": elapsed.Milliseconds(), "usage": out.Usage})); err != nil {
		return err
	}
	var result json.RawMessage
	if status == "completed" {
		result = marshal(map[string]any{"analysis_result_version": 1, "partial": false, "report": json.RawMessage(out.Artifact), "metrics_used": r.Metrics})
	}
	if status == "failed" {
		result = analysisPartial(r)
	}
	st.ActiveStep = ""
	// 再次核对新增指标依赖，禁止模型通过工具输出扩大权限。
	var allowed []string
	if err = tx.QueryRow(ctx, `SELECT allowed_metrics FROM users WHERE id=$1 AND enabled FOR SHARE`, r.UserID).Scan(&allowed); err != nil {
		return err
	}
	for _, m := range r.Metrics {
		if !slices.Contains(allowed, m) {
			return ErrPermission
		}
	}
	if err = persistAnalysis(ctx, tx, t, r, status, ready, result); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (s *Store) FailAnalysis(ctx context.Context, t Task, message string) error {
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	// 终止路径允许处理刚撤权的账号，但仍必须匹配本次领取版本。
	var r AnalysisRun
	if err = tx.QueryRow(ctx, `SELECT id FROM tasks WHERE id=$1 AND execution_version=$2 AND status='querying' FOR UPDATE`, t.ID, t.Version).Scan(&r.ID); err != nil {
		return err
	}
	r, err = scanAnalysis(tx.QueryRow(ctx, `SELECT `+analysisColumns+` FROM analysis_runs r JOIN tasks t ON t.id=r.id WHERE r.id=$1 FOR UPDATE OF r`, t.ID))
	if err != nil {
		return err
	}
	r.State.Message = message
	r.State.Phase = "failed"
	if r.State.ActiveStep != "" {
		if _, err = tx.Exec(ctx, `UPDATE analysis_steps SET status='unknown',ended_at=NOW() WHERE id=$1 AND status='running'`, r.State.ActiveStep); err != nil {
			return err
		}
		if _, err = tx.Exec(ctx, `UPDATE analysis_budget_ledger SET status='unknown' WHERE step_id=$1 AND status='reserved'`, r.State.ActiveStep); err != nil {
			return err
		}
	}
	if err = persistAnalysis(ctx, tx, t, r, "failed", time.Now(), analysisPartial(r)); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (s *Store) AnswerAnalysis(ctx context.Context, u User, id, requestID, answer string, version int) error {
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	var status string
	if err = tx.QueryRow(ctx, `SELECT status FROM tasks WHERE id=$1 AND tenant_id=$2 AND user_id=$3 FOR UPDATE`, id, u.TenantID, u.ID).Scan(&status); err != nil {
		return ErrMissing
	}
	r, err := scanAnalysis(tx.QueryRow(ctx, `SELECT `+analysisColumns+` FROM analysis_runs r JOIN tasks t ON t.id=r.id WHERE r.id=$1 FOR UPDATE OF r`, id))
	if err != nil {
		return err
	}
	if !AnalysisAllowed(u, r) {
		return ErrPermission
	}
	hash := TokenHash(answer)
	var oldHash string
	err = tx.QueryRow(ctx, `SELECT body_hash FROM analysis_answers WHERE run_id=$1 AND request_id=$2`, id, requestID).Scan(&oldHash)
	if err == nil {
		if oldHash != hash {
			return ErrConflict
		}
		return nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return err
	}
	if status != "needs_clarification" || version != r.Version || len(r.State.Answers) >= 2 {
		return ErrConflict
	}
	var expired bool
	if err = tx.QueryRow(ctx, `SELECT updated_at < NOW()-INTERVAL '24 hours' FROM analysis_runs WHERE id=$1`, id).Scan(&expired); err != nil {
		return err
	}
	if expired {
		return ErrConflict
	}
	r.State.Answers = append(r.State.Answers, answer)
	r.State.Phase = "decide"
	r.State.Pending = nil
	r.State.Message = ""
	r.State.ActiveStep = ""
	if _, err = tx.Exec(ctx, `UPDATE analysis_runs SET state=$2,version=version+1,ready_at=NOW(),updated_at=NOW() WHERE id=$1`, id, marshal(r.State)); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `UPDATE tasks SET status='pending',error='',updated_at=NOW() WHERE id=$1`, id); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `INSERT INTO analysis_answers(run_id,request_id,body_hash,resulting_version) VALUES($1,$2,$3,$4)`, id, requestID, hash, r.Version+1); err != nil {
		return err
	}
	if err = event(ctx, tx, id, "accepted", map[string]string{"message": "补充已接收"}); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// 等待不占执行槽；过期输入由 Worker 定期封存，恢复不能绕过 24 小时限制。
func (s *Store) ExpireAnalysisInputs(ctx context.Context) error {
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	rows, err := tx.Query(ctx, `SELECT t.id FROM tasks t JOIN analysis_runs r ON r.id=t.id WHERE t.status='needs_clarification' AND r.updated_at<NOW()-INTERVAL '24 hours' ORDER BY t.id LIMIT 100 FOR UPDATE OF t SKIP LOCKED`)
	if err != nil {
		return err
	}
	ids := []string{}
	for rows.Next() {
		var id string
		if err = rows.Scan(&id); err != nil {
			rows.Close()
			return err
		}
		ids = append(ids, id)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	for _, id := range ids {
		if _, err = tx.Exec(ctx, `UPDATE tasks SET status='canceled',error='等待补充已超过 24 小时',execution_version=execution_version+1,lease_until=NULL,updated_at=NOW() WHERE id=$1`, id); err != nil {
			return err
		}
		if _, err = tx.Exec(ctx, `UPDATE analysis_runs SET state=jsonb_set(jsonb_set(state,'{phase}','"canceled"'),'{message}','"等待补充已超过 24 小时"'),version=version+1,updated_at=NOW() WHERE id=$1`, id); err != nil {
			return err
		}
		if _, err = tx.Exec(ctx, `UPDATE task_outbox SET delivered_at=NOW() WHERE task_id=$1`, id); err != nil {
			return err
		}
		if err = event(ctx, tx, id, "canceled", map[string]string{"message": "等待补充已过期"}); err != nil {
			return err
		}
	}
	return tx.Commit(ctx)
}

// 仅清理终态任务的详细执行数据，保留原任务结果；不伪造可恢复的过期依据。
func (s *Store) PruneAnalysis(ctx context.Context) (int64, error) {
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback(ctx)
	rows, err := tx.Query(ctx, `SELECT t.id FROM tasks t JOIN analysis_runs r ON r.id=t.id WHERE t.status IN ('completed','failed','canceled') AND r.updated_at<NOW()-INTERVAL '30 days' AND NOT COALESCE((r.state->'feedback'->>'evidence_expired')::boolean,false) ORDER BY t.id LIMIT 100 FOR UPDATE OF t SKIP LOCKED`)
	if err != nil {
		return 0, err
	}
	ids := []string{}
	for rows.Next() {
		var id string
		if err = rows.Scan(&id); err != nil {
			rows.Close()
			return 0, err
		}
		ids = append(ids, id)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return 0, err
	}
	for _, id := range ids {
		var usage AnalysisUsage
		if err = tx.QueryRow(ctx, `SELECT COALESCE(SUM(actual_tokens),0),COALESCE(SUM(actual_cost),0),COUNT(*) FILTER(WHERE status IN ('reserved','unknown')) FROM analysis_budget_ledger WHERE run_id=$1`, id).Scan(&usage.KnownTokens, &usage.KnownCost, &usage.UnknownCalls); err != nil {
			return 0, err
		}
		for _, query := range []string{`DELETE FROM analysis_step_attempts WHERE step_id IN(SELECT id FROM analysis_steps WHERE run_id=$1)`, `DELETE FROM analysis_budget_ledger WHERE run_id=$1`, `DELETE FROM analysis_steps WHERE run_id=$1`, `DELETE FROM analysis_answers WHERE run_id=$1`, `DELETE FROM analysis_artifacts WHERE run_id=$1 AND body->>'kind'<>'report'`} {
			if _, err = tx.Exec(ctx, query, id); err != nil {
				return 0, err
			}
		}
		if _, err = tx.Exec(ctx, `UPDATE analysis_runs SET state=jsonb_set(jsonb_set(jsonb_set(state,'{feedback}','{"evidence_expired":true}'),'{archived_usage}',$2),'{answers}','[]'),updated_at=NOW() WHERE id=$1`, id, marshal(usage)); err != nil {
			return 0, err
		}
	}
	if err = tx.Commit(ctx); err != nil {
		return 0, err
	}
	return int64(len(ids)), nil
}
