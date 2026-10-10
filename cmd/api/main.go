package main

import (
	"bufio"
	"context"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"net/netip"
	"os"
	"os/signal"
	"regexp"
	"strings"
	"syscall"
	"time"

	"nl2sql_agent/internal/api"
	"nl2sql_agent/internal/store"
)

func run() error {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, nil)))
	command := flag.String("command", "serve", "serve / migrate / bootstrap / health / prune-attempts")
	tenant := flag.String("tenant", "", "tenant slug")
	username := flag.String("user", "", "username")
	role := flag.String("role", "admin", "admin / data / ops")
	flag.Parse()
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if *command == "health" {
		client := http.Client{Timeout: 2 * time.Second}
		response, err := client.Get("http://127.0.0.1:8080/healthz")
		if err != nil {
			return fmt.Errorf("health check failed")
		}
		defer response.Body.Close()
		if response.StatusCode != 200 {
			return fmt.Errorf("health check failed")
		}
		return nil
	}
	if os.Getenv("DATABASE_URL") == "" {
		return fmt.Errorf("DATABASE_URL is required")
	}
	db, err := store.Open(ctx, os.Getenv("DATABASE_URL"))
	if err != nil {
		return fmt.Errorf("platform database unavailable")
	}
	defer db.Pool.Close()
	switch *command {
	case "prune-attempts":
		n, err := db.PruneAttempts(ctx)
		if err != nil {
			return fmt.Errorf("attempt cleanup failed")
		}
		slog.Info("attempt cleanup finished", "deleted", n)
		return nil
	case "migrate":
		return db.Migrate(ctx)
	case "bootstrap":
		if !regexp.MustCompile(`^[a-z][a-z0-9_]{1,30}$`).MatchString(*tenant) || len(*username) < 2 || len(*username) > 100 || (*role != "admin" && *role != "data" && *role != "ops") {
			return fmt.Errorf("invalid tenant, username or role")
		}
		password, err := bufio.NewReader(os.Stdin).ReadString('\n')
		if err != nil && len(password) == 0 {
			return fmt.Errorf("password must be supplied on stdin")
		}
		password = strings.TrimRight(password, "\r\n")
		if len(password) < 12 || len(password) > 72 {
			return fmt.Errorf("password must have 12–72 bytes")
		}
		if _, err = db.Pool.Exec(ctx, `INSERT INTO tenants(id,name) VALUES($1,$1) ON CONFLICT DO NOTHING`, *tenant); err != nil {
			return fmt.Errorf("tenant creation failed")
		}
		u, err := db.AddUser(ctx, *tenant, *username, password, *role, store.Metrics)
		if err != nil {
			return fmt.Errorf("user creation failed")
		}
		fmt.Println("created user", u.Username, "in tenant", u.TenantID)
		return nil
	case "serve":
	default:
		return fmt.Errorf("unknown command")
	}
	origin := os.Getenv("PUBLIC_ORIGIN")
	agentURL := os.Getenv("AGENT_URL")
	key := os.Getenv("SERVICE_KEY")
	if origin == "" || agentURL == "" || len(key) < 32 {
		return fmt.Errorf("PUBLIC_ORIGIN, AGENT_URL and a 32-byte SERVICE_KEY are required")
	}
	secure := os.Getenv("COOKIE_SECURE") != "false"
	if secure && !strings.HasPrefix(origin, "https://") {
		return fmt.Errorf("secure cookie requires an HTTPS public origin")
	}
	var proxies []netip.Prefix
	if configured := os.Getenv("TRUSTED_PROXY_CIDRS"); configured != "" {
		for _, value := range strings.Split(configured, ",") {
			prefix, err := netip.ParsePrefix(strings.TrimSpace(value))
			if err != nil || prefix.Bits() == 0 {
				return fmt.Errorf("invalid trusted proxy CIDR")
			}
			proxies = append(proxies, prefix)
		}
	}
	app := api.New(db, api.Config{Origin: origin, AgentURL: agentURL, ServiceKey: key, SecureCookie: secure, TrustedProxies: proxies})
	address := os.Getenv("HTTP_ADDR")
	if address == "" {
		address = ":8080"
	}
	server := &http.Server{Addr: address, Handler: app.Handler(), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 1 << 16}
	go app.RunWorker(ctx)
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdown)
	}()
	slog.Info("API ready", "address", address)
	err = server.ListenAndServe()
	if err == http.ErrServerClosed {
		return nil
	}
	return err
}
func main() {
	if err := run(); err != nil {
		slog.Error("startup failed", "reason", err.Error())
		os.Exit(1)
	}
}
