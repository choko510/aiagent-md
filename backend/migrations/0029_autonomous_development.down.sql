-- 0029_autonomous_development rollback (dev/test only; production uses expand/contract).

DROP TABLE IF EXISTS agent.dev_debug_findings;
DROP TABLE IF EXISTS agent.dev_pr_records;
DROP TABLE IF EXISTS agent.dev_design_decisions;
DROP TABLE IF EXISTS agent.dev_projects;
