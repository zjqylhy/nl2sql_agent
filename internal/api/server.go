package api

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/netip"
	"net/url"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	"golang.org/x/crypto/bcrypt"
	"nl2sql_agent/internal/store"
)

type Config struct {
	Origin, AgentURL, ServiceKey string
	SecureCookie                 bool
	TrustedProxies               []netip.Prefix
}
type Server struct {
	DB      *store.Store
	Config  Config
	limiter loginLimiter
	client  *http.Client
}
type contextKey int

const principalKey contextKey = 1

var dummyHash, _ = bcrypt.GenerateFromPassword([]byte("invalid-login-placeholder"), bcrypt.DefaultCost)

func New(db *store.Store, c Config) *Server {
	return &Server{DB: db, Config: c, client: &http.Client{Timeout: 35 * time.Second}}
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /livez", func(w http.ResponseWriter, r *http.Request) { write(w, 200, map[string]string{"status": "ok"}) })
	mux.HandleFunc("GET /healthz", s.health)
	mux.HandleFunc("POST /api/login", s.login)
	mux.Handle("POST /api/logout", s.auth(http.HandlerFunc(s.logout)))
	mux.Handle("GET /api/me", s.auth(http.HandlerFunc(s.me)))
	mux.Handle("GET /api/metrics", s.auth(http.HandlerFunc(s.metrics)))
	mux.Handle("GET /api/operations/summary", s.auth(http.HandlerFunc(s.operations)))
	mux.Handle("POST /api/tasks", s.auth(http.HandlerFunc(s.create)))
	mux.Handle("GET /api/tasks", s.auth(http.HandlerFunc(s.list)))
	mux.Handle("GET /api/tasks/{id}", s.auth(http.HandlerFunc(s.detail)))
	mux.Handle("POST /api/tasks/{id}/stop", s.auth(http.HandlerFunc(s.stop)))
	mux.Handle("GET /api/tasks/{id}/events", s.auth(http.HandlerFunc(s.events)))
	mux.Handle("GET /api/users", s.auth(http.HandlerFunc(s.users)))
	mux.Handle("POST /api/users", s.auth(http.HandlerFunc(s.addUser)))
	mux.Handle("PATCH /api/users/{id}", s.auth(http.HandlerFunc(s.updateUser)))
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("Cache-Control", "no-store")
		// 同源写请求保护 Cookie 会话；不信任客户端提供的租户或角色。
		if r.Method != "GET" && r.Method != "HEAD" && !sameOrigin(r.Header.Get("Origin"), s.Config.Origin) {
			fail(w, 403, "请求来源不受信任")
			return
		}
		defer func() {
			if recovered := recover(); recovered != nil {
				slog.Error("HTTP handler panic")
				fail(w, 500, "服务暂时不可用")
			}
		}()
		mux.ServeHTTP(w, r)
	})
}

func sameOrigin(origin, expected string) bool {
	a, err := url.Parse(origin)
	if err != nil || a.Host == "" || a.User != nil {
		return false
	}
	b, err := url.Parse(expected)
	if err != nil {
		return false
	}
	return a.Scheme == b.Scheme && a.Host == b.Host && a.Path == "" && a.RawQuery == "" && a.Fragment == ""
}

func write(w http.ResponseWriter, code int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(value)
}
func fail(w http.ResponseWriter, code int, message string) {
	write(w, code, map[string]string{"error": message})
}
func decode(w http.ResponseWriter, r *http.Request, value any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, 16384)
	d := json.NewDecoder(r.Body)
	d.DisallowUnknownFields()
	if err := d.Decode(value); err != nil {
		fail(w, 400, "请求格式不正确")
		return false
	}
	if err := d.Decode(&struct{}{}); err != io.EOF {
		fail(w, 400, "请求格式不正确")
		return false
	}
	return true
}
func user(r *http.Request) store.User { return r.Context().Value(principalKey).(store.User) }

