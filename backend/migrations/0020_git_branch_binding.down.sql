DROP TABLE agent.git_patch_artifacts;
DROP FUNCTION agent.git_patch_artifact_guard();
ALTER TABLE agent.repository_bindings DROP CONSTRAINT repository_binding_branch_valid;
ALTER TABLE agent.repository_bindings DROP COLUMN approved_branch;
