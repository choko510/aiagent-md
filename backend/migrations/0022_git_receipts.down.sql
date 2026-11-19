DROP FUNCTION agent.git_operation_fetch(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid);
DROP FUNCTION agent.git_operation_finish(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid,uuid,bytea,bytea,text,text);
DROP FUNCTION agent.git_operation_receipt_lock(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid);
DROP TABLE agent.git_operation_receipts;
ALTER TABLE agent.git_operations DROP CONSTRAINT git_operations_state_check;
ALTER TABLE agent.git_operations ADD CONSTRAINT git_operations_state_check CHECK (state IN ('ADMITTED','UNKNOWN'));
