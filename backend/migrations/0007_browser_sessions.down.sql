DROP FUNCTION IF EXISTS agent.browser_session_revoke(uuid, uuid, uuid, uuid, uuid, bigint, bigint);
DROP FUNCTION IF EXISTS agent.browser_session_advance(uuid, uuid, uuid, uuid, uuid, bigint, bigint, uuid, bigint, bigint, text);
DROP FUNCTION IF EXISTS agent.browser_session_check(uuid, uuid, uuid, uuid, uuid, bigint, bigint, text);
DROP FUNCTION IF EXISTS agent.browser_session_register(uuid, uuid, uuid, uuid, uuid, bigint, bigint);
DROP INDEX IF EXISTS agent.browser_sessions_scope;
DROP TABLE IF EXISTS agent.browser_sessions;
