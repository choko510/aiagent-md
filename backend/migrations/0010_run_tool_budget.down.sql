-- Revert 0010_run_tool_budget.

ALTER TABLE agent.runs
  DROP COLUMN IF EXISTS max_tool_calls;
