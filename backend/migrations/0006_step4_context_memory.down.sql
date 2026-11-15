DROP FUNCTION IF EXISTS agent.gc_orphan_context_artifacts(interval);
DROP FUNCTION IF EXISTS agent.gc_expired_context_artifacts();
DROP FUNCTION IF EXISTS agent.memory_tombstone_coverage_read(uuid, uuid, uuid, uuid, bigint, text);
DROP FUNCTION IF EXISTS agent.memory_consent_lock(uuid, uuid, uuid, uuid, bigint, text, text);
DROP FUNCTION IF EXISTS agent.context_policy_lock(uuid, uuid, uuid, uuid, bigint, text);
DROP FUNCTION IF EXISTS agent.memory_consent_ok(uuid, uuid, uuid, uuid, bigint, text, text);
DROP FUNCTION IF EXISTS agent.context_dispatch_unresolved(uuid, uuid, uuid, uuid, bigint, text);
DROP FUNCTION IF EXISTS agent.context_policy(uuid, uuid, uuid, uuid, bigint, text);
DROP FUNCTION IF EXISTS agent.context_dispatch_finish(uuid, uuid, uuid, uuid, bigint, text, bigint, uuid, text);
DROP FUNCTION IF EXISTS agent.context_dispatch_record(uuid, uuid, uuid, uuid, bigint, text, bigint, uuid);
DROP FUNCTION IF EXISTS agent.memory_source_usable(uuid, uuid, uuid, uuid, bigint, text, uuid);
DROP FUNCTION IF EXISTS agent.memory_row(uuid, uuid, uuid, uuid, bigint, text, uuid);
DROP FUNCTION IF EXISTS agent.memory_row_current(uuid, uuid, uuid, uuid, bigint, text, uuid, bigint, text, bigint);
DROP FUNCTION IF EXISTS agent.memory_live_rows(uuid, uuid, uuid, uuid, bigint, text);
DROP FUNCTION IF EXISTS agent.memory_epoch_lock(uuid, uuid, uuid, uuid, bigint, text, bigint);
DROP FUNCTION IF EXISTS agent.memory_epoch(uuid, uuid, uuid, uuid, bigint, text);
DROP FUNCTION IF EXISTS agent.memory_artifact_ok(uuid, uuid);
DROP FUNCTION IF EXISTS agent.memory_restore_ready(uuid, uuid, uuid);
DROP FUNCTION IF EXISTS agent.context_artifact_revoke(uuid, uuid, uuid, uuid, bigint, text, uuid);
DROP FUNCTION IF EXISTS agent.context_artifact_live(uuid, uuid, uuid, uuid, bigint, text, uuid);
DROP FUNCTION IF EXISTS agent.context_artifact_present(uuid, uuid, uuid, uuid, bigint, text, uuid, text, text);
DROP FUNCTION IF EXISTS agent.context_artifact_fetch(uuid, uuid, uuid, uuid, bigint, text, uuid, text);
DROP FUNCTION IF EXISTS agent.context_artifact_put_user(uuid, uuid, uuid, uuid, uuid, text, text, integer, bytea, bytea, timestamptz, text);
DROP FUNCTION IF EXISTS agent.context_artifact_put(uuid, uuid, uuid, uuid, bigint, text, uuid, text, text, integer, bytea, bytea, timestamptz, text);
DROP FUNCTION IF EXISTS agent.memory_artifact_usable(uuid, uuid, uuid, uuid);

DROP TRIGGER IF EXISTS memory_consent_revision_append_only ON agent.memory_consent_revisions;
DROP TABLE IF EXISTS agent.memory_consent_revisions;
DROP TABLE IF EXISTS agent.task_context_policies;
DROP TABLE IF EXISTS agent.memory_consents;

DROP TRIGGER IF EXISTS context_dispatch_guard ON agent.context_dispatches;
DROP FUNCTION IF EXISTS agent.guard_context_dispatch();
DROP INDEX IF EXISTS agent.context_dispatch_unresolved_run;
DROP TABLE IF EXISTS agent.context_dispatches;

DROP TABLE IF EXISTS agent.memory_tombstone_coverage;
DROP TABLE IF EXISTS agent.memory_tombstone_watermarks;
DROP TABLE IF EXISTS agent.memory_restore_state;

DROP TRIGGER IF EXISTS memory_revision_append_only ON agent.memory_record_revisions;
DROP TABLE IF EXISTS agent.memory_record_revisions;
DROP TRIGGER IF EXISTS memory_record_guard ON agent.memory_records;
DROP FUNCTION IF EXISTS agent.guard_memory_record();
DROP TABLE IF EXISTS agent.memory_records;
DROP TRIGGER IF EXISTS memory_epoch_guard ON agent.memory_scope_epochs;
DROP FUNCTION IF EXISTS agent.guard_memory_epoch();
DROP TABLE IF EXISTS agent.memory_scope_epochs;

DROP TABLE IF EXISTS agent.context_artifacts;
DROP FUNCTION IF EXISTS agent.context_claim_valid(uuid, uuid, uuid, uuid, bigint, text);
DROP FUNCTION IF EXISTS agent.scope_active(uuid, uuid, uuid);
