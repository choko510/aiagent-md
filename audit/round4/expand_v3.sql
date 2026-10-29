BEGIN;
CREATE TABLE agent.source_attempt_receipts (
 tenant_id uuid NOT NULL, id uuid NOT NULL, owner_user_id uuid NOT NULL,
 source_key text NOT NULL, provider_attempt_key text NOT NULL,
 provider_ref text NOT NULL, outcome_code text NOT NULL CHECK
 (outcome_code IN ('success','partial','unavailable','restricted','failed','unknown')),
 fetched_at timestamptz NOT NULL DEFAULT now(), source_observed_at timestamptz,
 source_published_at timestamptz, evidence_id uuid, artifact_id uuid,
 scope_digest char(64) NOT NULL CHECK(scope_digest ~ '^[0-9a-f]{64}$'),
 coverage_code text NOT NULL CHECK(coverage_code IN ('complete','partial','unknown')),
 PRIMARY KEY(tenant_id,id),
 UNIQUE(tenant_id,source_key,provider_attempt_key),
 FOREIGN KEY(tenant_id,owner_user_id) REFERENCES agent.tenant_memberships(tenant_id,user_id),
 FOREIGN KEY(tenant_id,evidence_id) REFERENCES agent.evidence_records(tenant_id,id),
 FOREIGN KEY(tenant_id,artifact_id) REFERENCES agent.artifacts(tenant_id,id)
);
CREATE INDEX source_attempts_window ON agent.source_attempt_receipts(tenant_id,source_key,fetched_at DESC);
CREATE TABLE agent.source_profile_revisions (
 tenant_id uuid NOT NULL, id uuid NOT NULL, owner_user_id uuid NOT NULL,
 source_key text NOT NULL, window_start_at timestamptz NOT NULL,
 window_end_at timestamptz NOT NULL, profile_revision bigint NOT NULL CHECK(profile_revision>0),
 status_code text NOT NULL CHECK(status_code IN ('valid','insufficient','revoked','unknown')),
 metrics_json jsonb NOT NULL CHECK(jsonb_typeof(metrics_json)='object'),
 source_attempt_ids uuid[] NOT NULL DEFAULT '{}',
 calculated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,id),
 UNIQUE(tenant_id,source_key,window_start_at,window_end_at,profile_revision),
 FOREIGN KEY(tenant_id,owner_user_id) REFERENCES agent.tenant_memberships(tenant_id,user_id),
 CHECK(window_start_at < window_end_at)
);
-- Accepted series contains only comparable values; never overwrite baseline on failed fetch/304 mismatch.
CREATE TABLE agent.change_baselines (
 tenant_id uuid NOT NULL, id uuid NOT NULL, watch_id uuid NOT NULL,
 definition_revision bigint NOT NULL CHECK(definition_revision>0),
 scope_digest char(64) NOT NULL CHECK(scope_digest ~ '^[0-9a-f]{64}$'),
 field_key text NOT NULL, canonical_entity_id uuid,
 mapping_revision bigint,
 baseline_evidence_id uuid NOT NULL,
 source_attempt_id uuid NOT NULL,
 accepted_value_json jsonb NOT NULL, accepted_observed_at timestamptz NOT NULL,
 row_version bigint NOT NULL DEFAULT 1 CHECK(row_version>0),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,id),
 UNIQUE(tenant_id,watch_id,definition_revision,scope_digest,field_key),
 FOREIGN KEY(tenant_id,watch_id) REFERENCES agent.watches(tenant_id,id),
 FOREIGN KEY(tenant_id,canonical_entity_id) REFERENCES agent.canonical_entities(tenant_id,id),
 FOREIGN KEY(tenant_id,baseline_evidence_id) REFERENCES agent.evidence_records(tenant_id,id),
 FOREIGN KEY(tenant_id,source_attempt_id) REFERENCES agent.source_attempt_receipts(tenant_id,id),
 CHECK(mapping_revision IS NULL OR mapping_revision>0)
);
CREATE TABLE agent.discovery_plans (
 tenant_id uuid NOT NULL, id uuid NOT NULL, task_id uuid NOT NULL, run_id uuid NOT NULL,
 applied_goal_revision bigint NOT NULL CHECK(applied_goal_revision>0),
 plan_revision bigint NOT NULL DEFAULT 1 CHECK(plan_revision>0),
 status_code text NOT NULL CHECK(status_code IN
 ('created','active','paused','complete','partial','cancelled','failed')),
 plan_digest char(64) NOT NULL CHECK(plan_digest ~ '^[0-9a-f]{64}$'),
 budget_json jsonb NOT NULL CHECK(jsonb_typeof(budget_json)='object'),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id,task_id,run_id) REFERENCES agent.runs(tenant_id,task_id,id),
 FOREIGN KEY(tenant_id,task_id,applied_goal_revision)
 REFERENCES agent.task_goal_revisions(tenant_id,task_id,revision)
);
CREATE TABLE agent.discovery_slots (
 tenant_id uuid NOT NULL, id uuid NOT NULL, plan_id uuid NOT NULL,
 slot_key text NOT NULL, state_code text NOT NULL CHECK(state_code IN
 ('queued','running','completed','unknown','skipped','failed')),
 provider_cursor_ref text, attempt_count integer NOT NULL DEFAULT 0
 CHECK(attempt_count BETWEEN 0 AND 100),
 receipt_ref text, last_error_code text,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,id),
 UNIQUE(tenant_id,plan_id,slot_key),
 FOREIGN KEY(tenant_id,plan_id) REFERENCES agent.discovery_plans(tenant_id,id)
);
CREATE TABLE agent.opportunity_candidates (
 tenant_id uuid NOT NULL, id uuid NOT NULL, plan_id uuid NOT NULL,
 candidate_key text NOT NULL,
 canonical_entity_id uuid, entity_mapping_revision bigint,
 source_attempt_id uuid NOT NULL, evidence_id uuid NOT NULL,
 stage_code text NOT NULL CHECK(stage_code IN
 ('seen','needs_identity','needs_risk','verified_candidate','rejected','unknown')),
 reason_codes_json jsonb NOT NULL CHECK(jsonb_typeof(reason_codes_json)='array'),
 observed_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,id),
 UNIQUE(tenant_id,plan_id,candidate_key),
 FOREIGN KEY(tenant_id,plan_id) REFERENCES agent.discovery_plans(tenant_id,id),
 FOREIGN KEY(tenant_id,canonical_entity_id) REFERENCES agent.canonical_entities(tenant_id,id),
 FOREIGN KEY(tenant_id,source_attempt_id) REFERENCES agent.source_attempt_receipts(tenant_id,id),
 FOREIGN KEY(tenant_id,evidence_id) REFERENCES agent.evidence_records(tenant_id,id),
 CHECK(entity_mapping_revision IS NULL OR entity_mapping_revision>0)
);
CREATE TABLE agent.risk_decision_receipts (
 tenant_id uuid NOT NULL, id uuid NOT NULL, candidate_id uuid NOT NULL,
 engine_version text NOT NULL, input_digest char(64) NOT NULL
 CHECK(input_digest ~ '^[0-9a-f]{64}$'),
 expected_profit_minor_units bigint,
 currency_code char(3), uncertainty_code text NOT NULL CHECK
 (uncertainty_code IN ('verified','conditional','estimated','unknown')),
 scenario_json jsonb NOT NULL CHECK(jsonb_typeof(scenario_json)='object'),
 evidence_refs_json jsonb NOT NULL CHECK(jsonb_typeof(evidence_refs_json)='array'),
 calculated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,id),
 UNIQUE(tenant_id,candidate_id,engine_version,input_digest),
 FOREIGN KEY(tenant_id,candidate_id) REFERENCES agent.opportunity_candidates(tenant_id,id),
 CHECK(expected_profit_minor_units IS NULL OR currency_code IS NOT NULL)
);
CREATE TABLE agent.trend_receipts (
 tenant_id uuid NOT NULL, id uuid NOT NULL, owner_user_id uuid NOT NULL,
 canonical_entity_id uuid NOT NULL, mapping_revision bigint NOT NULL CHECK(mapping_revision>0),
 metric_key text NOT NULL, bucket_start_at timestamptz NOT NULL,
 bucket_end_at timestamptz NOT NULL, score_bps integer CHECK(score_bps BETWEEN 0 AND 10000),
 sample_count integer NOT NULL CHECK(sample_count>=0),
 independence_code text NOT NULL CHECK
 (independence_code IN ('independent','correlated','unknown')),
 coverage_code text NOT NULL CHECK(coverage_code IN ('complete','partial','unknown')),
 input_observation_refs_json jsonb NOT NULL CHECK(jsonb_typeof(input_observation_refs_json)='array'),
 source_profile_refs_json jsonb NOT NULL CHECK(jsonb_typeof(source_profile_refs_json)='array'),
 calculated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,id),
 UNIQUE(tenant_id,canonical_entity_id,mapping_revision,metric_key,bucket_start_at,bucket_end_at),
 FOREIGN KEY(tenant_id,owner_user_id) REFERENCES agent.tenant_memberships(tenant_id,user_id),
 FOREIGN KEY(tenant_id,canonical_entity_id) REFERENCES agent.canonical_entities(tenant_id,id),
 CHECK(bucket_start_at < bucket_end_at)
);
-- CHAT_06 is sole canonical Memory Writer; this is its proposed immutable feedback intent log.
CREATE TABLE agent.memory_feedback_mutations (
 tenant_id uuid NOT NULL, id uuid NOT NULL, owner_user_id uuid NOT NULL,
 idempotency_key text NOT NULL, target_entity_id uuid NOT NULL,
 target_mapping_revision bigint NOT NULL CHECK(target_mapping_revision>0),
 scope_code text NOT NULL CHECK(scope_code IN ('user','task','watch')),
 scope_ref uuid, user_consent_ref text NOT NULL,
 action_code text NOT NULL CHECK(action_code IN ('propose','activate','undo','reject')),
 preference_json jsonb NOT NULL CHECK(jsonb_typeof(preference_json)='object'),
 supersedes_mutation_id uuid, record_version bigint NOT NULL DEFAULT 1 CHECK(record_version>0),
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,id),
 UNIQUE(tenant_id,owner_user_id,idempotency_key),
 FOREIGN KEY(tenant_id,owner_user_id) REFERENCES agent.tenant_memberships(tenant_id,user_id),
 FOREIGN KEY(tenant_id,target_entity_id) REFERENCES agent.canonical_entities(tenant_id,id),
 FOREIGN KEY(tenant_id,supersedes_mutation_id)
 REFERENCES agent.memory_feedback_mutations(tenant_id,id) DEFERRABLE INITIALLY DEFERRED,
 CHECK((scope_code='user' AND scope_ref IS NULL) OR (scope_code<>'user' AND scope_ref IS NOT NULL))
);
COMMIT;
