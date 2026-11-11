-- A lost ACK may have caused a provider charge even when a new Context HEAD
-- is later created. Block *all* new Context admissions for that run until the
-- uncertain attempt has been reconciled by a future explicit operator flow.
CREATE UNIQUE INDEX context_dispatch_unresolved_run
 ON agent.context_dispatches(tenant_id,run_id)
 WHERE status_code IN ('ADMITTED','UNKNOWN');
