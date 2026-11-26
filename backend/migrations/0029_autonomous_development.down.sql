-- 0029_autonomous_development rollback (dev/test only; production uses expand/contract).

ALTER TABLE agent.egress_permits
  DROP COLUMN IF EXISTS body_sha256,
  DROP COLUMN IF EXISTS target_path;

ALTER TABLE agent.preview_leases
  DROP COLUMN IF EXISTS instance_generation,
  DROP COLUMN IF EXISTS preview_instance_id,
  DROP COLUMN IF EXISTS cycle_id;

DROP TABLE IF EXISTS agent.dev_debug_findings;
DROP TABLE IF EXISTS agent.dev_review_artifacts;
DROP TABLE IF EXISTS agent.dev_preview_instances;
DROP TABLE IF EXISTS agent.dev_pr_records;
DROP TABLE IF EXISTS agent.dev_design_decisions;
DROP TABLE IF EXISTS agent.dev_demo_artifacts;
DROP FUNCTION IF EXISTS agent.verify_dev_review_source_artifact(uuid, uuid, uuid, uuid, uuid);
DROP FUNCTION IF EXISTS agent.verify_dev_repository_binding(uuid, uuid, uuid, uuid, text, text);
DROP TABLE IF EXISTS agent.dev_projects;
