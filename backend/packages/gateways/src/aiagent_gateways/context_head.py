"""Publish an audited Context reference to the existing Run HEAD, not a second HEAD.

This adapter is internal; callers must supply the STEP 3 authenticated claim and
Postgres-backed artifacts and memory. It never grants a Tool or model permission.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from hashlib import sha256
from typing import Any

from aiagent_context.compaction import audit_proposal
from aiagent_context.models import (
    AnchorKind,
    ContextPlan,
    ContextPublication,
    SafeStop,
    Scope,
    SummaryClaim,
    SummaryProposal,
)
from aiagent_core import control
from aiagent_core.db import Database, Row
from aiagent_domain import sha256_hex

from .context_artifacts import PostgresArtifactStore
from .context_memory import PostgresMemoryRepository


class PostgresHeadPort:
    def __init__(
        self, db: Database, claim: control.Claim, store: PostgresArtifactStore, memory: PostgresMemoryRepository
    ):
        if claim != store._claim or db is not store._db or memory.store is not store:
            raise ValueError("context authorities must match")
        self.db, self.claim, self.store, self.memory = db, claim, store, memory

    @staticmethod
    def _policy(run: Row, profile: str) -> str:
        return sha256_hex(
            {"version": "context-v1", "profile": profile, "allowed_models": sorted(run["allowed_models"])}
        )

    def policy_version(self, scope: Scope) -> str:
        with self.db.tx() as c:
            self.memory._bind(c, scope)
            run = control.check_current(c, self.claim, for_update=True)
            task = c.execute(
                "SELECT profile_code FROM agent.tasks WHERE tenant_id=%s AND id=%s", (scope.tenant_id, scope.task_id)
            ).fetchone()
            if task is None:
                raise SafeStop("STALE_CONTEXT")
            return self._policy(run, task["profile_code"])

    def _assert(
        self,
        scope: Scope,
        expected_head: str,
        goal_revision: str,
        lease_epoch: str,
        policy_version: str,
        memory_epoch: str,
        *,
        lock_epoch: bool,
    ) -> None:
        if goal_revision != str(self.claim.goal_revision) or lease_epoch != str(self.claim.lease_epoch):
            raise SafeStop("STALE_CONTEXT")
        try:
            epoch = int(memory_epoch)
            if epoch < 1 or str(epoch) != memory_epoch:
                raise ValueError("epoch")
        except ValueError as exc:
            raise SafeStop("STALE_CONTEXT") from exc
        with self.db.tx() as c:
            self.memory._bind(c, scope)
            run = control.check_current(c, self.claim, for_update=True)
            task = c.execute(
                "SELECT profile_code FROM agent.tasks WHERE tenant_id=%s AND id=%s", (scope.tenant_id, scope.task_id)
            ).fetchone()
            if (
                task is None
                or str(run["head_revision"]) != expected_head
                or self._policy(run, task["profile_code"]) != policy_version
            ):
                raise SafeStop("STALE_CONTEXT")
            if lock_epoch:
                e = c.execute(
                    "SELECT agent.lock_memory_epoch(%s,%s,%s,%s) AS ok",
                    (scope.tenant_id, scope.owner_id, scope.task_id, epoch),
                ).fetchone()
            else:
                e = c.execute(
                    "SELECT source_epoch=%s AS ok FROM agent.memory_scope_epochs"
                    " WHERE tenant_id=%s AND owner_user_id=%s AND task_id=%s",
                    (epoch, scope.tenant_id, scope.owner_id, scope.task_id),
                ).fetchone()
            if e is None or not e["ok"]:
                raise SafeStop("STALE_MEMORY")

    def assert_current(
        self,
        scope: Scope,
        expected_head: str,
        goal_revision: str,
        lease_epoch: str,
        policy_version: str,
        memory_epoch: str,
    ) -> None:
        self._assert(scope, expected_head, goal_revision, lease_epoch, policy_version, memory_epoch, lock_epoch=False)

    def publish(
        self,
        scope: Scope,
        expected_head: str,
        goal_revision: str,
        lease_epoch: str,
        policy_version: str,
        memory_epoch: str,
        plan_digest: str,
        publication: ContextPublication,
    ) -> str:
        req = publication.request
        if (
            req.scope != scope
            or req.expected_head != expected_head
            or req.goal_revision != goal_revision
            or req.lease_epoch != lease_epoch
            or req.policy_version != policy_version
            or req.memory_source_epoch != memory_epoch
            or not req.recipient
            or len(plan_digest) != 64
        ):
            raise SafeStop("STALE_CONTEXT")
        try:
            payload = self.store.get_summary(scope, publication.payload_ref)
            summary = self.store.get_summary(scope, publication.summary_ref)
            if (
                sha256(payload).hexdigest() != publication.payload_digest
                or sha256(summary).hexdigest() != publication.summary_digest
            ):
                raise SafeStop("SOURCE_MISMATCH")
            data = json.loads(payload)
            expected_memory = json.loads(json.dumps([asdict(m) for m in publication.memory]))
            expected_anchors = [
                dict(kind=a.kind.value, sourceRef=a.source_ref, value=a.value, unit=a.unit, currency=a.currency)
                for a in req.anchors
            ]
            if (
                not isinstance(data, dict)
                or set(data)
                != {
                    "untrustedDataOnly",
                    "sourceBackedExcerpts",
                    "typedCanonicalAnchors",
                    "memoryDataNotInstructions",
                    "toolPairIds",
                }
                or data["untrustedDataOnly"] is not True
                or data["memoryDataNotInstructions"] != expected_memory
                or data["typedCanonicalAnchors"] != expected_anchors
            ):
                raise SafeStop("QUALITY_FAILED")
            claims = tuple(SummaryClaim(**x) for x in data["sourceBackedExcerpts"])
            audit_proposal(
                req,
                publication.raw_artifacts,
                self.store,
                SummaryProposal(
                    goal_revision, req.anchor_digest, tuple(r.artifact_ref for r in publication.raw_artifacts), claims
                ),
            )
        except (ValueError, TypeError, KeyError) as exc:
            raise SafeStop("QUALITY_FAILED") from exc
        if len({m.ref for m in publication.memory}) != len(publication.memory):
            raise SafeStop("QUALITY_FAILED")
        for item in publication.memory:
            record = self.memory.get(scope, item.ref)
            row = self.memory._row(scope, item.ref)
            if (
                record is None
                or row is None
                or row["recipient"] != req.recipient
                or item.source_epoch != memory_epoch
                or item.record_revision != record.revision
                or item.text != record.text
                or item.source_refs != record.source_refs
            ):
                raise SafeStop("STALE_MEMORY")
        with self.db.tx() as c:
            self.memory._bind(c, scope)
            run = control.check_current(c, self.claim, for_update=True)
            task = c.execute(
                "SELECT t.profile_code,g.goal_text FROM agent.tasks t JOIN agent.task_goal_revisions g"
                " ON g.tenant_id=t.tenant_id AND g.task_id=t.id AND g.revision=t.goal_revision"
                " WHERE t.tenant_id=%s AND t.id=%s",
                (scope.tenant_id, scope.task_id),
            ).fetchone()
            if (
                task is None
                or str(run["head_revision"]) != expected_head
                or self._policy(run, task["profile_code"]) != policy_version
                or req.budget.model_id not in run["allowed_models"]
            ):
                raise SafeStop("STALE_CONTEXT")
            # This slice has only a canonical unstructured goal; do not invent typed constraints.
            if (
                len(req.anchors) != 1
                or req.anchors[0].kind != AnchorKind.GOAL_REF
                or req.anchors[0].value != task["goal_text"]
                or req.anchors[0].source_ref != f"goal:{goal_revision}"
            ):
                raise SafeStop("INVALID_GOAL_ANCHOR")
            locked = c.execute(
                "SELECT agent.lock_memory_epoch(%s,%s,%s,%s) AS ok",
                (scope.tenant_id, scope.owner_id, scope.task_id, int(memory_epoch)),
            ).fetchone()
            if locked is None or not locked["ok"]:
                raise SafeStop("STALE_MEMORY")
            for item in publication.memory:
                row = c.execute(
                    "SELECT id FROM agent.memory_records WHERE tenant_id=%s AND owner_user_id=%s AND task_id=%s"
                    " AND id::text=%s AND revision=%s AND recipient=%s AND revoked_at IS NULL AND expires_at > clock_timestamp()"
                    " AND agent.memory_artifact_usable(tenant_id,owner_user_id,task_id,source_artifact_id)"
                    " AND agent.memory_artifact_usable(tenant_id,owner_user_id,task_id,projection_artifact_id)",
                    (scope.tenant_id, scope.owner_id, scope.task_id, item.ref, item.record_revision, req.recipient),
                ).fetchone()
                if row is None:
                    raise SafeStop("STALE_MEMORY")
            for ref, digest, kind in [
                *((r.artifact_ref, r.sha256, "raw") for r in publication.raw_artifacts),
                (publication.summary_ref, publication.summary_digest, "summary"),
                (publication.payload_ref, publication.payload_digest, "summary"),
            ]:
                row = c.execute(
                    "SELECT id FROM agent.context_artifacts WHERE tenant_id=%s AND owner_user_id=%s AND task_id=%s"
                    " AND id::text=%s AND kind=%s AND content_sha256=%s AND revoked_at IS NULL AND expires_at > clock_timestamp()",
                    (scope.tenant_id, scope.owner_id, scope.task_id, ref, kind, digest),
                ).fetchone()
                if row is None:
                    raise SafeStop("ARTIFACT_UNAVAILABLE")
            manifest: dict[str, Any] = {
                "kind": "context_manifest",
                "contextDigest": plan_digest,
                "payloadRef": publication.payload_ref,
                "payloadDigest": publication.payload_digest,
                "summaryRef": publication.summary_ref,
                "summaryDigest": publication.summary_digest,
                "rawRefs": [r.artifact_ref for r in publication.raw_artifacts],
                "memory": [{"id": m.ref, "revision": m.record_revision} for m in publication.memory],
                "sourceEpoch": memory_epoch,
                "policyVersion": policy_version,
                "recipient": req.recipient,
                "model": req.budget.model_id,
                "restoreIncarnation": str(self.claim.restore_incarnation),
            }
            revision = control.append_head(
                c, self.claim, "summary", manifest, writer="model_gateway", require_current_goal=True
            )
            return str(revision)

    def check_dispatch(self, plan: ContextPlan, lease_epoch: str) -> None:
        self._assert(
            plan.scope,
            plan.committed_head,
            plan.goal_revision,
            lease_epoch,
            plan.policy_version,
            plan.memory_source_epoch,
            lock_epoch=True,
        )
