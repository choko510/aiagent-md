"""PostgreSQL persistence for Phase 7 Development application entities (0029) & Review Attestation.

Hardens P1-03, P1-06, P1-07, and P2-10:
- PostgresReviewAttestationPort requires a SUCCEEDED independent reviewer Run AND a persisted
  `agent.dev_review_artifacts` row matching (id, task_id, run_id, owner_user_id, project_id,
  reviewer_role, target_sha, passed).
- Design & Demo operations serialize on deterministic lock order `dev_cycles -> dev_design_decisions`;
  changing selected candidate ALWAYS invalidates prior Demo approval (`demo_decision='PENDING'`) and
  recomputes `demo_snapshot_digest` over (cycle, commit_sha, goal_revision, design_revision,
  selected_candidate_id, candidates_digest).
- `begin_pr_record` enforces uniqueness on BOTH `idempotency_key` AND `intent_id` atomically so an
  EFFECT_UNKNOWN or PENDING intent can NEVER be re-dispatched under a different idempotency key.
- `claim_guardian_repair_slot` atomically checks and increments `repair_attempts < max_repair_attempts`
  inside one PostgreSQL UPDATE, preventing concurrent overrun and preventing read-only checks from
  consuming repair budget.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from aiagent_core import control
from aiagent_core.db import Database
from aiagent_core.events import append_event
from aiagent_domain import canonical_json, is_uuid7, sha256_hex, uuid7
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from .browser_debugger import DebugReport
from .contracts import (
    InvalidEvidence,
    ProjectScope,
    ReviewFinding,
    ReviewVerdict,
    RunFence,
    ScopeDenied,
    StateConflict,
    require_digest,
    require_sha,
)
from .design_demo import (
    VALID_DEMO_DECISIONS,
    DevelopmentPreferences,
    normalize_design_candidates,
    redact_dev_text,
)
from .ports import ReviewAttestationPort
from .reproduction import ReproductionProof

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_FRAMEWORKS = frozenset({"web", "python", "node", "android", "desktop", "generic"})
_PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")


def _validate_branch(branch: str) -> str:
    if (
        not branch
        or branch.startswith(("-", "/", "."))
        or branch.endswith(("/", ".", ".lock"))
        or branch in ("HEAD", "@")
        or branch.startswith("refs/")
        or any(part.startswith(".") or part.endswith((".", ".lock")) for part in branch.split("/"))
        or any(c in branch for c in ("\\", " ", "~", "^", ":", "?", "*", "[", "@{", "..", "//"))
        or any(ord(c) < 33 or ord(c) == 127 for c in branch)
    ):
        raise InvalidEvidence("invalid branch name")
    return branch


def _validate_github_base(url: str | None) -> str | None:
    if url is None or not str(url).strip():
        return None
    val = str(url).strip().rstrip("/")
    try:
        pu = urlsplit(val)
        _ = pu.port
    except ValueError as exc:
        raise InvalidEvidence("invalid githubApiBase URL") from exc
    if pu.scheme not in ("http", "https") or not pu.hostname or pu.username or pu.password:
        raise InvalidEvidence("invalid githubApiBase URL")
    return val


def _demo_binding_digest(
    *,
    cycle_id: str,
    commit_sha: str | None,
    goal_revision: int,
    design_revision: int,
    selected_candidate_id: str | None,
    candidates: list[Any],
    demo_render_artifact_id: str | None = None,
    demo_artifact_ref: str | None = None,
    demo_artifact_sha256: str | None = None,
    demo_viewport: str | None = None,
    demo_fixture_ref: str | None = None,
) -> str:
    return sha256_hex(
        {
            "cycleId": cycle_id,
            "commitSha": commit_sha or "",
            "goalRevision": goal_revision,
            "designRevision": design_revision,
            "selectedCandidateId": selected_candidate_id or "",
            "demoRenderArtifactId": demo_render_artifact_id or "",
            "demoArtifactRef": demo_artifact_ref or "",
            "demoArtifactSha256": demo_artifact_sha256 or "",
            "demoViewport": demo_viewport or "",
            "demoFixtureRef": demo_fixture_ref or "",
            "candidates": candidates,
        }
    )


def _normalize_review_findings(findings: Sequence[Mapping[str, Any] | ReviewFinding]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in findings:
        if isinstance(item, ReviewFinding):
            fid, sev, st = item.finding_id, item.severity, item.status
        else:
            fid = str(item.get("findingId") or item.get("finding_id") or "").strip()
            sev = str(item.get("severity") or "").strip()
            st = str(item.get("status") or "").strip()
        if not fid or sev not in ("P0", "P1", "P2") or st not in ("OPEN", "RESOLVED", "UNVERIFIED"):
            raise InvalidEvidence("invalid review finding entry")
        out.append({"findingId": fid, "severity": sev, "status": st})
    return out


def _review_verdict_digest(
    *,
    cycle_id: str,
    project_id: str,
    task_id: str,
    run_id: str,
    reviewer_role: str,
    target_sha: str,
    passed: bool,
    findings: list[dict[str, str]],
    evidence_sha256: str,
) -> str:
    return sha256_hex(
        {
            "cycleId": cycle_id,
            "projectId": project_id,
            "taskId": task_id,
            "runId": run_id,
            "reviewerRole": reviewer_role,
            "targetSha": target_sha,
            "passed": bool(passed),
            "findings": findings,
            "evidenceSha256": evidence_sha256,
        }
    )


def _project_dto(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "workspaceId": str(row["workspace_id"]),
        "name": str(row["name"]),
        "repository": str(row["repository"]),
        "defaultBranch": str(row["default_branch"]),
        "framework": str(row["framework"]),
        "repositoryBindingId": str(row["repository_binding_id"]) if row.get("repository_binding_id") else None,
        "githubApiBase": str(row["github_api_base"]) if row.get("github_api_base") else None,
        "preferences": dict(row["preferences_json"]) if isinstance(row.get("preferences_json"), dict) else {},
        "status": str(row["status"]),
        "revision": int(row["revision"]),
        "createdAt": row["created_at"].isoformat(),
        "updatedAt": row["updated_at"].isoformat(),
    }


def _design_dto(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "projectId": str(row["project_id"]),
        "cycleId": str(row["cycle_id"]),
        "goalRevision": int(row["goal_revision"]),
        "designRevision": int(row["design_revision"]),
        "designPolicy": str(row["design_policy"]),
        "demoAcceptancePolicy": str(row["demo_acceptance_policy"]),
        "candidates": list(row["candidates_json"]) if isinstance(row.get("candidates_json"), list) else [],
        "recommendedCandidateId": str(row["recommended_candidate_id"]) if row.get("recommended_candidate_id") else None,
        "selectedCandidateId": str(row["selected_candidate_id"]) if row.get("selected_candidate_id") else None,
        "demoDecision": str(row["demo_decision"]),
        "demoRenderArtifactId": str(row["demo_render_artifact_id"]) if row.get("demo_render_artifact_id") else None,
        "demoArtifactRef": str(row["demo_artifact_ref"]) if row.get("demo_artifact_ref") else None,
        "demoArtifactSha256": str(row["demo_artifact_sha256"]) if row.get("demo_artifact_sha256") else None,
        "demoViewport": str(row["demo_viewport"]) if row.get("demo_viewport") else None,
        "demoFixtureRef": str(row["demo_fixture_ref"]) if row.get("demo_fixture_ref") else None,
        "demoCommitSha": str(row["demo_commit_sha"]) if row.get("demo_commit_sha") else None,
        "demoSnapshotDigest": str(row["demo_snapshot_digest"]) if row.get("demo_snapshot_digest") else None,
        "feedback": str(row["feedback"]) if row.get("feedback") else None,
        "decidedAt": row["decided_at"].isoformat() if row.get("decided_at") else None,
        "createdAt": row["created_at"].isoformat(),
        "updatedAt": row["updated_at"].isoformat(),
    }


def _pr_dto(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "projectId": str(row["project_id"]),
        "cycleId": str(row["cycle_id"]),
        "repository": str(row["repository"]),
        "branch": str(row["branch"]),
        "headSha": str(row["head_sha"]),
        "prNumber": int(row["pr_number"]) if row.get("pr_number") is not None else None,
        "prUrl": str(row["pr_url"]) if row.get("pr_url") else None,
        "idempotencyKey": str(row["idempotency_key"]),
        "intentId": str(row["intent_id"]) if row.get("intent_id") else None,
        "gitPushReceiptRef": str(row["git_push_receipt_ref"]) if row.get("git_push_receipt_ref") else None,
        "requiredChecks": list(row["required_checks_json"])
        if isinstance(row.get("required_checks_json"), list)
        else [],
        "effectStatus": str(row["effect_status"]),
        "ciState": str(row["ci_state"]),
        "repairAttempts": int(row["repair_attempts"]),
        "maxRepairAttempts": int(row["max_repair_attempts"]),
        "lastErrorCode": str(row["last_error_code"]) if row.get("last_error_code") else None,
        "createdAt": row["created_at"].isoformat(),
        "updatedAt": row["updated_at"].isoformat(),
    }


def _debug_dto(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "projectId": str(row["project_id"]),
        "cycleId": str(row["cycle_id"]),
        "previewLeaseId": str(row["preview_lease_id"]) if row.get("preview_lease_id") else None,
        "commitSha": str(row["commit_sha"]),
        "sourceUrlRedacted": str(row["source_url_redacted"]),
        "consoleErrors": list(row["console_errors_json"]) if isinstance(row.get("console_errors_json"), list) else [],
        "networkErrors": list(row["network_errors_json"]) if isinstance(row.get("network_errors_json"), list) else [],
        "layoutIssues": list(row["layout_issues_json"]) if isinstance(row.get("layout_issues_json"), list) else [],
        "reproTestId": str(row["repro_test_id"]) if row.get("repro_test_id") else None,
        "reproBeforeFailDigest": (
            str(row["repro_before_fail_digest"]) if row.get("repro_before_fail_digest") else None
        ),
        "reproAfterPassDigest": str(row["repro_after_pass_digest"]) if row.get("repro_after_pass_digest") else None,
        "traceArtifactId": str(row["trace_artifact_id"]) if row.get("trace_artifact_id") else None,
        "status": str(row["status"]),
        "createdAt": row["created_at"].isoformat(),
        "updatedAt": row["updated_at"].isoformat(),
    }


class PostgresReviewAttestationPort(ReviewAttestationPort):
    """Verifies independent SUCCEEDED reviewer Run + full persisted Review Artifact & Findings digest (P1-07 / R2-07)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def load_verified_verdict(
        self,
        scope: ProjectScope,
        builder_run: RunFence,
        *,
        cycle_id: str,
        artifact_id: str,
    ) -> ReviewVerdict:
        """Reconstruct authoritative ReviewVerdict (with ALL persisted findings) directly from attested DB artifact (R2-07)."""
        if not is_uuid7(artifact_id):
            raise InvalidEvidence("reviewer artifact_id must be a persisted dev_review_artifacts uuid7")
        with self._db.tx(tenant=scope.tenant_id) as c:
            row = c.execute(
                "SELECT a.id, a.task_id, a.run_id, a.owner_user_id, a.project_id, a.cycle_id,"
                " a.source_artifact_id, a.reviewer_role, a.target_sha, a.evidence_sha256, a.verdict_digest_sha256,"
                " a.passed, a.findings_json, t.workspace_id, r.status_code"
                " FROM agent.dev_review_artifacts a"
                " JOIN agent.tasks t ON t.tenant_id=a.tenant_id AND t.id=a.task_id"
                " JOIN agent.runs r ON r.tenant_id=a.tenant_id AND r.task_id=a.task_id AND r.id=a.run_id"
                " WHERE a.tenant_id=%s AND a.id=%s",
                (scope.tenant_id, artifact_id),
            ).fetchone()
            src_art = (
                c.execute(
                    "SELECT * FROM agent.verify_dev_review_source_artifact(%s,%s,%s,%s,%s)",
                    (scope.tenant_id, scope.owner_user_id, row["task_id"], row["run_id"], row["source_artifact_id"]),
                ).fetchone()
                if row is not None
                else None
            )
        if (
            row is None
            or src_art is None
            or str(src_art["content_sha256"]) != str(row["evidence_sha256"])
            or str(row["task_id"]) == builder_run.task_id
            or str(row["owner_user_id"]) != scope.owner_user_id
            or str(row["workspace_id"]) != scope.workspace_id
            or str(row["project_id"]) != scope.project_id
            or str(row["cycle_id"]) != cycle_id
            or str(row["status_code"]) != "SUCCEEDED"
        ):
            raise ScopeDenied(
                "review artifact missing, source artifact invalid/revoked, self-reviewed, or reviewer run did not SUCCEED"
            )
        raw_findings = list(row["findings_json"]) if isinstance(row["findings_json"], list) else []
        norm_findings = _normalize_review_findings(raw_findings)
        expected_digest = _review_verdict_digest(
            cycle_id=str(row["cycle_id"]),
            project_id=str(row["project_id"]),
            task_id=str(row["task_id"]),
            run_id=str(row["run_id"]),
            reviewer_role=str(row["reviewer_role"]),
            target_sha=str(row["target_sha"]),
            passed=bool(row["passed"]),
            findings=norm_findings,
            evidence_sha256=str(row["evidence_sha256"]),
        )
        if str(row["verdict_digest_sha256"]) != expected_digest:
            raise ScopeDenied("review artifact verdict digest mismatch")
        findings_tuple = tuple(ReviewFinding(f["findingId"], f["severity"], f["status"]) for f in norm_findings)
        return ReviewVerdict(
            reviewer_task_id=str(row["task_id"]),
            reviewer_role=str(row["reviewer_role"]),
            target_sha=str(row["target_sha"]),
            passed=bool(row["passed"]),
            evidence_ref=str(row["id"]),
            findings=findings_tuple,
        )

    def assert_verified(
        self,
        scope: ProjectScope,
        builder_run: RunFence,
        verdict: ReviewVerdict,
        *,
        cycle_id: str | None = None,
    ) -> None:
        require_sha(verdict.target_sha)
        if not verdict.reviewer_task_id or verdict.reviewer_task_id == builder_run.task_id:
            raise ScopeDenied("independent reviewer task required (self-review forbidden)")
        if not is_uuid7(verdict.reviewer_task_id):
            raise ScopeDenied("reviewer_task_id must be a canonical task uuid7")
        if not is_uuid7(verdict.evidence_ref):
            raise InvalidEvidence("reviewer evidence_ref must be a persisted dev_review_artifacts uuid7")

        with self._db.tx(tenant=scope.tenant_id) as c:
            row = c.execute(
                "SELECT a.task_id, a.run_id, a.owner_user_id, a.project_id, a.cycle_id, a.source_artifact_id,"
                " a.reviewer_role, a.target_sha, a.evidence_sha256, a.verdict_digest_sha256, a.passed, a.findings_json,"
                " t.workspace_id, r.status_code"
                " FROM agent.dev_review_artifacts a"
                " JOIN agent.tasks t ON t.tenant_id=a.tenant_id AND t.id=a.task_id"
                " JOIN agent.runs r ON r.tenant_id=a.tenant_id AND r.task_id=a.task_id AND r.id=a.run_id"
                " WHERE a.tenant_id=%s AND a.id=%s",
                (scope.tenant_id, verdict.evidence_ref),
            ).fetchone()
            src_art = (
                c.execute(
                    "SELECT * FROM agent.verify_dev_review_source_artifact(%s,%s,%s,%s,%s)",
                    (scope.tenant_id, scope.owner_user_id, row["task_id"], row["run_id"], row["source_artifact_id"]),
                ).fetchone()
                if row is not None
                else None
            )
            if (
                row is None
                or src_art is None
                or str(src_art["content_sha256"]) != str(row["evidence_sha256"])
                or str(row["task_id"]) != verdict.reviewer_task_id
                or str(row["owner_user_id"]) != scope.owner_user_id
                or str(row["workspace_id"]) != scope.workspace_id
                or str(row["project_id"]) != scope.project_id
                or (cycle_id is not None and str(row["cycle_id"]) != cycle_id)
                or str(row["reviewer_role"]) != verdict.reviewer_role
                or str(row["target_sha"]) != verdict.target_sha
                or bool(row["passed"]) != verdict.passed
                or str(row["status_code"]) != "SUCCEEDED"
            ):
                raise ScopeDenied(
                    "review artifact missing, source artifact invalid/revoked, mismatched, or reviewer run did not SUCCEED"
                )
            raw_findings = list(row["findings_json"]) if isinstance(row["findings_json"], list) else []
            norm_stored = _normalize_review_findings(raw_findings)
            expected_digest = _review_verdict_digest(
                cycle_id=str(row["cycle_id"]),
                project_id=str(row["project_id"]),
                task_id=str(row["task_id"]),
                run_id=str(row["run_id"]),
                reviewer_role=str(row["reviewer_role"]),
                target_sha=str(row["target_sha"]),
                passed=bool(row["passed"]),
                findings=norm_stored,
                evidence_sha256=str(row["evidence_sha256"]),
            )
            if str(row["verdict_digest_sha256"]) != expected_digest:
                raise ScopeDenied("review artifact verdict digest mismatch")
            norm_submitted = _normalize_review_findings(verdict.findings)
            if norm_submitted != norm_stored:
                raise ScopeDenied("submitted ReviewVerdict findings do not match persisted review artifact")