func (s *Server) auth(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := r.Cookie("session")
		if err != nil {
			fail(w, 401, "请先登录")
			return
		}
		u, err := s.DB.SessionUser(r.Context(), c.Value)
		if err != nil {
			fail(w, 401, "会话已失效")
			return
		}
		next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), principalKey, u)))
	})
}
func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
	defer cancel()
	if s.DB.Pool.Ping(ctx) != nil {
		fail(w, 503, "数据库不可用")
		return
	}
	write(w, 200, map[string]string{"status": "ok", "milestone": "M1"})
}
func (s *Server) login(w http.ResponseWriter, r *http.Request) {
	ip := s.clientIP(r)
	if !s.limiter.Allow(ip) {
		fail(w, 429, "登录尝试过多，请稍后重试")
		return
	}
	var body struct {
		Tenant   string `json:"tenant"`
		Username string `json:"username"`
		Password string `json:"password"`
	}
	if !decode(w, r, &body) {
		return
	}
	u, err := s.DB.LoginUser(r.Context(), body.Tenant, body.Username)
	hash := u.Hash
	if err != nil {
		hash = dummyHash
	}
	valid := bcrypt.CompareHashAndPassword(hash, []byte(body.Password)) == nil
	if err != nil || !valid {
		fail(w, 401, "租户、账号或密码不正确")
		return
	}
	token, err := s.DB.NewSession(r.Context(), u)
	if err != nil {
		fail(w, 503, "登录暂时不可用")
		return
	}
	http.SetCookie(w, &http.Cookie{Name: "session", Value: token, Path: "/", HttpOnly: true, Secure: s.Config.SecureCookie, SameSite: http.SameSiteStrictMode, MaxAge: 43200})
	write(w, 200, u)
}
func (s *Server) logout(w http.ResponseWriter, r *http.Request) {
	c, _ := r.Cookie("session")
	_, err := s.DB.Pool.Exec(r.Context(), `DELETE FROM sessions WHERE token_hash=$1`, store.TokenHash(c.Value))
	if err != nil {
		fail(w, 503, "退出失败，请重试")
		return
	}
	http.SetCookie(w, &http.Cookie{Name: "session", Path: "/", MaxAge: -1, HttpOnly: true, Secure: s.Config.SecureCookie, SameSite: http.SameSiteStrictMode})
	write(w, 200, map[string]bool{"ok": true})
}
func (s *Server) me(w http.ResponseWriter, r *http.Request) { write(w, 200, user(r)) }
func (s *Server) metrics(w http.ResponseWriter, r *http.Request) {
	list := []map[string]string{}
	names := []string{"GMV", "有效订单量", "客单价", "取消率", "平均交付时长", "平均评分"}
	for i, id := range store.Metrics {
		if slices.Contains(user(r).AllowedMetrics, id) {
			list = append(list, map[string]string{"id": id, "name": names[i], "version": "1"})
		}
	}
	write(w, 200, list)
}

func validateInput(in store.Input) bool {
	if strings.TrimSpace(in.Question) == "" || len([]rune(in.Question)) > 2000 {
		return false
	}
	if in.Metric != "" && !slices.Contains(store.Metrics, in.Metric) {
		return false
	}
	if in.GroupBy != "" && !slices.Contains([]string{"state", "day", "month"}, in.GroupBy) {
		return false
	}
	return true
}
func (s *Server) create(w http.ResponseWriter, r *http.Request) {
	var in store.Input
	if !decode(w, r, &in) {
		return
	}
	if !validateInput(in) {
		fail(w, 400, "请输入有效问题与指标")
		return
	}
	u := user(r)
	if in.Metric != "" && !slices.Contains(u.AllowedMetrics, in.Metric) {
		fail(w, 403, "没有该指标权限")
		return
	}
	t, err := s.DB.CreateTask(r.Context(), u, in)
	if errors.Is(err, store.ErrQueueFull) {
		fail(w, 429, "任务队列已满，请稍后再试")
		return
	}
	if err != nil {
		fail(w, 503, "任务受理失败")
		return
	}
	write(w, 202, t)
}

