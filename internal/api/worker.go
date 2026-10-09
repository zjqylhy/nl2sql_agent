package api

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"nl2sql_agent/internal/store"
	"time"
)

func (s *Server) RunWorker(ctx context.Context) {
	ticker := time.NewTicker(250 * time.Millisecond)
	defer ticker.Stop()
	slots := make(chan struct{}, 50)
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			select {
			case slots <- struct{}{}:
			default:
				continue
			}
			t, err := s.DB.Claim(ctx)
			if err != nil {
				<-slots
				continue
			}
			go func() { defer func() { <-slots }(); s.execute(ctx, t) }()
		}
	}
}

func (s *Server) execute(ctx context.Context, t store.Task) {
	current, err := s.executionUser(ctx, t)
	if err != nil {
		s.finish(t, "failed", nil, "账号已停用或权限不可用")
		return
	}
	if !allowed(current, t) {
		s.finish(t, "failed", nil, "指标权限已撤销")
		return
	}
	raw, _ := json.Marshal(map[string]any{"tenant_id": current.TenantID, "role": current.Role, "allowed_metrics": current.AllowedMetrics, "input": t.Input})
	callCtx, cancel := context.WithTimeout(ctx, 35*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(callCtx, "POST", s.Config.AgentURL+"/internal/query", bytes.NewReader(raw))
	if err != nil {
		s.finish(t, "failed", nil, "分析服务配置错误")
		return
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Service-Key", s.Config.ServiceKey)
	// 停止信号跨 API 实例生效，及时中断正在等待的分析请求。
	monitorDone := make(chan struct{})
	defer close(monitorDone)
	monitorUser, monitorTask := current, t
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
				latest, e := s.DB.Task(callCtx, monitorUser, monitorTask.ID)
				if e != nil || latest.Status != "querying" || latest.Version != monitorTask.Version {
					cancel()
					return
				}
				updated, e := s.executionUser(callCtx, monitorTask)
				if e != nil || !allowed(updated, monitorTask) {
					cancel()
					return
				}
			}
		}
	}()
	response, err := s.client.Do(req)
	if err != nil {
		s.retry(t, "分析服务连接失败或执行超时")
		return
	}
	defer response.Body.Close()
	body, err := io.ReadAll(io.LimitReader(response.Body, 2*1024*1024+1))
	if err != nil || len(body) > 2*1024*1024 {
		s.finish(t, "failed", nil, "分析结果超出限制")
		return
	}
	if response.StatusCode != 200 {
		message := "分析服务暂时不可用"
		var failure struct {
			Detail string `json:"detail"`
		}
		if json.Unmarshal(body, &failure) == nil && failure.Detail != "" && len([]rune(failure.Detail)) <= 200 {
			message = failure.Detail
		}
		if response.StatusCode >= 500 {
			s.retry(t, message)
		} else {
			s.finish(t, "failed", nil, message)
		}
		return
	}
	var outcome struct {
		Status string `json:"status"`
	}
	if json.Unmarshal(body, &outcome) != nil || (outcome.Status != "completed" && outcome.Status != "needs_clarification") {
		s.finish(t, "failed", nil, "分析服务返回格式错误")
		return
	}
	t.Result = body
	current, err = s.executionUser(ctx, t)
	if err != nil || !allowed(current, t) {
		s.finish(t, "failed", nil, "指标权限已撤销")
		return
	}
	s.finish(t, outcome.Status, body, "")
}

func (s *Server) retry(t store.Task, message string) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := s.DB.Retry(ctx, t, message); err != nil {
		slog.Error("task retry persistence failed", "task_id", t.ID)
	}
}
func (s *Server) executionUser(ctx context.Context, t store.Task) (store.User, error) {
	var u store.User
	err := s.DB.Pool.QueryRow(ctx, `SELECT id,tenant_id,username,role,allowed_metrics FROM users WHERE id=$1 AND tenant_id=$2 AND enabled`, t.UserID, t.TenantID).Scan(&u.ID, &u.TenantID, &u.Username, &u.Role, &u.AllowedMetrics)
	return u, err
}
func (s *Server) finish(t store.Task, status string, body json.RawMessage, message string) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := s.DB.Finish(ctx, t, status, body, message); err != nil {
		slog.Error("task completion persistence failed", "task_id", t.ID)
	}
}
