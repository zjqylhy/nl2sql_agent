WITH a AS (
        SELECT * FROM task_attempts WHERE tenant_id=%(tenant)s AND started_at>=NOW()-make_interval(days=>%(days)s)
    ), t AS (SELECT * FROM tasks WHERE tenant_id=%(tenant)s AND created_at>=NOW()-make_interval(days=>%(days)s)),
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
    SELECT jsonb_build_object('days',%(days)s,'generated_at',NOW(),'available_attempts_from',(SELECT MIN(started_at) FROM task_attempts WHERE tenant_id=%(tenant)s),
        'tasks',COALESCE((SELECT jsonb_object_agg(status,n) FROM state_counts),'{}'::jsonb),
        'attempts',(SELECT COUNT(*) FROM a),'retries',(SELECT COUNT(*) FROM a WHERE state='retry'),
        'parsers',COALESCE((SELECT jsonb_object_agg(parser,n) FROM parsers),'{}'::jsonb),
        'errors',COALESCE((SELECT jsonb_object_agg(error_code,n) FROM errors),'{}'::jsonb),
        'known_tokens',COALESCE((SELECT SUM((telemetry->>'total_tokens')::bigint) FROM a),0),
        'unknown_usage_attempts',(SELECT COUNT(*) FROM a WHERE telemetry IS NULL OR ((telemetry->>'model_calls')::int>0 AND telemetry->>'total_tokens' IS NULL)),
        'missing_telemetry_attempts',(SELECT COUNT(*) FROM a WHERE telemetry IS NULL),
        'model_calls',(SELECT SUM((telemetry->>'model_calls')::int) FROM a),
        'latency_ms',COALESCE((SELECT jsonb_object_agg(name,jsonb_build_object('samples',n,'p50',p50,'p95',p95)) FROM distributions),'{}'::jsonb)) AS summary