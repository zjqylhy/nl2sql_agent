"""追加消息树、不可变回答版本、摘要来源与模型调用回执。"""
from alembic import op

revision = '0004_push_tree'
down_revision = '0003_query_clarification'


def upgrade():
    op.execute("""
    CREATE TABLE platform.push_threads(
        id text PRIMARY KEY, tenant_id text NOT NULL, user_id text NOT NULL,
        role text NOT NULL, binding text NOT NULL, version integer NOT NULL DEFAULT 0,
        root_task_id text NOT NULL UNIQUE REFERENCES platform.tasks(id),
        active_branch_id text, cursor_id text, active_task_id text REFERENCES platform.tasks(id),
        created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE platform.push_messages(
        id text PRIMARY KEY, thread_id text NOT NULL REFERENCES platform.push_threads(id),
        parent_message_id text, seq bigserial UNIQUE,
        role text NOT NULL CHECK(role IN ('user','assistant','tool')),
        kind text NOT NULL, content jsonb NOT NULL, task_id text REFERENCES platform.tasks(id),
        created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(thread_id,id),
        FOREIGN KEY(thread_id,parent_message_id) REFERENCES platform.push_messages(thread_id,id),
        CHECK(id IS DISTINCT FROM parent_message_id)
    );
    CREATE TABLE platform.push_branches(
        id text PRIMARY KEY, thread_id text NOT NULL REFERENCES platform.push_threads(id),
        fork_message_id text, head_message_id text NOT NULL, label text NOT NULL,
        FOREIGN KEY(thread_id,fork_message_id) REFERENCES platform.push_messages(thread_id,id),
        FOREIGN KEY(thread_id,head_message_id) REFERENCES platform.push_messages(thread_id,id),
        UNIQUE(thread_id,id)
    );
    ALTER TABLE platform.push_threads ADD FOREIGN KEY(id,active_branch_id) REFERENCES platform.push_branches(thread_id,id);
    ALTER TABLE platform.push_threads ADD FOREIGN KEY(id,cursor_id) REFERENCES platform.push_messages(thread_id,id);
    CREATE TABLE platform.push_revisions(
        message_id text PRIMARY KEY REFERENCES platform.push_messages(id),
        thread_id text NOT NULL REFERENCES platform.push_threads(id),
        parent_revision_id text REFERENCES platform.push_revisions(message_id),
        plan jsonb NOT NULL, presentation jsonb NOT NULL,
        result_task_id text NOT NULL REFERENCES platform.tasks(id), UNIQUE(thread_id,message_id),
        FOREIGN KEY(thread_id,message_id) REFERENCES platform.push_messages(thread_id,id)
    );
    CREATE TABLE platform.push_requests(
        thread_id text NOT NULL REFERENCES platform.push_threads(id), request_id text NOT NULL,
        body_hash text NOT NULL, task_id text REFERENCES platform.tasks(id), PRIMARY KEY(thread_id,request_id)
    );
    CREATE TABLE platform.push_model_calls(
        task_id text NOT NULL REFERENCES platform.tasks(id), round integer NOT NULL, sequence integer NOT NULL,
        kind text NOT NULL, status text NOT NULL, request_snapshot jsonb NOT NULL,
        reserved_tokens integer NOT NULL, response_content text, parsed_decision jsonb, usage jsonb,
        created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(task_id,round,sequence)
    );
    CREATE TABLE platform.push_summaries(
        id text PRIMARY KEY, thread_id text NOT NULL REFERENCES platform.push_threads(id),
        source_message_ids text[] NOT NULL, content text NOT NULL,
        task_id text NOT NULL REFERENCES platform.tasks(id), created_at timestamptz NOT NULL DEFAULT now()
    );
    ALTER TABLE platform.tasks ADD COLUMN push_thread_id text REFERENCES platform.push_threads(id);
    ALTER TABLE platform.tasks ADD COLUMN push_turn_context jsonb;
    CREATE INDEX push_message_tree ON platform.push_messages(thread_id,parent_message_id);
    CREATE FUNCTION platform.immutable_push_record() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'push history is immutable'; END $$;
    CREATE TRIGGER immutable_push_messages BEFORE UPDATE OR DELETE ON platform.push_messages
        FOR EACH ROW EXECUTE FUNCTION platform.immutable_push_record();
    CREATE TRIGGER immutable_push_revisions BEFORE UPDATE OR DELETE ON platform.push_revisions
        FOR EACH ROW EXECUTE FUNCTION platform.immutable_push_record();
    CREATE TRIGGER immutable_push_summaries BEFORE UPDATE OR DELETE ON platform.push_summaries
        FOR EACH ROW EXECUTE FUNCTION platform.immutable_push_record();
    CREATE FUNCTION platform.check_push_parent() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NEW.parent_message_id IS NOT NULL AND NOT EXISTS(
            SELECT 1 FROM platform.push_messages WHERE id=NEW.parent_message_id AND thread_id=NEW.thread_id AND seq<NEW.seq
        ) THEN RAISE EXCEPTION 'parent must be an existing ancestor'; END IF;
        RETURN NEW;
    END $$;
    CREATE TRIGGER check_push_parent BEFORE INSERT ON platform.push_messages
        FOR EACH ROW EXECUTE FUNCTION platform.check_push_parent();
    """)


def downgrade():
    raise RuntimeError('消息树与调用记录不可丢弃；回退应用保留追加结构')
