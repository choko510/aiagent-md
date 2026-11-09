-- 0003_framework_checkpoint: dedicated schema for the LangGraph PostgreSQL checkpointer (derived cache, ADR-038).
-- DDL = langgraph-checkpoint-postgres 3.2.0 MIGRATIONS[0..9] (indexes created non-concurrently inside this TX).
-- The runtime never calls saver.setup(): its checkpointer login has DML only on this schema, no DDL anywhere.
-- A library upgrade that adds MIGRATIONS entries fails test_framework_migrations_pinned until a new migration exists.
CREATE SCHEMA lg_checkpoint;

CREATE TABLE lg_checkpoint.checkpoint_migrations (v integer PRIMARY KEY);
CREATE TABLE lg_checkpoint.checkpoints (
  thread_id text NOT NULL,
  checkpoint_ns text NOT NULL DEFAULT '',
  checkpoint_id text NOT NULL,
  parent_checkpoint_id text,
  type text,
  checkpoint jsonb NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}',
  PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);
CREATE TABLE lg_checkpoint.checkpoint_blobs (
  thread_id text NOT NULL,
  checkpoint_ns text NOT NULL DEFAULT '',
  channel text NOT NULL,
  version text NOT NULL,
  type text NOT NULL,
  blob bytea,
  PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);
CREATE TABLE lg_checkpoint.checkpoint_writes (
  thread_id text NOT NULL,
  checkpoint_ns text NOT NULL DEFAULT '',
  checkpoint_id text NOT NULL,
  task_id text NOT NULL,
  idx integer NOT NULL,
  channel text NOT NULL,
  type text,
  blob bytea NOT NULL,
  task_path text NOT NULL DEFAULT '',
  PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);
CREATE INDEX checkpoints_thread_id_idx ON lg_checkpoint.checkpoints (thread_id);
CREATE INDEX checkpoint_blobs_thread_id_idx ON lg_checkpoint.checkpoint_blobs (thread_id);
CREATE INDEX checkpoint_writes_thread_id_idx ON lg_checkpoint.checkpoint_writes (thread_id);
INSERT INTO lg_checkpoint.checkpoint_migrations (v) SELECT generate_series(0, 9);

-- Retention / GC. Framework state of a run is garbage once the run is terminal (HEAD is canonical) or once its
-- thread is no longer the run's framework_ref (discarded derived cache). thread_id = '<run_id>:<nonce>'.
CREATE FUNCTION lg_checkpoint.gc(older_than interval) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, lg_checkpoint AS $$
DECLARE n integer := 0; m integer;
BEGIN
  CREATE TEMP TABLE IF NOT EXISTS _lg_gc(thread_id text PRIMARY KEY) ON COMMIT DROP;
  TRUNCATE _lg_gc;
  INSERT INTO _lg_gc
  SELECT DISTINCT c.thread_id FROM lg_checkpoint.checkpoints c
  LEFT JOIN agent.runs r ON r.id::text = split_part(c.thread_id, ':', 1)
  WHERE r.id IS NULL
     OR (r.framework_ref IS DISTINCT FROM c.thread_id AND r.updated_at < now() - older_than)
     OR (r.status_code IN ('SUCCEEDED','PARTIAL','FAILED','CANCELLED','EXPIRED','BUDGET_EXHAUSTED')
         AND r.updated_at < now() - older_than);
  DELETE FROM lg_checkpoint.checkpoint_writes w USING _lg_gc g WHERE w.thread_id = g.thread_id;
  DELETE FROM lg_checkpoint.checkpoint_blobs b USING _lg_gc g WHERE b.thread_id = g.thread_id;
  DELETE FROM lg_checkpoint.checkpoints c USING _lg_gc g WHERE c.thread_id = g.thread_id;
  GET DIAGNOSTICS m = ROW_COUNT; n := n + m;
  RETURN n;
END $$;
