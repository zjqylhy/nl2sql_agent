package api

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"regexp"
	"strconv"
	"time"

	"nl2sql_agent/internal/store"
)

var requestIdentity = regexp.MustCompile(`^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$`)
var bindingIdentity = regexp.MustCompile(`^[a-f0-9]{64}$`)

func (s *Server) contextMetadata(ctx context.Context, u store.User) (store.ContextMetadata, error) {
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	body, _ := json.Marshal(map[string]string{"tenant_id": u.TenantID, "role": u.Role})
	req, err := http.NewRequestWithContext(ctx, "POST", s.Config.AgentURL+"/internal/context", bytes.NewReader(body))
	if err != nil {
		return store.ContextMetadata{}, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Service-Key", s.Config.ServiceKey)
	response, err := s.client.Do(req)
	if err != nil {
		return store.ContextMetadata{}, err
	}
	defer response.Body.Close()
	if response.StatusCode != 200 {
		if response.StatusCode == 403 {
			return store.ContextMetadata{}, store.ErrPermission
		}
		return store.ContextMetadata{}, errors.New("context service unavailable")
	}
	var meta store.ContextMetadata
	decoder := json.NewDecoder(io.LimitReader(response.Body, 4096))
	decoder.DisallowUnknownFields()
	if err = decoder.Decode(&meta); err != nil {
		return meta, err
	}
	if !bindingIdentity.MatchString(meta.Binding) || !bindingIdentity.MatchString(meta.CatalogHash) {
		return meta, store.ErrPermission
	}
	if _, err = time.Parse("2006-01-02", meta.AnalysisDate); err != nil {
		return meta, err
	}
	return meta, nil
}

func (s *Server) checkedConversation(ctx context.Context, u store.User, id string) (store.Conversation, error) {
	c, err := s.DB.Conversation(ctx, u, id)
	if err != nil {
		return c, err
	}
	if !store.ConversationAllowed(u, c) {
		return c, store.ErrPermission
	}
	meta, err := s.contextMetadata(ctx, u)
	if err != nil {
		return c, err
	}
	if c.Binding != meta.Binding {
		return c, store.ErrConflict
	}
	return c, nil
}

func conversationError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, store.ErrMissing):
		fail(w, 404, "会话不存在或无权访问")
	case errors.Is(err, store.ErrPermission):
		fail(w, 403, "权限已变化，请新建问数")
	case errors.Is(err, store.ErrConflict):
		fail(w, 409, "会话正在执行或版本已变化，请刷新；数据基准变化时请新建问数")
	case errors.Is(err, store.ErrTurnLimit):
		fail(w, 422, "已达到 30 轮，请新建问数")
	case errors.Is(err, store.ErrQueueFull):
		fail(w, 429, "任务队列已满")
	default:
		fail(w, 503, "会话服务暂时不可用")
	}
}

func (s *Server) createConversation(w http.ResponseWriter, r *http.Request) {
	var input struct{}
	if !decode(w, r, &input) {
		return
	}
	meta, err := s.contextMetadata(r.Context(), user(r))
	if err != nil {
		conversationError(w, err)
		return
	}
	c, err := s.DB.NewConversation(r.Context(), user(r), meta)
	if err != nil {
		conversationError(w, err)
		return
	}
	write(w, 201, c)
}

func (s *Server) conversations(w http.ResponseWriter, r *http.Request) {
	page := 0
	if value := r.URL.Query().Get("page"); value != "" {
		n, err := strconv.Atoi(value)
		if err != nil || n < 0 || n > 100000 {
			fail(w, 400, "页码无效")
			return
		}
		page = n
	}
	values, err := s.DB.Conversations(r.Context(), user(r), page)
	if err != nil {
		conversationError(w, err)
		return
	}
	meta, err := s.contextMetadata(r.Context(), user(r))
	if err != nil {
		conversationError(w, err)
		return
	}
	visible := []store.Conversation{}
	for _, c := range values {
		if store.ConversationAllowed(user(r), c) && c.Binding == meta.Binding {
			c.Title = "新的问数"
			if tasks, e := s.DB.ConversationTasks(r.Context(), user(r), c.ID); e == nil {
				for _, t := range tasks {
					if allowed(user(r), t) {
						c.Title = t.Input.Question
						break
					}
				}
			}
			visible = append(visible, c)
		}
	}
	write(w, 200, map[string]any{"conversations": visible, "page": page, "has_more": len(values) == 20})
}

func (s *Server) conversation(w http.ResponseWriter, r *http.Request) {
	c, err := s.checkedConversation(r.Context(), user(r), r.PathValue("id"))
	if err != nil {
		conversationError(w, err)
		return
	}
	tasks, err := s.DB.ConversationTasks(r.Context(), user(r), c.ID)
	if err != nil {
		conversationError(w, err)
		return
	}
	visible := []any{}
	for _, t := range tasks {
		if allowed(user(r), t) {
			visible = append(visible, t)
		} else {
			visible = append(visible, map[string]any{"id": t.ID, "conversation_turn": t.ConversationTurn, "status": "unavailable"})
		}
	}
	write(w, 200, map[string]any{"conversation": c, "tasks": visible})
}

func (s *Server) createTurn(w http.ResponseWriter, r *http.Request) {
	var input struct {
		RequestID       string `json:"request_id"`
		ExpectedVersion int    `json:"expected_version"`
		store.Input
	}
	if !decode(w, r, &input) {
		return
	}
	if !requestIdentity.MatchString(input.RequestID) || input.ExpectedVersion < 0 || !validateInput(input.Input) {
		fail(w, 400, "请求标识、版本或问题无效")
		return
	}
	if input.Metric != "" && !store.MetricsAllowed(json.RawMessage(`{"metrics":["`+input.Metric+`"]}`), user(r).AllowedMetrics) {
		fail(w, 403, "没有该指标权限")
		return
	}
	c, err := s.checkedConversation(r.Context(), user(r), r.PathValue("id"))
	if err != nil {
		conversationError(w, err)
		return
	}
	task, err := s.DB.CreateTurn(r.Context(), user(r), c.ID, input.RequestID, input.ExpectedVersion, input.Input, c.ContextMetadata)
	if err != nil {
		conversationError(w, err)
		return
	}
	write(w, 202, task)
}

func (s *Server) discardPending(w http.ResponseWriter, r *http.Request) {
	var input struct {
		ExpectedVersion int `json:"expected_version"`
	}
	if !decode(w, r, &input) {
		return
	}
	c, err := s.checkedConversation(r.Context(), user(r), r.PathValue("id"))
	if err != nil {
		conversationError(w, err)
		return
	}
	if err = s.DB.DiscardPending(r.Context(), user(r), c.ID, input.ExpectedVersion); err != nil {
		conversationError(w, err)
		return
	}
	c, err = s.checkedConversation(r.Context(), user(r), c.ID)
	if err != nil {
		conversationError(w, err)
		return
	}
	write(w, 200, c)
}

func (s *Server) taskContextAllowed(ctx context.Context, u store.User, t store.Task) bool {
	if t.Kind == "analysis" {
		_, err := s.checkedAnalysis(ctx, u, t.ID)
		return err == nil
	}
	if t.ConversationID == "" {
		return true
	}
	_, err := s.checkedConversation(ctx, u, t.ConversationID)
	return err == nil
}
