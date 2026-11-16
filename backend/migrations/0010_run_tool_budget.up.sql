-- 0010_run_tool_budget: per-Run dedicated tool execution cap enforced atomically by Tool Gateway.

ALTER TABLE agent.runs
  ADD COLUMN IF NOT EXISTS max_tool_calls integer CHECK (max_tool_calls IS NULL OR max_tool_calls >= 1);