func allowed(u store.User, t store.Task) bool {
	metrics := []string{}
	if t.Input.Metric != "" {
		metrics = append(metrics, t.Input.Metric)
	}
	if len(t.Result) > 0 {
		var result struct {
			Metric        string `json:"metric"`
			ResultVersion int    `json:"result_version"`
			Metrics       []struct {
				ID string `json:"id"`
			} `json:"metrics"`
			Plan struct {
				Metrics []string `json:"metrics"`
			} `json:"plan"`
		}
		if json.Unmarshal(t.Result, &result) != nil {
			return false
		}
		if result.Metric != "" {
			metrics = append(metrics, result.Metric)
		}
		// 新结果检查元信息与计划的完整指标集合，兼容旧单指标历史。
		if result.ResultVersion == 2 && (len(result.Metrics) == 0 || len(result.Plan.Metrics) == 0) {
			return false
		}
		for _, item := range result.Metrics {
			metrics = append(metrics, item.ID)
		}
		metrics = append(metrics, result.Plan.Metrics...)
	}
	for _, metric := range metrics {
		if !slices.Contains(store.Metrics, metric) || !slices.Contains(u.AllowedMetrics, metric) {
			return false
		}
	}
	return true
}
func (s *Server) fetch(w http.ResponseWriter, r *http.Request) (store.Task, bool) {
	t, err := s.DB.Task(r.Context(), user(r), r.PathValue("id"))
	if err != nil || !allowed(user(r), t) {
		fail(w, 404, "任务不存在或无权访问")
		return store.Task{}, false
	}
	return t, true
}
func (s *Server) list(w http.ResponseWriter, r *http.Request) {
	tasks, err := s.DB.Tasks(r.Context(), user(r))
	if err != nil {
		fail(w, 503, "任务列表暂时不可用")
		return
	}
	visible := []store.Task{}
	for _, t := range tasks {
		if allowed(user(r), t) {
			visible = append(visible, t)
		}
	}
	write(w, 200, visible)
}
func (s *Server) detail(w http.ResponseWriter, r *http.Request) {
	if t, ok := s.fetch(w, r); ok {
		write(w, 200, t)
	}
}
func (s *Server) stop(w http.ResponseWriter, r *http.Request) {
	if _, ok := s.fetch(w, r); !ok {
		return
	}
	if err := s.DB.Stop(r.Context(), user(r), r.PathValue("id")); err != nil {
		fail(w, 503, "停止失败，请重试")
		return
	}
	write(w, 200, map[string]bool{"ok": true})
}

