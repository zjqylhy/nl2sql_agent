package store

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"strings"
	"time"
)

// 遥测采用固定字段，缺失值保留为 NULL，不保存用户内容。
type Telemetry struct {
	Version          int                `json:"version"`
	TaskID           string             `json:"task_id"`
	ExecutionVersion int                `json:"execution_version"`
	Parser           string             `json:"parser"`
	Model            string             `json:"model"`
	ModelCalls       *int               `json:"model_calls"`
	InputTokens      *int64             `json:"input_tokens"`
	OutputTokens     *int64             `json:"output_tokens"`
	TotalTokens      *int64             `json:"total_tokens"`
	Stages           map[string]float64 `json:"stages_ms"`
	ErrorCode        string             `json:"error_code"`
	Rows             *int               `json:"rows"`
}

var ErrorCodes = map[string]bool{"": true, "permission": true, "invalid_input": true, "clarification": true, "unsupported": true, "model_config": true, "model_timeout": true, "model_rate_limit": true, "model_auth": true, "model_http": true, "model_network": true, "model_format": true, "sql_timeout": true, "db_error": true, "sql_safety": true, "result_limit": true, "datasource_config": true, "internal": true, "agent_network": true, "agent_format": true, "canceled": true, "lease_expired": true}
var stages = map[string]bool{"parse": true, "rules": true, "model": true, "plan_validation": true, "compile": true, "sql_validation": true, "db_connect": true, "sql": true, "normalize": true}

func DecodeTelemetry(raw json.RawMessage, t Task) *Telemetry {
	var v Telemetry
	d := json.NewDecoder(strings.NewReader(string(raw)))
	d.DisallowUnknownFields()
	if d.Decode(&v) != nil || v.Version != 1 || v.TaskID != t.ID || v.ExecutionVersion != t.Version || (v.Parser != "rules" && v.Parser != "llm" && v.Parser != "unknown") || len(v.Model) > 100 || strings.ContainsAny(v.Model, "\r\n") || !ErrorCodes[v.ErrorCode] || v.ModelCalls == nil || *v.ModelCalls < 0 || *v.ModelCalls > 1 || len(v.Stages) > len(stages) {
		return nil
	}
	if v.Stages == nil {
		return nil
	}
	for k, x := range v.Stages {
		if !stages[k] || x < 0 || x > 300000 || math.IsNaN(x) || math.IsInf(x, 0) {
			return nil
		}
	}
	for _, x := range []*int64{v.InputTokens, v.OutputTokens, v.TotalTokens} {
		if x != nil && (*x < 0 || *x > 10000000) {
			return nil
		}
	}
	if v.Rows != nil && (*v.Rows < 0 || *v.Rows > 1000) {
		return nil
	}
	if v.Parser == "rules" && (*v.ModelCalls != 0 || v.TotalTokens == nil || *v.TotalTokens != 0) {
		return nil
	}
	if *v.ModelCalls == 0 && ((v.TotalTokens != nil && *v.TotalTokens != 0) || (v.InputTokens != nil && *v.InputTokens != 0) || (v.OutputTokens != nil && *v.OutputTokens != 0)) {
		return nil
	}
	return &v
}

func (s *Store) RecordAttempt(ctx context.Context, t Task, trace *Telemetry, elapsed time.Duration, bytes int, code string) error {
	if !ErrorCodes[code] || elapsed < 0 || bytes < 0 {
		return errors.New("invalid telemetry")
	}
	var raw []byte
	if trace != nil {
		raw, _ = json.Marshal(trace)
		if code == "" {
			code = trace.ErrorCode
		}
	}
	// 首次观测幂等写入；迟到遥测只补原 attempt，不覆盖停止或新执行状态。
	_, err := s.Pool.Exec(ctx, `UPDATE task_attempts SET call_ms=$4,result_bytes=$5,telemetry=$6,error_code=CASE WHEN error_code IN ('canceled','lease_expired') THEN error_code ELSE $7 END WHERE tenant_id=$1 AND task_id=$2 AND execution_version=$3 AND call_ms IS NULL`, t.TenantID, t.ID, t.Version, float64(elapsed.Microseconds())/1000, bytes, raw, code)
	return err
}

