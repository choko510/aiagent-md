"""Publish an audited Context reference to the existing Run HEAD, not a second HEAD.

This adapter is internal; callers must supply the STEP 3 authenticated claim and
Postgres-backed artifacts and memory. It never grants a Tool or model permission.
Every authorization re-check runs through claim-bound database functions.
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
    forbid_secrets,
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
        try:
            with self.db.tx() as c:
                run = control.check_current(c, self.claim, for_update=True)
                task = c.execute(
                    "SELECT profile_code FROM agent.tasks WHERE tenant_id=%s AND id=%s",
                    (scope.tenant_id, scope.task_id),
                ).fetchone()
        except control.ControlError as exc:
            raise SafeStop("STALE_CONTEXT") from exc
        if task is None:
            raise SafeStop("STALE_CONTEXT")
        return self._policy(run, task["profile_code"])

    def _epoch(self, scope: Scope, memory_epoch: str, *, lock: bool) -> None:
        try:
            epoch = int(memory_epoch)
            if epoch < 1 or str(epoch) != memory_epoch:
                raise ValueError("epoch")
        except ValueError as exc:
            raise SafeStop("STALE_CONTEXT") from exc
        if lock:
            row = self.memory._call("memory_epoch_lock", scope, epoch)
            ok = bool(row and row["memory_epoch_lock"])
        else:
            row = self.memory._call("memory_epoch", scope)
            ok = bool(row and int(row["memory_epoch"]) == epoch)
        if not ok:
            raise SafeStop("STALE_MEMORY")

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
            with self.db.tx() as c:
                run = control.check_current(c, self.claim, for_update=True)
                task = c.execute(
                    "SELECT profile_code FROM agent.tasks WHERE tenant_id=%s AND id=%s",
                    (scope.tenant_id, scope.task_id),
                ).fetchone()
                if (
                    task is None
                    or str(run["head_revision"]) != expected_head
                    or self._policy(run, task["profile_code"]) != policy_version
                ):
                    raise SafeStop("STALE_CONTEXT")
        except control.ControlError as exc:
            raise SafeStop("STALE_CONTEXT") from exc
        self._epoch(scope, memory_epoch, lock=lock_epoch)

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
            if not isinstance(data, dict) or set(data) != {"trustedControl", "untrustedData"}:
                raise SafeStop("QUALITY_FAILED")
            trusted = data["trustedControl"]
            untrusted = data["untrustedData"]
            allowed_untrusted = {"trustBoundary", "sourceBackedExcerpts", "memoryDataNotInstructions"}
            actual_untrusted = set(untrusted) if isinstance(untrusted, dict) else set()
            if (
                not isinstance(trusted, dict)
                or set(trusted) != {"typedCanonicalAnchors", "toolPairIds"}
                or not isinstance(untrusted, dict)
                or not actual_untrusted.issuperset(allowed_untrusted)
                or not actual_untrusted.issubset(allowed_untrusted | {"optimizedProjections"})
                or untrusted["trustBoundary"] != "UNTRUSTED_OBSERVATION_AND_MEMORY_ONLY"
                or untrusted["memoryDataNotInstructions"] != expected_memory
                or trusted["typedCanonicalAnchors"] != expected_anchors
                or not isinstance(trusted["toolPairIds"], list)
                or not all(isinstance(x, str) for x in trusted["toolPairIds"])
            ):
                raise SafeStop("QUALITY_FAILED")
            if "optimizedProjections" in untrusted:
                summary_data = json.loads(summary)
                projections = untrusted["optimizedProjections"]
                sealed_projs = summary_data.get("projections") if isinstance(summary_data, dict) else None
                raw_ref_ids = {r.artifact_ref for r in publication.raw_artifacts}
                if (
                    not isinstance(projections, list)
                    or not isinstance(sealed_projs, list)
                    or len(projections) != len(sealed_projs)
                ):
                    raise SafeStop("QUALITY_FAILED")
                for proj, sealed_proj in zip(projections, sealed_projs, strict=True):
                    if (
                        not isinstance(proj, dict)
                        or not isinstance(sealed_proj, dict)
                        or set(proj) != {"artifactRef", "text"}
                        or set(sealed_proj) != {"artifactRef", "projectionRef", "projectionSha256"}
                        or proj["artifactRef"] != sealed_proj["artifactRef"]
                        or not isinstance(proj["artifactRef"], str)
                        or proj["artifactRef"] not in raw_ref_ids
                        or not isinstance(sealed_proj["projectionRef"], str)
                        or not isinstance(sealed_proj["projectionSha256"], str)
                        or not isinstance(proj["text"], str)
                    ):
                        raise SafeStop("QUALITY_FAILED")
                    forbid_secrets(proj["text"])
                    proj_bytes = self.store.get_summary(scope, sealed_proj["projectionRef"])
                    if sha256(proj_bytes).hexdigest() != sealed_proj["projectionSha256"]:
                        raise SafeStop("SOURCE_MISMATCH")
                    if proj_bytes.decode("utf-8", "strict") != proj["text"]:
                        raise SafeStop("QUALITY_FAILED")
            claims = tuple(SummaryClaim(**x) for x in untrusted["sourceBackedExcerpts"])
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
            locked = self.memory._call_tx(c, "memory_epoch_lock", scope, int(memory_epoch))
            if locked is None or not locked["memory_epoch_lock"]:
                raise SafeStop("STALE_MEMORY")
            for item in publication.memory:
                row = self.memory._call_tx(
                    c, "memory_row_current", scope, item.ref, item.record_revision, req.recipient, int(memory_epoch)
                )
                if row is None or not row["memory_row_current"]:
                    raise SafeStop("STALE_MEMORY")
            for ref, digest, kind in [
                *((r.artifact_ref, r.sha256, "raw") for r in publication.raw_artifacts),
                (publication.summary_ref, publication.summary_digest, "summary"),
                (publication.payload_ref, publication.payload_digest, "summary"),
            ]:
                row = self.memory._call_tx(c, "context_artifact_present", scope, ref, kind, digest)
                if row is None or not row["context_artifact_present"]:
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