func (s *Server) events(w http.ResponseWriter, r *http.Request) {
	t, ok := s.fetch(w, r)
	if !ok {
		return
	}
	after := 0
	if value := r.Header.Get("Last-Event-ID"); value != "" {
		n, err := strconv.Atoi(value)
		if err != nil || n < 0 {
			fail(w, 400, "事件序号不正确")
			return
		}
		after = n
	}
	flusher, ok := w.(http.Flusher)
	if !ok {
		fail(w, 500, "不支持事件流")
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("X-Accel-Buffering", "no")
	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()
	heartbeat := time.NewTicker(10 * time.Second)
	defer heartbeat.Stop()
	for {
		// 长连接也重新加载权限，撤权后不继续发送历史数据。
		c, _ := r.Cookie("session")
		current, err := s.DB.SessionUser(r.Context(), c.Value)
		if err != nil {
			return
		}
		t, err = s.DB.Task(r.Context(), current, t.ID)
		if err != nil || !allowed(current, t) {
			return
		}
		events, err := s.DB.Events(r.Context(), t.ID, after)
		if err != nil {
			return
		}
		for _, e := range events {
			if _, err = fmt.Fprintf(w, "id: %d\nevent: %s\ndata: %s\n\n", e.Seq, e.Kind, e.Data); err != nil {
				return
			}
			after = e.Seq
		}
		flusher.Flush()
		if t.Status != "pending" && t.Status != "querying" {
			return
		}
		select {
		case <-r.Context().Done():
			return
		case <-ticker.C:
		case <-heartbeat.C:
			_, _ = io.WriteString(w, ": heartbeat\n\n")
			flusher.Flush()
		}
	}
}

func (s *Server) users(w http.ResponseWriter, r *http.Request) {
	u := user(r)
	if u.Role != "admin" {
		fail(w, 403, "需要管理员权限")
		return
	}
	rows, err := s.DB.Pool.Query(r.Context(), `SELECT id,tenant_id,username,role,allowed_metrics,enabled FROM users WHERE tenant_id=$1 ORDER BY username LIMIT 200`, u.TenantID)
	if err != nil {
		fail(w, 503, "暂时不可用")
		return
	}
	defer rows.Close()
	list := []map[string]any{}
	for rows.Next() {
		var item store.User
		var enabled bool
		if rows.Scan(&item.ID, &item.TenantID, &item.Username, &item.Role, &item.AllowedMetrics, &enabled) != nil {
			fail(w, 503, "暂时不可用")
			return
		}
		list = append(list, map[string]any{"user": item, "enabled": enabled})
	}
	if rows.Err() != nil {
		fail(w, 503, "暂时不可用")
		return
	}
	write(w, 200, list)
}
func validMetrics(metrics []string) bool {
	for _, m := range metrics {
		if !slices.Contains(store.Metrics, m) {
			return false
		}
	}
	return true
}
func (s *Server) addUser(w http.ResponseWriter, r *http.Request) {
	u := user(r)
	if u.Role != "admin" {
		fail(w, 403, "需要管理员权限")
		return
	}
	var body struct {
		Username string   `json:"username"`
		Password string   `json:"password"`
		Role     string   `json:"role"`
		Metrics  []string `json:"allowed_metrics"`
	}
	if !decode(w, r, &body) {
		return
	}
	if len(body.Username) < 2 || len(body.Username) > 100 || len(body.Password) < 12 || len(body.Password) > 72 || !slices.Contains([]string{"admin", "data", "ops"}, body.Role) || !validMetrics(body.Metrics) {
		fail(w, 400, "账号、密码、角色或指标权限不正确（密码 12–72 字节）")
		return
	}
	created, err := s.DB.AddUser(r.Context(), u.TenantID, body.Username, body.Password, body.Role, body.Metrics)
	if err != nil {
		fail(w, 409, "账号创建失败，请检查是否重复")
		return
	}
	write(w, 201, created)
}
func (s *Server) updateUser(w http.ResponseWriter, r *http.Request) {
	u := user(r)
	if u.Role != "admin" {
		fail(w, 403, "需要管理员权限")
		return
	}
	var body struct {
		Enabled bool     `json:"enabled"`
		Metrics []string `json:"allowed_metrics"`
	}
	if !decode(w, r, &body) {
		return
	}
	if !validMetrics(body.Metrics) {
		fail(w, 400, "指标权限不正确")
		return
	}
	if r.PathValue("id") == u.ID {
		fail(w, 400, "不能在此修改自身权限")
		return
	}
	if body.Metrics == nil {
		body.Metrics = []string{}
	}
	tag, err := s.DB.Pool.Exec(r.Context(), `UPDATE users SET enabled=$3,allowed_metrics=$4 WHERE tenant_id=$1 AND id=$2`, u.TenantID, r.PathValue("id"), body.Enabled, body.Metrics)
	if err != nil {
		fail(w, 503, "权限更新失败")
		return
	}
	if tag.RowsAffected() == 0 {
		fail(w, 404, "账号不存在")
		return
	}
	write(w, 200, map[string]bool{"ok": true})
}

type loginWindow struct {
	Start time.Time
	Count int
}
type loginLimiter struct {
	mu      sync.Mutex
	windows map[string]loginWindow
}

func (l *loginLimiter) Allow(key string) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	now := time.Now()
	if l.windows == nil {
		l.windows = map[string]loginWindow{}
	}
	if len(l.windows) > 10000 {
		for k, v := range l.windows {
			if now.Sub(v.Start) > time.Minute {
				delete(l.windows, k)
			}
		}
		if len(l.windows) > 10000 {
			return false
		}
	}
	v := l.windows[key]
	if now.Sub(v.Start) > time.Minute {
		v = loginWindow{Start: now}
	}
	v.Count++
	l.windows[key] = v
	return v.Count <= 10
}

// 内部服务令牌只在后端发送，浏览器和模型都不能获得。
func ValidServiceKey(got, want string) bool {
	return len(want) >= 32 && subtle.ConstantTimeCompare([]byte(got), []byte(want)) == 1
}
