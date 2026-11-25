DROP FUNCTION agent.git_operation_finish_fenced(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid,uuid,bytea,bytea,text,text);
DROP FUNCTION agent.git_operation_enter_cas(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid,text,text,text);
ALTER TABLE agent.git_operations DROP CONSTRAINT git_cas_candidate_consistent;
ALTER TABLE agent.git_operations DROP COLUMN prepared_journal_sha256;
ALTER TABLE agent.git_operations DROP COLUMN candidate_tree;
ALTER TABLE agent.git_operations DROP COLUMN candidate_commit;
ALTER TABLE agent.git_operations DROP CONSTRAINT git_operations_state_check;
ALTER TABLE agent.git_operations ADD CONSTRAINT git_operations_state_check
  CHECK (state IN ('ADMITTED','RECEIPT_PERSISTED','UNKNOWN'));
