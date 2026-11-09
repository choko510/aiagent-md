-- aiagent-md control-plane tables used by the AgentRuntimePort conformance suite.
-- This is a *test fixture* schema (PoC simplification of Checkpoint 02-06 / Tool Gateway 03 / Ledger),
-- not the production DDL. Production DDL stays owned by CHAT_10 (database-schema-and-migrations.md).
CREATE SCHEMA IF NOT EXISTS aiagent;

CREATE TABLE IF NOT EXISTS aiagent.runs (
  run_id         text PRIMARY KEY,
  parent_run_id  text,
  goal_revision  int    NOT NULL DEFAULT 1,
  fence          bigint NOT NULL DEFAULT 0,
  lease_owner    text,
  lease_until    timestamptz,
  head_seq       bigint NOT NULL DEFAULT 0,      -- checkpoint HEAD revision (CAS)
  status         text   NOT NULL DEFAULT 'QUEUED',
  framework_ref  text                            -- derived cache pointer (DBOS workflow id / LangGraph thread id)
);

-- Append-only canonical transcript (= Checkpoint HEAD contents). Framework state is a derived cache of this.
CREATE TABLE IF NOT EXISTS aiagent.head_entries (
  run_id        text   NOT NULL REFERENCES aiagent.runs(run_id),
  seq           bigint NOT NULL,
  kind          text   NOT NULL,                 -- user | assistant | tool | summary
  stream        text   NOT NULL DEFAULT 'main',  -- main | summarize | subagent:<id>
  request_key   text,                            -- identity of the request an assistant entry answers
  tool_call_id  text,
  payload       jsonb  NOT NULL,
  goal_revision int    NOT NULL,
  fence         bigint NOT NULL,
  PRIMARY KEY (run_id, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS head_assistant_once ON aiagent.head_entries(run_id, stream, request_key) WHERE kind = 'assistant';
CREATE UNIQUE INDEX IF NOT EXISTS head_tool_once ON aiagent.head_entries(run_id, tool_call_id) WHERE kind = 'tool';

CREATE TABLE IF NOT EXISTS aiagent.approvals (
  approval_id   text PRIMARY KEY,
  run_id        text NOT NULL,
  intent_key    text NOT NULL UNIQUE,
  args_hash     text NOT NULL,
  goal_revision int  NOT NULL,
  status        text NOT NULL DEFAULT 'PENDING',  -- PENDING | APPROVED | REJECTED
  decided_by    text,
  expires_at    timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS aiagent.tool_ledger (
  intent_key    text PRIMARY KEY,
  run_id        text NOT NULL,
  root_run_id   text NOT NULL,
  tool          text NOT NULL,
  args_hash     text NOT NULL,
  goal_revision int  NOT NULL,
  status        text NOT NULL,   -- DONE | PENDING_APPROVAL | SENDING | CONFIRMED | DENIED | EXPIRED | UNKNOWN
  result        jsonb,
  attempts      int  NOT NULL DEFAULT 1
);

-- The fake external world. A row here == an irreversible external side effect.
CREATE TABLE IF NOT EXISTS aiagent.external_sends (
  id          bigserial PRIMARY KEY,
  intent_key  text NOT NULL,
  payload     jsonb NOT NULL,
  sent_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS aiagent.model_calls (
  id            bigserial PRIMARY KEY,
  run_id        text NOT NULL,
  stream        text NOT NULL,
  goal_revision int  NOT NULL,
  admitted      boolean NOT NULL,
  reason        text,
  tools         text[],
  at            timestamptz NOT NULL DEFAULT now()
);

-- QueuePort fixture (used when the candidate brings no queue of its own).
CREATE TABLE IF NOT EXISTS aiagent.jobs (
  job_id       bigserial PRIMARY KEY,
  dedupe_key   text NOT NULL UNIQUE,
  run_id       text NOT NULL,
  status       text NOT NULL DEFAULT 'READY',  -- READY | CLAIMED | DONE
  attempts     int  NOT NULL DEFAULT 0,
  locked_by    text,
  locked_until timestamptz
);

-- Every write tool call observed by the gateway stub; used to detect framework-side parallel execution.
CREATE TABLE IF NOT EXISTS aiagent.tool_concurrency (
  run_id      text PRIMARY KEY,
  max_inflight_writes int NOT NULL DEFAULT 0
);
