// 独立评测入口只使用显式测试 DSN，并在独立 schema 建立临时身份。
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"nl2sql_agent/internal/api"
	"nl2sql_agent/internal/store"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"
)

func run() error {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))
	dsn, password, prefix := os.Getenv("TEST_DATABASE_URL"), os.Getenv("EVAL_PASSWORD"), os.Getenv("EVAL_PREFIX")
	if dsn == "" || len(password) < 12 || len(prefix) != 16 || strings.Trim(prefix, "0123456789abcdef") != "" {
		return fmt.Errorf("explicit test configuration required")
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	admin, err := store.Open(ctx, dsn)
	if err != nil {
		return fmt.Errorf("test database unavailable")
	}
	defer admin.Pool.Close()
	schema := "test_eval_" + prefix
	// Windows 强制结束子进程后仍可由父评测器清理精确匹配的临时 schema。
	if os.Getenv("EVAL_CLEANUP_ONLY") == "true" {
		_, err = admin.Pool.Exec(ctx, "DROP SCHEMA IF EXISTS "+schema+" CASCADE")
		return err
	}
	if _, err = admin.Pool.Exec(ctx, "CREATE SCHEMA "+schema); err != nil {
		return fmt.Errorf("test schema creation failed")
	}
	defer func() {
		cleanup, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_, _ = admin.Pool.Exec(cleanup, "DROP SCHEMA "+schema+" CASCADE")
	}()
	parsed, err := url.Parse(dsn)
	if err != nil {
		return fmt.Errorf("test DSN invalid")
	}
	q := parsed.Query()
	q.Set("search_path", schema)
	parsed.RawQuery = q.Encode()
	db, err := store.Open(ctx, parsed.String())
	if err != nil {
		return fmt.Errorf("test schema connection failed")
	}
	defer db.Pool.Close()
	if err = db.Migrate(ctx); err != nil {
		return fmt.Errorf("test migration failed")
	}
	for _, tenant := range []string{"ea_" + prefix, "eb_" + prefix} {
		if _, err = db.Pool.Exec(ctx, `INSERT INTO tenants VALUES($1,$1)`, tenant); err != nil {
			return err
		}
		for _, role := range []string{"admin", "ops"} {
			metrics := store.Metrics
			if role == "ops" {
				metrics = []string{"gmv"}
			}
			if _, err = db.AddUser(ctx, tenant, role, password, role, metrics); err != nil {
				return fmt.Errorf("test identity creation failed")
			}
		}
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return err
	}
	origin := "http://" + listener.Addr().String()
	app := api.New(db, api.Config{Origin: origin, AgentURL: os.Getenv("AGENT_URL"), ServiceKey: os.Getenv("SERVICE_KEY")})
	server := http.Server{Handler: app.Handler(), ReadHeaderTimeout: 5 * time.Second}
	go app.RunWorker(ctx)
	go func() {
		<-ctx.Done()
		deadline, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = server.Shutdown(deadline)
	}()
	// 父评测器仅接收访问地址，密码和 DSN 不进入输出。
	_ = json.NewEncoder(os.Stdout).Encode(map[string]string{"api_url": origin})
	if err = server.Serve(listener); err != nil && err != http.ErrServerClosed {
		return err
	}
	return nil
}
func main() {
	if err := run(); err != nil {
		slog.Error("evaluation fixture failed")
		os.Exit(1)
	}
}
