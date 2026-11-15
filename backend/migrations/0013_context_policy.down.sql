DROP FUNCTION IF EXISTS agent.context_dispatch_unresolved(uuid, uuid, uuid, uuid, bigint, text);
DROP FUNCTION IF EXISTS agent.memory_consent_ok(uuid, uuid, uuid, uuid, bigint, text, text);
DROP FUNCTION IF EXISTS agent.context_policy(uuid, uuid, uuid, uuid, bigint, text);
DROP TABLE IF EXISTS agent.task_context_policies;
DROP TABLE IF EXISTS agent.memory_consents;
