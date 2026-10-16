package api

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"nl2sql_agent/internal/store"
)

func AnalysisPolicyFromEnv() (*store.AnalysisPolicy, error) {
	if os.Getenv("ANALYSIS_ENABLED") != "true" {
		return nil, nil
	}
	p := &store.AnalysisPolicy{Currency: os.Getenv("ANALYSIS_CURRENCY"), PriceVersion: os.Getenv("ANALYSIS_PRICE_VERSION"), Model: os.Getenv("LLM_MODEL"), Tokenizer: os.Getenv("ANALYSIS_TOKENIZER")}
	p.Strategy = os.Getenv("ANALYSIS_STRATEGY")
	var err error
	for _, v := range []struct {
		name string
		dst  *int64
	}{{"ANALYSIS_INPUT_PRICE_MICROS", &p.InputPrice}, {"ANALYSIS_OUTPUT_PRICE_MICROS", &p.OutputPrice}, {"ANALYSIS_MAX_COST_MICROS", &p.MaxCost}} {
		*v.dst, err = strconv.ParseInt(os.Getenv(v.name), 10, 64)
		if err != nil {
			return nil, errors.New("analysis pricing configuration invalid")
		}
	}
	if !p.Valid() {
		return nil, errors.New("analysis policy invalid")
	}
	return p, nil
}

func (s *Server) analysisCall(ctx context.Context, path string, body any) ([]byte, error) {
	raw, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, "POST", s.Config.AgentURL+"/internal/analysis/"+path, bytes.NewReader(raw))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Service-Key", s.Config.ServiceKey)
	response, err := s.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	data, err := io.ReadAll(io.LimitReader(response.Body, 2*1024*1024+1))
	if err != nil || len(data) > 2*1024*1024 || response.StatusCode != 200 {
		return nil, errors.New("analysis service response invalid")
	}
	return data, nil
}
func identity(u store.User) map[string]any {
	return map[string]any{"tenant_id": u.TenantID, "role": u.Role, "allowed_metrics": u.AllowedMetrics}
}

func (s *Server) analysisMetadata(ctx context.Context, u store.User) (store.AnalysisMetadata, error) {
	call, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	data, err := s.analysisCall(call, "context", identity(u))
	if err != nil {
		return store.AnalysisMetadata{}, err
	}
	var meta store.AnalysisMetadata
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&meta) != nil || !bindingIdentity.MatchString(meta.Binding) || !bindingIdentity.MatchString(meta.CatalogHash) || !bindingIdentity.MatchString(meta.DatasetRevision) || meta.RuntimeVersion != "analysis-1" || s.Config.Analysis == nil || meta.Model != s.Config.Analysis.Model {
		return meta, store.ErrPermission
	}
	if _, err = time.Parse("2006-01-02", meta.AnalysisDate); err != nil {
		return meta, err
	}
	return meta, nil
}

func (s *Server) checkedAnalysis(ctx context.Context, u store.User, id string) (store.AnalysisRun, error) {
	if s.Config.Analysis == nil {
		return store.AnalysisRun{}, store.ErrPermission
	}
	r, err := s.DB.Analysis(ctx, u, id)
	if err != nil {
		return r, err
	}
	meta, err := s.analysisMetadata(ctx, u)
	if err != nil {
		return r, err
	}
	if meta.Binding != r.Metadata.Binding {
		return r, store.ErrPermission
	}
	return r, nil
}