func (s *Store) Operations(ctx context.Context, tenant string, days int) (json.RawMessage, error) {
	if days != 1 && days != 7 && days != 30 {
		return nil, errors.New("invalid window")
	}
	// 所有聚合都在数据库按租户限界，禁止将问题或逐条执行内容返回管理员。
	var result json.RawMessage
	err := s.Pool.QueryRow(ctx, `WITH a AS (
        SELECT * FROM task_attempts WHERE tenant_id=$1 AND started_at>=NOW()-make_interval(days=>$2)
    ), t AS (SELECT * FROM tasks WHERE tenant_id=$1 AND created_at>=NOW()-make_interval(days=>$2)),
    state_counts AS (SELECT status,COUNT(*) n FROM t GROUP BY status),
    parsers AS (SELECT COALESCE(telemetry->>'parser','unknown') parser,COUNT(*) n FROM a GROUP BY 1),
    errors AS (SELECT error_code,COUNT(*) n FROM a WHERE error_code<>'' GROUP BY error_code),
    timings AS (
        SELECT 'queue' name,queue_ms ms FROM a
        UNION ALL SELECT 'call',call_ms FROM a WHERE call_ms IS NOT NULL
        UNION ALL SELECT 'attempt',EXTRACT(EPOCH FROM (ended_at-started_at))*1000 FROM a WHERE ended_at IS NOT NULL
        UNION ALL SELECT 'task',EXTRACT(EPOCH FROM (updated_at-created_at))*1000 FROM t WHERE status IN ('completed','needs_clarification','failed','canceled')
        UNION ALL SELECT j.key,(j.value::text)::double precision FROM a CROSS JOIN LATERAL jsonb_each(COALESCE(telemetry->'stages_ms','{}'::jsonb)) j
    ), distributions AS (SELECT name,COUNT(*) n,percentile_cont(0.5) WITHIN GROUP(ORDER BY ms) p50,percentile_cont(0.95) WITHIN GROUP(ORDER BY ms) p95 FROM timings GROUP BY name)
    SELECT jsonb_build_object('days',$2,'generated_at',NOW(),'available_attempts_from',(SELECT MIN(started_at) FROM task_attempts WHERE tenant_id=$1),
        'tasks',COALESCE((SELECT jsonb_object_agg(status,n) FROM state_counts),'{}'::jsonb),
        'attempts',(SELECT COUNT(*) FROM a),'retries',(SELECT COUNT(*) FROM a WHERE state='retry'),
        'parsers',COALESCE((SELECT jsonb_object_agg(parser,n) FROM parsers),'{}'::jsonb),
        'errors',COALESCE((SELECT jsonb_object_agg(error_code,n) FROM errors),'{}'::jsonb),
        'known_tokens',COALESCE((SELECT SUM((telemetry->>'total_tokens')::bigint) FROM a),0),
        'unknown_usage_attempts',(SELECT COUNT(*) FROM a WHERE telemetry IS NULL OR ((telemetry->>'model_calls')::int>0 AND telemetry->>'total_tokens' IS NULL)),
        'missing_telemetry_attempts',(SELECT COUNT(*) FROM a WHERE telemetry IS NULL),
        'model_calls',(SELECT SUM((telemetry->>'model_calls')::int) FROM a),
        'latency_ms',COALESCE((SELECT jsonb_object_agg(name,jsonb_build_object('samples',n,'p50',p50,'p95',p95)) FROM distributions),'{}'::jsonb))`, tenant, days).Scan(&result)
	return result, err
}

func (s *Store) PruneAttempts(ctx context.Context) (int64, error) {
	tag, err := s.Pool.Exec(ctx, `DELETE FROM task_attempts WHERE (task_id,execution_version) IN (SELECT task_id,execution_version FROM task_attempts WHERE ended_at<NOW()-INTERVAL '30 days' ORDER BY ended_at LIMIT 1000)`)
	return tag.RowsAffected(), err
}
