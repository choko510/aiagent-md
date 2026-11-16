-- Rollback 0012_web_sessions_and_notifications (development/test only).

ALTER TABLE agent.automation_watches
  DROP COLUMN IF EXISTS local_minute,
  DROP COLUMN IF EXISTS local_hour;

DROP TABLE IF EXISTS agent.notification_delivery_nonces;
DROP TABLE IF EXISTS agent.notification_attempts;
DROP TABLE IF EXISTS agent.notification_intents;
DROP TABLE IF EXISTS agent.notification_bindings;

DROP FUNCTION IF EXISTS agent.revoke_web_session(text);
DROP FUNCTION IF EXISTS agent.resolve_web_session(text);
DROP FUNCTION IF EXISTS agent.create_web_session(text, uuid, text, text, integer);
DROP TABLE IF EXISTS agent.web_sessions;