func (s *Server) analysisCapabilities(w http.ResponseWriter, r *http.Request) {
	enabled := s.Config.Analysis != nil
	if enabled {
		meta, err := s.analysisMetadata(r.Context(), user(r))
		enabled = err == nil && meta.ModelReady
	}
	write(w, 200, map[string]any{"enabled": enabled, "message": "经营分析需启用开关、冻结数据版本、模型及预算价格配置"})
}
func (s *Server) createAnalysis(w http.ResponseWriter, r *http.Request) {
	if s.Config.Analysis == nil {
		fail(w, 503, "经营分析尚未启用")
		return
	}
	var input struct {
		Question  string          `json:"question"`
		RequestID string          `json:"request_id"`
		Spec      json.RawMessage `json:"spec"`
	}
	if !decode(w, r, &input) {
		return
	}
	if !requestIdentity.MatchString(input.RequestID) || !validateInput(store.Input{Question: input.Question}) || len(input.Spec) > 8000 {
		fail(w, 400, "问题、请求标识或结构化条件无效")
		return
	}
	meta, err := s.analysisMetadata(r.Context(), user(r))
	if err != nil {
		conversationError(w, err)
		return
	}
	if !meta.ModelReady {
		fail(w, 503, "分析模型尚未配置")
		return
	}
	if len(input.Spec) > 0 && string(input.Spec) != "null" {
		body := identity(user(r))
		body["question"] = input.Question
		body["binding"] = meta.Binding
		body["spec"] = input.Spec
		raw, e := s.analysisCall(r.Context(), "validate", body)
		var result struct {
			Status string          `json:"status"`
			Spec   json.RawMessage `json:"spec"`
		}
		if e != nil || json.Unmarshal(raw, &result) != nil || result.Status != "completed" {
			fail(w, 400, "结构化条件不完整、超出范围或未授权")
			return
		}
		input.Spec = result.Spec
	}
	run, err := s.DB.CreateAnalysis(r.Context(), user(r), input.RequestID, input.Question, input.Spec, meta, *s.Config.Analysis)
	if err != nil {
		conversationError(w, err)
		return
	}
	write(w, 202, run)
}
func (s *Server) analysisDetail(w http.ResponseWriter, r *http.Request) {
	run, err := s.checkedAnalysis(r.Context(), user(r), r.PathValue("id"))
	if err != nil {
		conversationError(w, err)
		return
	}
	write(w, 200, run)
}
func (s *Server) analysisList(w http.ResponseWriter, r *http.Request) {
	if s.Config.Analysis == nil {
		write(w, 200, []any{})
		return
	}
	meta, err := s.analysisMetadata(r.Context(), user(r))
	if err != nil {
		conversationError(w, err)
		return
	}
	tasks, err := s.DB.Tasks(r.Context(), user(r))
	if err != nil {
		conversationError(w, err)
		return
	}
	result := []any{}
	for _, t := range tasks {
		if t.Kind != "analysis" || !allowed(user(r), t) {
			continue
		}
		run, e := s.DB.Analysis(r.Context(), user(r), t.ID)
		if e == nil && run.Metadata.Binding == meta.Binding {
			result = append(result, map[string]any{"id": run.ID, "question": run.Question, "status": run.Status, "version": run.Version})
		}
	}
	write(w, 200, result)
}
func (s *Server) answerAnalysis(w http.ResponseWriter, r *http.Request) {
	var input struct {
		RequestID       string `json:"request_id"`
		ExpectedVersion int    `json:"expected_version"`
		Answer          string `json:"answer"`
	}
	if !decode(w, r, &input) {
		return
	}
	if !requestIdentity.MatchString(input.RequestID) || input.ExpectedVersion < 0 || len(strings.TrimSpace(input.Answer)) == 0 || len([]rune(input.Answer)) > 1000 {
		fail(w, 400, "补充内容或版本无效")
		return
	}
	run, err := s.checkedAnalysis(r.Context(), user(r), r.PathValue("id"))
	if err != nil {
		conversationError(w, err)
		return
	}
	if err = s.DB.AnswerAnalysis(r.Context(), user(r), run.ID, input.RequestID, input.Answer, input.ExpectedVersion); err != nil {
		conversationError(w, err)
		return
	}
	run, err = s.checkedAnalysis(r.Context(), user(r), run.ID)
	if err != nil {
		conversationError(w, err)
		return
	}
	write(w, 202, run)
}
func (s *Server) resumeAnalysis(w http.ResponseWriter, r *http.Request) {
	var input struct {
		ExpectedVersion int `json:"expected_version"`
	}
	if !decode(w, r, &input) {
		return
	}
	run, err := s.checkedAnalysis(r.Context(), user(r), r.PathValue("id"))
	if err != nil {
		conversationError(w, err)
		return
	}
	// 过期租约由数据库自动恢复；手动恢复不能绕过重试等待、版本或终态。
	if input.ExpectedVersion != run.Version || run.Status != "pending" {
		conversationError(w, store.ErrConflict)
		return
	}
	write(w, 202, run)
}