class PostgresDevStore:
    """Owner- and tenant-scoped persistence for DEV-01..DEV-14 APIs and UI projections."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------------ Projects
    def create_project(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        workspace_id: str,
        project_id: str | None,
        name: str,
        repository: str,
        default_branch: str = "main",
        framework: str = "web",
        repository_binding_id: str | None = None,
        github_api_base: str | None = None,
        preferences: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        pid = (project_id or f"proj-{uuid7()[:12]}").strip().lower()
        if not _PROJECT_ID_RE.fullmatch(pid):
            raise InvalidEvidence("invalid projectId format")
        clean_name = redact_dev_text(name.strip(), limit=120)
        if not clean_name:
            raise InvalidEvidence("project name is required")
        if not _REPO_RE.fullmatch(repository) or any(part in (".", "..") for part in repository.split("/")):
            raise InvalidEvidence("repository must be owner/repo")
        branch = _validate_branch(default_branch.strip())
        fw = framework.strip().lower()
        if fw not in _FRAMEWORKS:
            raise InvalidEvidence(f"unsupported framework: {fw}")
        if repository_binding_id is not None and not is_uuid7(repository_binding_id):
            raise InvalidEvidence("repositoryBindingId must be uuid7")
        gh_base = _validate_github_base(github_api_base)
        prefs = DevelopmentPreferences.from_raw(preferences)

        try:
            with self._db.tx(tenant=tenant_id) as c:
                if not control.scope_active(c, tenant_id, owner_user_id, workspace_id):
                    raise ScopeDenied("workspace scope inactive")
                if repository_binding_id is not None:
                    # R2-02: verify binding belongs to (tenant, owner, workspace, repository) and is active
                    bound = c.execute(
                        "SELECT * FROM agent.verify_dev_repository_binding(%s,%s,%s,%s,%s,NULL)",
                        (tenant_id, owner_user_id, workspace_id, repository_binding_id, repository),
                    ).fetchone()
                    if bound is None:
                        raise ScopeDenied("REPOSITORY_BINDING_UNAUTHORIZED")
                row = c.execute(
                    "INSERT INTO agent.dev_projects(tenant_id, id, owner_user_id, workspace_id, name,"
                    " repository, default_branch, framework, repository_binding_id, github_api_base,"
                    " preferences_json)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                    " RETURNING *",
                    (
                        tenant_id,
                        pid,
                        owner_user_id,
                        workspace_id,
                        clean_name,
                        repository,
                        branch,
                        fw,
                        repository_binding_id,
                        gh_base,
                        Jsonb(prefs.to_dict()),
                    ),
                ).fetchone()
        except UniqueViolation as exc:
            raise StateConflict("project or repository/branch already exists in workspace") from exc
        assert row is not None
        return _project_dto(dict(row))

    def list_projects(self, *, tenant_id: str, owner_user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        lim = max(1, min(limit, 200))
        with self._db.tx(tenant=tenant_id) as c:
            rows = c.execute(
                "SELECT * FROM agent.dev_projects"
                " WHERE tenant_id=%s AND owner_user_id=%s"
                " ORDER BY created_at DESC LIMIT %s",
                (tenant_id, owner_user_id, lim),
            ).fetchall()
        return [_project_dto(dict(r)) for r in rows]

    def get_project(self, *, tenant_id: str, owner_user_id: str, project_id: str) -> dict[str, Any] | None:
        with self._db.tx(tenant=tenant_id) as c:
            row = c.execute(
                "SELECT * FROM agent.dev_projects WHERE tenant_id=%s AND id=%s AND owner_user_id=%s",
                (tenant_id, project_id, owner_user_id),
            ).fetchone()
        return _project_dto(dict(row)) if row is not None else None

    # ------------------------------------------------------------------ Review Evidence Artifacts (R2-07 / R3-06)
    def record_review_artifact(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        project_id: str,
        cycle_id: str,
        task_id: str,
        run_id: str,
        source_artifact_id: str,
        evidence_payload: Mapping[str, Any] | bytes,
    ) -> str:
        """Materialize a verified dev_review_artifacts row strictly from a persisted context_artifacts payload (R3-06).

        Refuses self-declared hashes without raw payload AND refuses payloads that do not match the reviewer run's
        persisted `agent.context_artifacts` row (byte_length + content_sha256). Derives `reviewerRole`, `targetSha`,
        `passed`, and `findings` authoritatively from the verified payload itself.
        """
        if not is_uuid7(source_artifact_id):
            raise InvalidEvidence("source_artifact_id must be a persisted context_artifacts uuid7")
        if isinstance(evidence_payload, (bytes, bytearray)):
            raw_bytes = bytes(evidence_payload)
            try:
                doc = json.loads(raw_bytes.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise InvalidEvidence("review evidence_payload must be valid UTF-8 JSON") from exc
        elif isinstance(evidence_payload, Mapping):
            doc = dict(evidence_payload)
            raw_bytes = canonical_json(doc).encode("utf-8")
        else:
            raise InvalidEvidence("evidence_payload bytes or JSON mapping is required")
        if not isinstance(doc, dict):
            raise InvalidEvidence("review evidence payload must be a JSON object")

        computed_sha = hashlib.sha256(raw_bytes).hexdigest()
        reviewer_role = str(doc.get("reviewerRole") or "").strip()
        target_sha = str(doc.get("targetSha") or "").strip()
        passed = doc.get("passed")
        raw_findings = doc.get("findings", ())
        require_sha(target_sha)
        if reviewer_role not in ("functional", "security", "ui-e2e"):
            raise InvalidEvidence("invalid reviewerRole in review artifact payload")
        if type(passed) is not bool:
            raise InvalidEvidence("passed boolean is required in review artifact payload")
        if not isinstance(raw_findings, (list, tuple)):
            raise InvalidEvidence("findings list is required in review artifact payload")

        norm_findings = _normalize_review_findings(raw_findings)
        if passed and any(
            f["severity"] in ("P0", "P1") and f["status"] in ("OPEN", "UNVERIFIED") for f in norm_findings
        ):
            raise InvalidEvidence("cannot record passed=True review artifact with open/unverified P0 or P1 findings")
        verdict_digest = _review_verdict_digest(
            cycle_id=cycle_id,
            project_id=project_id,
            task_id=task_id,
            run_id=run_id,
            reviewer_role=reviewer_role,
            target_sha=target_sha,
            passed=passed,
            findings=norm_findings,
            evidence_sha256=computed_sha,
        )
        art_id = uuid7()
        with self._db.tx(tenant=tenant_id) as c:
            src_art = c.execute(
                "SELECT * FROM agent.verify_dev_review_source_artifact(%s,%s,%s,%s,%s)",
                (tenant_id, owner_user_id, task_id, run_id, source_artifact_id),
            ).fetchone()
            if (
                src_art is None
                or str(src_art["content_sha256"]) != computed_sha
                or int(src_art["byte_length"]) != len(raw_bytes)
            ):
                raise ScopeDenied(
                    "SOURCE_ARTIFACT_MISMATCH: review payload must exactly match reviewer run's context_artifacts entry"
                )
            c.execute(
                "INSERT INTO agent.dev_review_artifacts(tenant_id, id, task_id, run_id, owner_user_id,"
                " project_id, cycle_id, source_artifact_id, reviewer_role, target_sha, evidence_sha256,"
                " verdict_digest_sha256, passed, findings_json)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    tenant_id,
                    art_id,
                    task_id,
                    run_id,
                    owner_user_id,
                    project_id,
                    cycle_id,
                    source_artifact_id,
                    reviewer_role,
                    target_sha,
                    computed_sha,
                    verdict_digest,
                    passed,
                    Jsonb(norm_findings),
                ),
            )
        return art_id

    # ------------------------------------------------------------------ Cycles Read Projection
    def list_cycles(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        project_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        lim = max(1, min(limit, 200))
        with self._db.tx(tenant=tenant_id) as c:
            if project_id:
                rows = c.execute(
                    "SELECT id, project_id, workspace_id, task_id, run_id, version, phase, cycle_json,"
                    " created_at, updated_at FROM agent.dev_cycles"
                    " WHERE tenant_id=%s AND owner_user_id=%s AND project_id=%s"
                    " ORDER BY created_at DESC LIMIT %s",
                    (tenant_id, owner_user_id, project_id, lim),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT id, project_id, workspace_id, task_id, run_id, version, phase, cycle_json,"
                    " created_at, updated_at FROM agent.dev_cycles"
                    " WHERE tenant_id=%s AND owner_user_id=%s"
                    " ORDER BY created_at DESC LIMIT %s",
                    (tenant_id, owner_user_id, lim),
                ).fetchall()
        return [self._cycle_summary(dict(r)) for r in rows]

    def get_cycle(self, *, tenant_id: str, owner_user_id: str, cycle_id: str) -> dict[str, Any] | None:
        with self._db.tx(tenant=tenant_id) as c:
            row = c.execute(
                "SELECT id, project_id, workspace_id, task_id, run_id, version, phase, cycle_json,"
                " created_at, updated_at FROM agent.dev_cycles"
                " WHERE tenant_id=%s AND id=%s AND owner_user_id=%s",
                (tenant_id, cycle_id, owner_user_id),
            ).fetchone()
        return self._cycle_summary(dict(row)) if row is not None else None

    @staticmethod
    def _cycle_summary(row: dict[str, Any]) -> dict[str, Any]:
        cj = dict(row["cycle_json"]) if isinstance(row.get("cycle_json"), dict) else {}
        raw_commit = cj.get("commit")
        commit: dict[str, Any] | None = raw_commit if isinstance(raw_commit, dict) else None
        raw_reviews = cj.get("reviews")
        reviews: list[Any] = raw_reviews if isinstance(raw_reviews, list) else []
        raw_tests = cj.get("tests")
        tests: list[Any] = raw_tests if isinstance(raw_tests, list) else []
        raw_scope = cj.get("scope")
        scope: dict[str, Any] = raw_scope if isinstance(raw_scope, dict) else {}
        raw_run = cj.get("run")
        run_obj: dict[str, Any] = raw_run if isinstance(raw_run, dict) else {}
        return {
            "id": str(row["id"]),
            "projectId": str(row["project_id"]),
            "workspaceId": str(row["workspace_id"]),
            "taskId": str(row["task_id"]),
            "runId": str(row["run_id"]),
            "goalRevision": int(run_obj.get("goal_revision", 1)),
            "version": int(row["version"]),
            "phase": str(row["phase"]),
            "roundNumber": int(cj.get("round_number", 1)),
            "changeRequestRef": str(cj.get("change_request_ref", "")),
            "repository": str(scope.get("repository", "")),
            "branch": str(scope.get("branch", "")),
            "baseSha": str(scope.get("base_sha", "")),
            "commitSha": str(commit["commit_sha"]) if commit and commit.get("commit_sha") else None,
            "tests": tests,
            "reviews": reviews,
            "pushReceipt": cj.get("push_receipt"),
            "prReceipt": cj.get("pr_receipt"),
            "createdAt": row["created_at"].isoformat(),
            "updatedAt": row["updated_at"].isoformat(),
        }

    # ------------------------------------------------------------------ Design & Demo Decisions (DEV-03 / P1-06)
    def create_design_proposal(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        cycle_id: str,
        candidates: Sequence[dict[str, Any]],
        design_policy: str | None = None,
        demo_acceptance_policy: str | None = None,
        recommended_candidate_id: str | None = None,
    ) -> dict[str, Any]:
        normalized, _ = normalize_design_candidates(candidates)
        cand_ids = {c["candidateId"] for c in normalized}
        if recommended_candidate_id is not None and recommended_candidate_id not in cand_ids:
            raise InvalidEvidence("recommendedCandidateId not in candidates")

        with self._db.tx(tenant=tenant_id) as c:
            # Lock order: tasks -> dev_cycles -> dev_design_decisions (R2-11 live goal_revision verification)
            cy = c.execute(
                "SELECT c.project_id, c.task_id, c.run_id, c.cycle_json, p.preferences_json,"
                " t.goal_revision AS live_goal_revision"
                " FROM agent.dev_cycles c"
                " JOIN agent.dev_projects p ON p.tenant_id=c.tenant_id AND p.id=c.project_id"
                " JOIN agent.tasks t ON t.tenant_id=c.tenant_id AND t.id=c.task_id"
                " WHERE c.tenant_id=%s AND c.id=%s AND c.owner_user_id=%s FOR UPDATE OF c FOR SHARE OF t",
                (tenant_id, cycle_id, owner_user_id),
            ).fetchone()
            if cy is None:
                raise ScopeDenied("cycle or project not found for owner")
            prefs = DevelopmentPreferences.from_raw(
                dict(cy["preferences_json"]) if isinstance(cy["preferences_json"], dict) else {}
            )
            dp = design_policy or prefs.design_policy
            dap = demo_acceptance_policy or prefs.demo_acceptance_policy
            DevelopmentPreferences.from_raw({"designPolicy": dp, "demoAcceptancePolicy": dap})
            max_rev_row = c.execute(
                "SELECT COALESCE(MAX(design_revision), 0) AS r FROM agent.dev_design_decisions"
                " WHERE tenant_id=%s AND cycle_id=%s",
                (tenant_id, cycle_id),
            ).fetchone()
            next_rev = int(max_rev_row["r"]) + 1 if max_rev_row is not None else 1
            cj = dict(cy["cycle_json"]) if isinstance(cy["cycle_json"], dict) else {}
            raw_run = cj.get("run")
            run_obj: dict[str, Any] = raw_run if isinstance(raw_run, dict) else {}
            raw_commit = cj.get("commit")
            commit_sha = (
                str(raw_commit["commit_sha"]) if isinstance(raw_commit, dict) and raw_commit.get("commit_sha") else None
            )
            goal_rev = max(1, int(run_obj.get("goal_revision", 1)))
            if int(cy["live_goal_revision"]) != goal_rev:
                raise StateConflict("STALE_GOAL_REVISION: task goal changed since cycle started")
            rec_id = recommended_candidate_id or (normalized[0]["candidateId"] if dp == "AUTO_RECOMMEND" else None)
            did = uuid7()
            digest = _demo_binding_digest(
                cycle_id=cycle_id,
                commit_sha=commit_sha,
                goal_revision=goal_rev,
                design_revision=next_rev,
                selected_candidate_id=None,
                candidates=normalized,
            )
            row = c.execute(
                "INSERT INTO agent.dev_design_decisions(tenant_id, id, owner_user_id, project_id, cycle_id,"
                " goal_revision, design_revision, design_policy, demo_acceptance_policy, candidates_json,"
                " recommended_candidate_id, demo_decision, demo_snapshot_digest)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s)"
                " RETURNING *",
                (
                    tenant_id,
                    did,
                    owner_user_id,
                    str(cy["project_id"]),
                    cycle_id,
                    goal_rev,
                    next_rev,
                    dp,
                    dap,
                    Jsonb(normalized),
                    rec_id,
                    digest,
                ),
            ).fetchone()
        assert row is not None
        return _design_dto(dict(row))

    def list_design_decisions(self, *, tenant_id: str, owner_user_id: str, cycle_id: str) -> list[dict[str, Any]]:
        with self._db.tx(tenant=tenant_id) as c:
            rows = c.execute(
                "SELECT * FROM agent.dev_design_decisions"
                " WHERE tenant_id=%s AND cycle_id=%s AND owner_user_id=%s"
                " ORDER BY design_revision DESC",
                (tenant_id, cycle_id, owner_user_id),
            ).fetchall()
        return [_design_dto(dict(r)) for r in rows]

    # ------------------------------------------------------------------ Demo Render Artifacts (R3-08)
    def record_demo_render_artifact(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        cycle_id: str,
        candidate_id: str,
        viewport: str,
        fixture_ref: str,
        render_bytes: bytes,
    ) -> dict[str, Any]:
        """Persist a real rendered Demo artifact bound to (cycle, candidate, commit_sha, viewport, fixture_ref) (R3-08)."""
        if not candidate_id.strip():
            raise InvalidEvidence("candidateId is required")
        if not re.fullmatch(r"[0-9]{2,5}x[0-9]{2,5}(@[a-z0-9_-]{1,16})?", viewport.strip()):
            raise InvalidEvidence("invalid demo viewport format (e.g. 1280x800)")
        clean_fix = fixture_ref.strip()
        if not clean_fix or len(clean_fix) > 256:
            raise InvalidEvidence("invalid demo fixtureRef")
        if not isinstance(render_bytes, (bytes, bytearray)) or not 1 <= len(render_bytes) <= 4_194_304:
            raise InvalidEvidence("render_bytes must be non-empty bytes <= 4MiB")
        content_sha = hashlib.sha256(bytes(render_bytes)).hexdigest()
        art_id = uuid7()
        with self._db.tx(tenant=tenant_id) as c:
            cy = c.execute(
                "SELECT project_id, cycle_json FROM agent.dev_cycles WHERE tenant_id=%s AND id=%s AND owner_user_id=%s",
                (tenant_id, cycle_id, owner_user_id),
            ).fetchone()
            if cy is None:
                raise ScopeDenied("cycle not found for owner")
            cj = dict(cy["cycle_json"]) if isinstance(cy["cycle_json"], dict) else {}
            raw_commit = cj.get("commit")
            commit_sha = (
                str(raw_commit["commit_sha"]) if isinstance(raw_commit, dict) and raw_commit.get("commit_sha") else ""
            )
            require_sha(commit_sha)
            row = c.execute(
                "INSERT INTO agent.dev_demo_artifacts(tenant_id, id, owner_user_id, project_id, cycle_id,"
                " candidate_id, commit_sha, viewport, fixture_ref, content_sha256, byte_length)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    tenant_id,
                    art_id,
                    owner_user_id,
                    str(cy["project_id"]),
                    cycle_id,
                    candidate_id.strip(),
                    commit_sha,
                    viewport.strip(),
                    clean_fix,
                    content_sha,
                    len(render_bytes),
                ),
            ).fetchone()
        assert row is not None
        return {
            "id": str(row["id"]),
            "projectId": str(row["project_id"]),
            "cycleId": str(row["cycle_id"]),
            "candidateId": str(row["candidate_id"]),
            "commitSha": str(row["commit_sha"]),
            "viewport": str(row["viewport"]),
            "fixtureRef": str(row["fixture_ref"]),
            "contentSha256": str(row["content_sha256"]),
            "byteLength": int(row["byte_length"]),
            "createdAt": row["created_at"].isoformat(),
        }

    @staticmethod
    def _resolve_rendered_demo_binding(
        c: Any,
        *,
        tenant_id: str,
        owner_user_id: str,
        cycle_id: str,
        selected_candidate_id: str,
        commit_sha: str | None,
        cand_fixture: str,
        demo_render_artifact_id: str | None,
        demo_artifact_ref: str | None,
        demo_artifact_sha256: str | None,
    ) -> tuple[str | None, str | None, str | None, str | None, str | None, str | None]:
        """Resolve and validate persisted rendered Demo artifact binding without fallback hashing (R3-08)."""
        render_row: dict[str, Any] | None = None
        if demo_render_artifact_id is not None:
            found = c.execute(
                "SELECT * FROM agent.dev_demo_artifacts"
                " WHERE tenant_id=%s AND id=%s AND owner_user_id=%s AND cycle_id=%s AND candidate_id=%s",
                (tenant_id, demo_render_artifact_id, owner_user_id, cycle_id, selected_candidate_id),
            ).fetchone()
            if found is None:
                raise InvalidEvidence("RENDERED_DEMO_ARTIFACT_NOT_FOUND")
            render_row = dict(found)
        elif commit_sha:
            found = c.execute(
                "SELECT * FROM agent.dev_demo_artifacts"
                " WHERE tenant_id=%s AND owner_user_id=%s AND cycle_id=%s AND candidate_id=%s"
                " AND commit_sha=%s AND fixture_ref=%s"
                " ORDER BY created_at DESC LIMIT 1",
                (tenant_id, owner_user_id, cycle_id, selected_candidate_id, commit_sha, cand_fixture),
            ).fetchone()
            if found is not None:
                render_row = dict(found)

        if render_row is None:
            if demo_artifact_sha256 is not None:
                raise InvalidEvidence("RENDERED_DEMO_ARTIFACT_REQUIRED: unverified demoArtifactSha256 rejected")
            return None, None, None, None, None, None

        if (
            (commit_sha and str(render_row["commit_sha"]) != commit_sha)
            or str(render_row["fixture_ref"]) != cand_fixture
            or (demo_artifact_sha256 is not None and str(render_row["content_sha256"]) != demo_artifact_sha256)
        ):
            raise InvalidEvidence("RENDERED_DEMO_ARTIFACT_MISMATCH")
        resolved_render_id = str(render_row["id"])
        resolved_ref = (
            redact_dev_text(demo_artifact_ref.strip(), limit=256)
            if demo_artifact_ref and demo_artifact_ref.strip()
            else f"demo-artifact://{resolved_render_id}"
        )
        return (
            resolved_render_id,
            resolved_ref,
            str(render_row["content_sha256"]),
            str(render_row["viewport"]),
            str(render_row["fixture_ref"]),
            str(render_row["commit_sha"]),
        )

    def record_design_decision(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        cycle_id: str,
        design_revision: int,
        selected_candidate_id: str,
        feedback: str | None = None,
        demo_render_artifact_id: str | None = None,
        demo_artifact_ref: str | None = None,
        demo_artifact_sha256: str | None = None,
    ) -> dict[str, Any]:
        if demo_render_artifact_id is not None and not is_uuid7(demo_render_artifact_id):
            raise InvalidEvidence("demoRenderArtifactId must be uuid7")
        if demo_artifact_sha256 is not None:
            require_digest(demo_artifact_sha256)
        with self._db.tx(tenant=tenant_id) as c:
            cy_row = c.execute(
                "SELECT c.task_id, c.run_id, c.cycle_json, t.goal_revision AS live_goal_revision"
                " FROM agent.dev_cycles c"
                " JOIN agent.tasks t ON t.tenant_id=c.tenant_id AND t.id=c.task_id"
                " WHERE c.tenant_id=%s AND c.id=%s AND c.owner_user_id=%s FOR UPDATE OF c FOR SHARE OF t",
                (tenant_id, cycle_id, owner_user_id),
            ).fetchone()
            if cy_row is None:
                raise ScopeDenied("cycle not found")
            cur_row = c.execute(
                "SELECT * FROM agent.dev_design_decisions"
                " WHERE tenant_id=%s AND cycle_id=%s AND owner_user_id=%s"
                " ORDER BY design_revision DESC LIMIT 1 FOR UPDATE",
                (tenant_id, cycle_id, owner_user_id),
            ).fetchone()
            if cur_row is None:
                raise ScopeDenied("design proposal not found")
            if int(cur_row["design_revision"]) != design_revision:
                raise StateConflict("stale design_revision")
            if int(cy_row["live_goal_revision"]) != int(cur_row["goal_revision"]):
                raise StateConflict("STALE_GOAL_REVISION: task goal changed since design proposal")
            cands = list(cur_row["candidates_json"]) if isinstance(cur_row["candidates_json"], list) else []
            matched_cand = next(
                (x for x in cands if isinstance(x, dict) and str(x.get("candidateId")) == selected_candidate_id),
                None,
            )
            if matched_cand is None:
                raise InvalidEvidence("selectedCandidateId not found in design revision")

            cj = dict(cy_row["cycle_json"]) if isinstance(cy_row["cycle_json"], dict) else {}
            raw_commit = cj.get("commit")
            commit_sha = (
                str(raw_commit["commit_sha"]) if isinstance(raw_commit, dict) and raw_commit.get("commit_sha") else None
            )
            cand_fixture = str(matched_cand.get("fixtureRef") or "")

            (
                resolved_render_id,
                resolved_ref,
                resolved_sha,
                resolved_vp,
                resolved_fix,
                resolved_commit,
            ) = self._resolve_rendered_demo_binding(
                c,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                cycle_id=cycle_id,
                selected_candidate_id=selected_candidate_id,
                commit_sha=commit_sha,
                cand_fixture=cand_fixture,
                demo_render_artifact_id=demo_render_artifact_id,
                demo_artifact_ref=demo_artifact_ref,
                demo_artifact_sha256=demo_artifact_sha256,
            )

            new_digest = _demo_binding_digest(
                cycle_id=cycle_id,
                commit_sha=commit_sha,
                goal_revision=int(cur_row["goal_revision"]),
                design_revision=design_revision,
                selected_candidate_id=selected_candidate_id,
                candidates=cands,
                demo_render_artifact_id=resolved_render_id,
                demo_artifact_ref=resolved_ref,
                demo_artifact_sha256=resolved_sha,
                demo_viewport=resolved_vp,
                demo_fixture_ref=resolved_fix,
            )
            clean_fb = redact_dev_text(feedback, limit=500) if feedback else None
            # P1-06 / R2-11 / R3-08: selecting/changing candidate resets demo_decision to PENDING & binds real render artifact!
            updated = c.execute(
                "UPDATE agent.dev_design_decisions"
                " SET selected_candidate_id=%s, demo_decision='PENDING', decided_at=NULL,"
                " demo_render_artifact_id=%s, demo_artifact_ref=%s, demo_artifact_sha256=%s,"
                " demo_viewport=%s, demo_fixture_ref=%s, demo_commit_sha=%s,"
                " demo_snapshot_digest=%s, feedback=%s, updated_at=now()"
                " WHERE tenant_id=%s AND id=%s RETURNING *",
                (
                    selected_candidate_id,
                    resolved_render_id,
                    resolved_ref,
                    resolved_sha,
                    resolved_vp,
                    resolved_fix,
                    resolved_commit,
                    new_digest,
                    clean_fb,
                    tenant_id,
                    cur_row["id"],
                ),
            ).fetchone()
            append_event(
                c,
                tenant_id=tenant_id,
                aggregate_type="task",
                aggregate_id=str(cy_row["task_id"]),
                kind="dev.design_decided",
                payload={
                    "cycleId": cycle_id,
                    "designRevision": design_revision,
                    "candidateId": selected_candidate_id,
                    "demoRenderArtifactId": resolved_render_id,
                    "demoArtifactSha256": resolved_sha,
                    "demoSnapshotDigest": new_digest,
                },
                actor={"type": "user", "id": owner_user_id},
                owner_user_id=owner_user_id,
                task_id=str(cy_row["task_id"]),
                run_id=str(cy_row["run_id"]),
            )
        assert updated is not None
        return _design_dto(dict(updated))

    def record_demo_decision(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        cycle_id: str,
        design_revision: int,
        decision: str,
        demo_snapshot_digest: str,
        feedback: str | None = None,
    ) -> dict[str, Any]:
        if decision not in VALID_DEMO_DECISIONS:
            raise InvalidEvidence(f"invalid demo decision: {decision}")
        require_digest(demo_snapshot_digest)

        with self._db.tx(tenant=tenant_id) as c:
            cy_row = c.execute(
                "SELECT c.task_id, c.run_id, c.cycle_json, t.goal_revision AS live_goal_revision"
                " FROM agent.dev_cycles c"
                " JOIN agent.tasks t ON t.tenant_id=c.tenant_id AND t.id=c.task_id"
                " WHERE c.tenant_id=%s AND c.id=%s AND c.owner_user_id=%s FOR UPDATE OF c FOR SHARE OF t",
                (tenant_id, cycle_id, owner_user_id),
            ).fetchone()
            if cy_row is None:
                raise ScopeDenied("cycle not found")
            cur_row = c.execute(
                "SELECT * FROM agent.dev_design_decisions"
                " WHERE tenant_id=%s AND cycle_id=%s AND owner_user_id=%s"
                " ORDER BY design_revision DESC LIMIT 1 FOR UPDATE",
                (tenant_id, cycle_id, owner_user_id),
            ).fetchone()
            if cur_row is None:
                raise ScopeDenied("design proposal not found")
            if int(cur_row["design_revision"]) != design_revision:
                raise StateConflict("stale design_revision")
            if int(cy_row["live_goal_revision"]) != int(cur_row["goal_revision"]):
                raise StateConflict("STALE_GOAL_REVISION: task goal changed before demo decision")
            if decision == "APPROVED" and not cur_row["selected_candidate_id"]:
                raise StateConflict("must select a design candidate before approving demo")
            # Re-verify digest against live cycle commit, live goal_revision, selected candidate & real rendered artifact!
            cj = dict(cy_row["cycle_json"]) if isinstance(cy_row["cycle_json"], dict) else {}
            raw_commit = cj.get("commit")
            commit_sha = (
                str(raw_commit["commit_sha"]) if isinstance(raw_commit, dict) and raw_commit.get("commit_sha") else None
            )
            cands = list(cur_row["candidates_json"]) if isinstance(cur_row["candidates_json"], list) else []
            if decision == "APPROVED":
                render_id = cur_row.get("demo_render_artifact_id")
                if not render_id:
                    raise InvalidEvidence("RENDERED_DEMO_ARTIFACT_REQUIRED")
                render_row = c.execute(
                    "SELECT * FROM agent.dev_demo_artifacts"
                    " WHERE tenant_id=%s AND id=%s AND owner_user_id=%s AND cycle_id=%s",
                    (tenant_id, render_id, owner_user_id, cycle_id),
                ).fetchone()
                matched_cand = next(
                    (
                        x
                        for x in cands
                        if isinstance(x, dict) and str(x.get("candidateId")) == str(cur_row["selected_candidate_id"])
                    ),
                    None,
                )
                if (
                    render_row is None
                    or matched_cand is None
                    or str(render_row["candidate_id"]) != str(cur_row["selected_candidate_id"])
                    or not commit_sha
                    or str(render_row["commit_sha"]) != commit_sha
                    or str(render_row["fixture_ref"]) != str(matched_cand.get("fixtureRef") or "")
                    or str(render_row["viewport"]) != str(cur_row.get("demo_viewport") or "")
                    or str(render_row["content_sha256"]) != str(cur_row.get("demo_artifact_sha256") or "")
                ):
                    raise StateConflict("RENDERED_DEMO_ARTIFACT_MISMATCH")

            expected_digest = _demo_binding_digest(
                cycle_id=cycle_id,
                commit_sha=commit_sha,
                goal_revision=int(cy_row["live_goal_revision"]),
                design_revision=design_revision,
                selected_candidate_id=str(cur_row["selected_candidate_id"])
                if cur_row["selected_candidate_id"]
                else None,
                candidates=cands,
                demo_render_artifact_id=str(cur_row["demo_render_artifact_id"])
                if cur_row.get("demo_render_artifact_id")
                else None,
                demo_artifact_ref=str(cur_row["demo_artifact_ref"]) if cur_row.get("demo_artifact_ref") else None,
                demo_artifact_sha256=str(cur_row["demo_artifact_sha256"])
                if cur_row.get("demo_artifact_sha256")
                else None,
                demo_viewport=str(cur_row["demo_viewport"]) if cur_row.get("demo_viewport") else None,
                demo_fixture_ref=str(cur_row["demo_fixture_ref"]) if cur_row.get("demo_fixture_ref") else None,
            )
            if demo_snapshot_digest != expected_digest or str(cur_row["demo_snapshot_digest"] or "") != expected_digest:
                raise StateConflict("demoSnapshotDigest mismatch with current design/commit/artifact state")
            clean_fb = redact_dev_text(feedback, limit=500) if feedback else None
            updated = c.execute(
                "UPDATE agent.dev_design_decisions"
                " SET demo_decision=%s, demo_snapshot_digest=%s, feedback=COALESCE(%s, feedback),"
                " decided_at=now(), updated_at=now()"
                " WHERE tenant_id=%s AND id=%s RETURNING *",
                (decision, expected_digest, clean_fb, tenant_id, cur_row["id"]),
            ).fetchone()
            append_event(
                c,
                tenant_id=tenant_id,
                aggregate_type="task",
                aggregate_id=str(cy_row["task_id"]),
                kind="dev.demo_decided",
                payload={
                    "cycleId": cycle_id,
                    "designRevision": design_revision,
                    "decision": decision,
                    "demoSnapshotDigest": expected_digest,
                },
                actor={"type": "user", "id": owner_user_id},
                owner_user_id=owner_user_id,
                task_id=str(cy_row["task_id"]),
                run_id=str(cy_row["run_id"]),
            )
        assert updated is not None
        return _design_dto(dict(updated))

    # ------------------------------------------------------------------ Debug Findings & Repro (DEV-06 / DEV-12)
    def record_debug_finding(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        cycle_id: str,
        preview_lease_id: str | None,
        report: DebugReport,
        repro: ReproductionProof | None = None,
    ) -> dict[str, Any]:
        with self._db.tx(tenant=tenant_id) as c:
            cy = c.execute(
                "SELECT project_id, task_id, run_id FROM agent.dev_cycles"
                " WHERE tenant_id=%s AND id=%s AND owner_user_id=%s",
                (tenant_id, cycle_id, owner_user_id),
            ).fetchone()
            if cy is None:
                raise ScopeDenied("cycle not found for owner")
            fid = uuid7()
            status = "VERIFIED" if (repro is not None and repro.verified) else report.status
            row = c.execute(
                "INSERT INTO agent.dev_debug_findings(tenant_id, id, owner_user_id, project_id, cycle_id,"
                " preview_lease_id, commit_sha, source_url_redacted, console_errors_json,"
                " network_errors_json, layout_issues_json, repro_test_id, repro_before_fail_digest,"
                " repro_after_pass_digest, status)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    tenant_id,
                    fid,
                    owner_user_id,
                    str(cy["project_id"]),
                    cycle_id,
                    preview_lease_id,
                    report.commit_sha,
                    report.source_url_redacted,
                    Jsonb(list(report.console_errors)),
                    Jsonb(list(report.network_errors)),
                    Jsonb(list(report.layout_issues)),
                    repro.test_id if repro else None,
                    repro.before_fail_digest if repro else None,
                    repro.after_pass_digest if repro else None,
                    status,
                ),
            ).fetchone()
            append_event(
                c,
                tenant_id=tenant_id,
                aggregate_type="task",
                aggregate_id=str(cy["task_id"]),
                kind="dev.repro_verified" if (repro and repro.verified) else "dev.debug_finding_recorded",
                payload={
                    "cycleId": cycle_id,
                    "findingId": fid,
                    "commitSha": report.commit_sha,
                    "status": status,
                },
                actor={"type": "service", "id": "development"},
                owner_user_id=owner_user_id,
                task_id=str(cy["task_id"]),
                run_id=str(cy["run_id"]),
            )
        assert row is not None
        return _debug_dto(dict(row))

    def list_debug_findings(self, *, tenant_id: str, owner_user_id: str, cycle_id: str) -> list[dict[str, Any]]:
        with self._db.tx(tenant=tenant_id) as c:
            rows = c.execute(
                "SELECT * FROM agent.dev_debug_findings"
                " WHERE tenant_id=%s AND cycle_id=%s AND owner_user_id=%s"
                " ORDER BY created_at DESC",
                (tenant_id, cycle_id, owner_user_id),
            ).fetchall()
        return [_debug_dto(dict(r)) for r in rows]

    # ------------------------------------------------------------------ PR Records & Guardian (P1-03 / P2-10)
    def begin_pr_record(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        project_id: str,
        cycle_id: str,
        repository: str,
        branch: str,
        head_sha: str,
        idempotency_key: str,
        intent_id: str,
        git_push_receipt_ref: str | None = None,
        required_checks: Sequence[str] = (),
        max_repair_attempts: int = 3,
    ) -> tuple[dict[str, Any], bool]:
        """Concurrency-safe atomic reservation per BOTH idempotency_key AND intent_id via ON CONFLICT DO NOTHING (R2-13)."""
        require_sha(head_sha)
        if not idempotency_key.strip() or not is_uuid7(intent_id):
            raise InvalidEvidence("idempotency_key and uuid7 intent_id are required")
        rid = uuid7()
        with self._db.tx(tenant=tenant_id) as c:
            row = c.execute(
                "INSERT INTO agent.dev_pr_records(tenant_id, id, owner_user_id, project_id, cycle_id,"
                " repository, branch, head_sha, idempotency_key, intent_id, git_push_receipt_ref,"
                " required_checks_json, effect_status, max_repair_attempts)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s)"
                " ON CONFLICT DO NOTHING RETURNING *",
                (
                    tenant_id,
                    rid,
                    owner_user_id,
                    project_id,
                    cycle_id,
                    repository,
                    branch,
                    head_sha,
                    idempotency_key,
                    intent_id,
                    git_push_receipt_ref,
                    Jsonb(list(required_checks)),
                    max(1, min(max_repair_attempts, 10)),
                ),
            ).fetchone()
            if row is not None:
                return _pr_dto(dict(row)), True
            existing = c.execute(
                "SELECT * FROM agent.dev_pr_records"
                " WHERE tenant_id=%s AND (idempotency_key=%s OR intent_id=%s)"
                " ORDER BY created_at ASC LIMIT 1 FOR UPDATE",
                (tenant_id, idempotency_key, intent_id),
            ).fetchone()
            if existing is None:
                raise StateConflict("concurrent pr reservation conflict")
            if str(existing["owner_user_id"]) != owner_user_id:
                raise ScopeDenied("pr record belongs to another owner")
            return _pr_dto(dict(existing)), False

    def settle_pr_record(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        record_id: str,
        effect_status: str,
        pr_number: int | None = None,
        pr_url: str | None = None,
        ci_state: str | None = None,
        last_error_code: str | None = None,
        event_kind: str = "dev.pr_recorded",
    ) -> dict[str, Any]:
        with self._db.tx(tenant=tenant_id) as c:
            cur = c.execute(
                "SELECT r.*, cy.task_id, cy.run_id FROM agent.dev_pr_records r"
                " JOIN agent.dev_cycles cy ON cy.tenant_id=r.tenant_id AND cy.id=r.cycle_id"
                " WHERE r.tenant_id=%s AND r.id=%s AND r.owner_user_id=%s FOR UPDATE OF r",
                (tenant_id, record_id, owner_user_id),
            ).fetchone()
            if cur is None:
                raise ScopeDenied("pr record not found")
            row = c.execute(
                "UPDATE agent.dev_pr_records"
                " SET effect_status=%s, pr_number=COALESCE(%s, pr_number), pr_url=COALESCE(%s, pr_url),"
                " ci_state=COALESCE(%s, ci_state), last_error_code=%s, updated_at=now()"
                " WHERE tenant_id=%s AND id=%s RETURNING *",
                (
                    effect_status,
                    pr_number,
                    pr_url,
                    ci_state,
                    last_error_code,
                    tenant_id,
                    record_id,
                ),
            ).fetchone()
            append_event(
                c,
                tenant_id=tenant_id,
                aggregate_type="task",
                aggregate_id=str(cur["task_id"]),
                kind=event_kind,
                payload={
                    "prRecordId": record_id,
                    "cycleId": str(cur["cycle_id"]),
                    "effectStatus": effect_status,
                    "ciState": ci_state or str(cur["ci_state"]),
                },
                actor={"type": "service", "id": "development"},
                owner_user_id=owner_user_id,
                task_id=str(cur["task_id"]),
                run_id=str(cur["run_id"]),
            )
        assert row is not None
        return _pr_dto(dict(row))

    def claim_guardian_repair_slot(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        record_id: str,
        ci_state: str,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically check repair_attempts < max_repair_attempts and increment in ONE SQL UPDATE (P2-10)."""
        with self._db.tx(tenant=tenant_id) as c:
            meta = c.execute(
                "SELECT r.cycle_id, cy.task_id, cy.run_id FROM agent.dev_pr_records r"
                " JOIN agent.dev_cycles cy ON cy.tenant_id=r.tenant_id AND cy.id=r.cycle_id"
                " WHERE r.tenant_id=%s AND r.id=%s AND r.owner_user_id=%s FOR UPDATE OF r",
                (tenant_id, record_id, owner_user_id),
            ).fetchone()
            if meta is None:
                raise ScopeDenied("pr record not found")
            claimed = c.execute(
                "UPDATE agent.dev_pr_records"
                " SET repair_attempts = repair_attempts + 1, ci_state=%s, updated_at=now()"
                " WHERE tenant_id=%s AND id=%s AND owner_user_id=%s AND repair_attempts < max_repair_attempts"
                " RETURNING *",
                (ci_state, tenant_id, record_id, owner_user_id),
            ).fetchone()
            if claimed is not None:
                append_event(
                    c,
                    tenant_id=tenant_id,
                    aggregate_type="task",
                    aggregate_id=str(meta["task_id"]),
                    kind="dev.pr_reconciled",
                    payload={
                        "prRecordId": record_id,
                        "cycleId": str(meta["cycle_id"]),
                        "ciState": ci_state,
                        "repairAttempt": int(claimed["repair_attempts"]),
                    },
                    actor={"type": "service", "id": "development"},
                    owner_user_id=owner_user_id,
                    task_id=str(meta["task_id"]),
                    run_id=str(meta["run_id"]),
                )
                return _pr_dto(dict(claimed)), True

            escalated = c.execute(
                "UPDATE agent.dev_pr_records SET ci_state='ESCALATED_LIMIT_REACHED', updated_at=now()"
                " WHERE tenant_id=%s AND id=%s AND owner_user_id=%s RETURNING *",
                (tenant_id, record_id, owner_user_id),
            ).fetchone()
            assert escalated is not None
            append_event(
                c,
                tenant_id=tenant_id,
                aggregate_type="task",
                aggregate_id=str(meta["task_id"]),
                kind="dev.guardian_escalated",
                payload={
                    "prRecordId": record_id,
                    "cycleId": str(meta["cycle_id"]),
                    "ciState": "ESCALATED_LIMIT_REACHED",
                    "repairAttempts": int(escalated["repair_attempts"]),
                },
                actor={"type": "service", "id": "development"},
                owner_user_id=owner_user_id,
                task_id=str(meta["task_id"]),
                run_id=str(meta["run_id"]),
            )
            return _pr_dto(dict(escalated)), False

    def get_pr_record(self, *, tenant_id: str, owner_user_id: str, record_id: str) -> dict[str, Any] | None:
        with self._db.tx(tenant=tenant_id) as c:
            row = c.execute(
                "SELECT * FROM agent.dev_pr_records WHERE tenant_id=%s AND id=%s AND owner_user_id=%s",
                (tenant_id, record_id, owner_user_id),
            ).fetchone()
        return _pr_dto(dict(row)) if row is not None else None

    def list_pr_records(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        project_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        lim = max(1, min(limit, 200))
        with self._db.tx(tenant=tenant_id) as c:
            rows = c.execute(
                "SELECT * FROM agent.dev_pr_records"
                " WHERE tenant_id=%s AND owner_user_id=%s AND project_id=%s"
                " ORDER BY created_at DESC LIMIT %s",
                (tenant_id, owner_user_id, project_id, lim),
            ).fetchall()
        return [_pr_dto(dict(r)) for r in rows]
