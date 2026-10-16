"""有界保留与等待过期；终态报告保留，详细证据和对应图线程一起清理。"""
import os

import psycopg
from psycopg.types.json import Jsonb


def prune(repo):
    with repo.db.transaction() as conn:
        expired = conn.execute("SELECT t.id FROM tasks t WHERE kind='analysis' AND status='needs_clarification' AND updated_at<now()-interval '24 hours' LIMIT 100 FOR UPDATE SKIP LOCKED").fetchall()
        for row in expired:
            conn.execute("UPDATE tasks SET status='failed',error='等待补充已超过 24 小时',execution_version=execution_version+1,lease_until=NULL WHERE id=%s", (row["id"],))
            conn.execute("UPDATE analysis_runs SET state=jsonb_set(jsonb_set(state,'{phase}','\"failed\"'),'{message}','\"等待补充已超过 24 小时\"'),version=version+1 WHERE id=%s", (row["id"],))
            conn.execute("UPDATE task_outbox SET delivered_at=now() WHERE task_id=%s", (row["id"],))
            repo.event(conn, row["id"], "error", {"status": "failed", "message": "等待补充已过期"})
        values = conn.execute("SELECT t.id,r.state FROM tasks t JOIN analysis_runs r ON r.id=t.id WHERE t.status IN ('completed','failed','canceled') AND t.updated_at<now()-interval '30 days' LIMIT 100 FOR UPDATE OF t SKIP LOCKED").fetchall()
        for row in values:
            usage = conn.execute("SELECT coalesce(sum(actual_tokens),0) known_tokens,coalesce(sum(actual_cost),0) known_cost_micros,count(*) FILTER(WHERE status IN ('reserved','unknown')) unknown_calls FROM analysis_budget_ledger WHERE run_id=%s", (row["id"],)).fetchone()
            state = row["state"]
            if "archived_usage" not in state:
                state["archived_usage"] = {k: int(v) for k, v in usage.items()}
            state["feedback"] = {"evidence_expired": True}
            conn.execute("UPDATE analysis_runs SET state=%s,resume_input=NULL WHERE id=%s", (Jsonb(state), row["id"]))
            conn.execute("DELETE FROM analysis_budget_ledger WHERE run_id=%s", (row["id"],))
            conn.execute("DELETE FROM analysis_step_attempts WHERE step_id IN (SELECT id FROM analysis_steps WHERE run_id=%s)", (row["id"],))
            conn.execute("DELETE FROM analysis_steps WHERE run_id=%s", (row["id"],))
            conn.execute("DELETE FROM analysis_answers WHERE run_id=%s", (row["id"],))
            conn.execute("DELETE FROM analysis_artifacts WHERE run_id=%s AND body->>'kind'<>'report'", (row["id"],))
        conn.execute("DELETE FROM sessions WHERE expires_at<now()")
        conn.execute("DELETE FROM login_windows WHERE started_at<now()-interval '1 day'")
        conn.execute("DELETE FROM task_attempts WHERE ended_at<now()-interval '30 days'")
    # 终态已拒绝所有 saver 写入；跨连接清理失败可重复执行，已归档用量不会归零。
    if values:
        with psycopg.connect(os.getenv("CHECKPOINT_DATABASE_URL", repo.db.dsn)) as conn:
            for row in values:
                for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                    conn.execute(f"DELETE FROM agent_checkpoint.{table} WHERE thread_id=%s", (row["id"],))