func (s *Server) executeAnalysis(ctx context.Context, t store.Task) {
	started := time.Now()
	current, err := s.executionUser(ctx, t)
	if err != nil {
		_ = s.DB.FailAnalysis(ctx, t, "账号已停用")
		return
	}
	run, err := s.checkedAnalysis(ctx, current, t.ID)
	if err != nil {
		_ = s.DB.FailAnalysis(ctx, t, "权限、数据绑定或模型配置已变化")
		return
	}
	metaBefore, e := s.analysisMetadata(ctx, current)
	if e != nil || !metaBefore.ModelReady {
		_ = s.DB.FailAnalysis(ctx, t, "分析模型配置不可用")
		return
	}
	run, step, err := s.DB.BeginAnalysisStep(ctx, t)
	if err != nil {
		if errors.Is(err, store.ErrAnalysisBudget) {
			_ = s.DB.FailAnalysis(ctx, t, "分析预算或安全恢复额度已耗尽")
		}
		return
	}
	var out store.AnalysisOutcome
	issuedModel, bytesRead := false, 0
	defer func() {
		calls, zero := 0, int64(0)
		trace := &store.Telemetry{Version: 1, TaskID: t.ID, ExecutionVersion: t.Version, Parser: "unknown", Model: run.Policy.Model,
			ModelCalls: &calls, InputTokens: &zero, OutputTokens: &zero, TotalTokens: &zero, Stages: map[string]float64{}}
		if issuedModel && (out.ModelSent == nil || *out.ModelSent) {
			calls = 1
			trace.Parser = "llm"
			trace.InputTokens, trace.OutputTokens, trace.TotalTokens = nil, nil, nil
			if out.Usage != nil && out.Usage.Input != nil && out.Usage.Output != nil && *out.Usage.Input >= 0 && *out.Usage.Input <= 6000 && *out.Usage.Output >= 0 && *out.Usage.Output <= 1024 {
				total := *out.Usage.Input + *out.Usage.Output
				trace.InputTokens, trace.OutputTokens, trace.TotalTokens = out.Usage.Input, out.Usage.Output, &total
			}
		}
		code := out.ErrorCode
		if !store.ErrorCodes[code] {
			code = "internal"
		}
		persist, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if e := s.DB.RecordAttempt(persist, t, trace, time.Since(started), bytesRead, code); e != nil {
			slog.Error("analysis telemetry persistence failed", "task_id", t.ID, "execution_version", t.Version)
		}
	}()
	// Begin 只返回元数据；证据从已提交 artifact 表重新读取，避免未保存结果进入下一步。
	snapshot, err := s.DB.Analysis(ctx, current, t.ID)
	if err != nil {
		return
	}
	body := identity(current)
	body["binding"] = run.Metadata.Binding
	body["question"] = run.Question
	body["spec"] = run.State.Spec
	body["artifacts"] = snapshot.Artifacts
	body["feedback"] = run.State.Feedback
	body["answers"] = run.State.Answers
	path := "decide"
	if step.Phase == "fixed" {
		path = "fixed-decide"
	}
	if step.Phase == "tool" {
		path = "tool"
		body["action"] = run.State.Pending
	}
	callCtx, cancel := context.WithTimeout(ctx, 35*time.Second)
	defer cancel()
	monitorDone := make(chan struct{})
	defer close(monitorDone)
	go func() {
		tick := time.NewTicker(500 * time.Millisecond)
		defer tick.Stop()
		for {
			select {
			case <-monitorDone:
				return
			case <-callCtx.Done():
				return
			case <-tick.C:
				latest, e := s.DB.Task(callCtx, current, t.ID)
				if e != nil || latest.Status != "querying" || latest.Version != t.Version {
					cancel()
					return
				}
				fresh, e := s.executionUser(callCtx, t)
				if e != nil || !allowed(fresh, latest) {
					cancel()
					return
				}
			}
		}
	}()
	data := step.Cached
	if len(data) == 0 {
		issuedModel = step.Phase == "decide"
		data, err = s.analysisCall(callCtx, path, body)
	}
	bytesRead = len(data)
	if err != nil || json.Unmarshal(data, &out) != nil || (out.Status != "completed" && out.Status != "error" && out.Status != "needs_clarification") {
		code := "unknown_execution"
		message := "模型调用结果未知，任务已停止"
		if step.Phase == "tool" && run.State.Pending.Action == "query_metrics" {
			code = "sql_unknown"
			message = "只读查询回执未知"
		} else if step.Phase == "tool" {
			code = "agent_format"
			message = "工具未返回可验证结果"
		}
		out = store.AnalysisOutcome{Status: "error", ErrorCode: code, Message: message}
	}
	fresh, e := s.executionUser(ctx, t)
	if e != nil || !store.AnalysisAllowed(fresh, run) {
		_ = s.DB.FailAnalysis(ctx, t, "指标权限已撤销")
		return
	}
	meta, e := s.analysisMetadata(ctx, fresh)
	if e != nil || meta.Binding != run.Metadata.Binding {
		_ = s.DB.FailAnalysis(ctx, t, "数据或执行版本已变化")
		return
	}
	persist, stop := context.WithTimeout(context.Background(), 5*time.Second)
	defer stop()
	if e = s.DB.CompleteAnalysisStep(persist, t, step, out, time.Since(started)); e != nil {
		if errors.Is(e, store.ErrPermission) {
			_ = s.DB.FailAnalysis(persist, t, "指标权限已撤销")
		}
		// 持久化失败保持 running；不发下一调用，待租约过期后按 unknown 恢复。
	}
}
