"""DEV-09 FastAPI routes for Autonomous Software Development (Projects, Cycles, Design/Demo, Previews, Debug, PRs).

Hardened against PR #122 Round 1 & Round 2 reviews (R2-01..R2-13):
- Zero Tool Gateway DB credentials in API: mutating PR dispatches and reconciliations route exclusively through
  authenticated ToolGateway RPC (`AIAGENT_TOOL_GATEWAY_ENDPOINT` + `AIAGENT_API_TOOL_RPC_KEY`), while PR Guardian
  reads route exclusively through authenticated Sentinel RPC (`AIAGENT_SENTINEL_ENDPOINT` + `AIAGENT_API_SENTINEL_RPC_KEY`).
- Browser Debugger routes strictly through PR #110 OS-isolated Sandbox Cell (`preview.debug` RPC + signed Grant).
- Repository Binding is verified in PostgreSQL (`agent.verify_dev_repository_binding`) before project creation or PR dispatch.
- Preflight intent & approval validation runs BEFORE `begin_pr_record` so invalid/tampered requests never consume an intent.
- Reconcile refuses `FAILED` records and delegates strictly to ToolGateway's canonical CAS reconciler.
- Required CI checks are derived authoritatively from Project policy + GitHub Branch Protection.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from typing import Any

from aiagent_core import config
from aiagent_core.commands import Scope
from aiagent_core.db import Database
from aiagent_core.events import append_event
from aiagent_core.rpc import RpcClient
from aiagent_development.browser_debugger import AutonomousBrowserDebugger
from aiagent_development.contracts import (
    DevelopmentError,
    EffectUnknown,
    InvalidEvidence,
    InvalidPath,
    ProjectScope,
    ScopeDenied,
    StaleReference,
    StateConflict,
    require_sha,
)
from aiagent_development.github_egress import GitHubReadClient, canonical_pr_request, pr_request_sha256
from aiagent_development.pg_store import PostgresCycleStore, PreviewBroker
from aiagent_development.pr_guardian import PrGuardian
from aiagent_development.store import PostgresDevStore
from aiagent_domain import is_uuid7
from aiagent_domain.rpcwire import RpcError
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

_SYNTHETIC_DEMO_ACTIONS = frozenset({"click_demo", "toggle_theme", "switch_viewport"})


async def _parse_json_obj(request: Request) -> dict[str, Any]:
    raw = await request.body()
    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        raise HTTPException(422, detail={"error": "INVALID_JSON"}) from None
    if not isinstance(body, dict):
        raise HTTPException(422, detail={"error": "JSON_OBJECT_REQUIRED"})
    return body


def _raise_dev_http(exc: DevelopmentError) -> None:
    if isinstance(exc, (InvalidEvidence, InvalidPath)):
        raise HTTPException(422, detail={"error": str(exc)}) from None
    if isinstance(exc, ScopeDenied):
        raise HTTPException(403, detail={"error": str(exc)}) from None
    if isinstance(exc, (StateConflict, StaleReference)):
        raise HTTPException(409, detail={"error": str(exc)}) from None
    if isinstance(exc, EffectUnknown):
        raise HTTPException(502, detail={"error": "EFFECT_UNKNOWN", "message": str(exc)}) from None
    raise HTTPException(400, detail={"error": str(exc)}) from None


def _resolve_tool_gateway_rpc() -> RpcClient:
    endpoint = (os.environ.get("AIAGENT_TOOL_GATEWAY_ENDPOINT") or "").strip()
    key = config.secret("AIAGENT_API_TOOL_RPC_KEY", required=False)
    if not endpoint or key is None:
        raise HTTPException(503, detail={"error": "TOOL_GATEWAY_RPC_UNAVAILABLE"})
    return RpcClient(endpoint, "api", bytes.fromhex(key.reveal().strip()), timeout_s=35.0)


def register_development_routes(
    app: FastAPI,
    *,
    get_db: Callable[[], Database],
    scope_dep: Any,
    sentinel: Any = None,
    egress_proxy: str | None = None,
    dev_github_client: Any = None,
    dev_debug_probe_fn: Any = None,
    tool_gateway: Any = None,
) -> None:
    def _store() -> PostgresDevStore:
        return PostgresDevStore(get_db())

    def _previews() -> PreviewBroker:
        return PreviewBroker(get_db())

    def _github_reader(github_api_base: str, *, repository_binding_id: str | None) -> Any:
        if dev_github_client is not None:
            return dev_github_client
        return GitHubReadClient(
            db=get_db(),
            github_api_base=github_api_base,
            repository_binding_id=repository_binding_id,
            sentinel=sentinel,
            egress_proxy=egress_proxy,
        )

    async def _dispatch_pr_via_gateway(
        *,
        tenant_id: str,
        owner_user_id: str,
        workspace_id: str,
        intent_id: str,
        expected_args: dict[str, Any],
    ) -> dict[str, Any]:
        if tool_gateway is not None:
            return dict(
                await tool_gateway.dispatch_approved_dev_pr(
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                    workspace_id=workspace_id,
                    intent_id=intent_id,
                    expected_args=expected_args,
                )
            )
        cli = _resolve_tool_gateway_rpc()
        return dict(
            await cli.call(
                "tool.dev_pr_dispatch",
                {
                    "tenantId": tenant_id,
                    "ownerUserId": owner_user_id,
                    "workspaceId": workspace_id,
                    "intentId": intent_id,
                    "args": expected_args,
                },
            )
        )

    async def _reconcile_via_gateway(*, tenant_id: str, owner_user_id: str, intent_id: str) -> dict[str, Any]:
        if tool_gateway is not None:
            return dict(await tool_gateway.reconcile(tenant_id, intent_id, owner_user_id=owner_user_id))
        cli = _resolve_tool_gateway_rpc()
        return dict(
            await cli.call(
                "tool.reconcile",
                {"tenantId": tenant_id, "ownerUserId": owner_user_id, "intentId": intent_id},
            )
        )

    # ------------------------------------------------------------------ Projects
    @app.get("/v1/development/projects")
    async def list_dev_projects(s: Scope = Depends(scope_dep), limit: int = 50) -> dict[str, Any]:  # noqa: B008
        items = await asyncio.to_thread(
            _store().list_projects, tenant_id=s.tenant_id, owner_user_id=s.user_id, limit=limit
        )
        return {"schemaVersion": "1.0", "items": items}

    @app.post("/v1/development/projects", status_code=201)
    async def create_dev_project(request: Request, s: Scope = Depends(scope_dep)) -> dict[str, Any]:  # noqa: B008
        body = await _parse_json_obj(request)
        try:
            proj = await asyncio.to_thread(
                _store().create_project,
                tenant_id=s.tenant_id,
                owner_user_id=s.user_id,
                workspace_id=s.workspace_id,
                project_id=body.get("projectId"),
                name=str(body.get("name") or ""),
                repository=str(body.get("repository") or ""),
                default_branch=str(body.get("defaultBranch") or "main"),
                framework=str(body.get("framework") or "web"),
                repository_binding_id=body.get("repositoryBindingId"),
                github_api_base=body.get("githubApiBase"),
                preferences=body.get("preferences") if isinstance(body.get("preferences"), dict) else None,
            )
        except DevelopmentError as exc:
            _raise_dev_http(exc)
        return {"schemaVersion": "1.0", "project": proj}

    @app.get("/v1/development/projects/{project_id}")
    async def get_dev_project(project_id: str, s: Scope = Depends(scope_dep)) -> dict[str, Any]:  # noqa: B008
        st = _store()
        proj = await asyncio.to_thread(
            st.get_project, tenant_id=s.tenant_id, owner_user_id=s.user_id, project_id=project_id
        )
        if proj is None:
            raise HTTPException(404)
        cycles = await asyncio.to_thread(
            st.list_cycles, tenant_id=s.tenant_id, owner_user_id=s.user_id, project_id=project_id, limit=50
        )
        prs = await asyncio.to_thread(
            st.list_pr_records, tenant_id=s.tenant_id, owner_user_id=s.user_id, project_id=project_id, limit=50
        )
        return {"schemaVersion": "1.0", "project": proj, "cycles": cycles, "prRecords": prs}

    # ------------------------------------------------------------------ Cycles & Reviews
    @app.get("/v1/development/cycles")
    async def list_dev_cycles(
        projectId: str | None = None,
        limit: int = 50,
        s: Scope = Depends(scope_dep),  # noqa: B008
    ) -> dict[str, Any]:
        items = await asyncio.to_thread(
            _store().list_cycles,
            tenant_id=s.tenant_id,
            owner_user_id=s.user_id,
            project_id=projectId,
            limit=limit,
        )
        return {"schemaVersion": "1.0", "items": items}

    @app.get("/v1/development/cycles/{cycle_id}")
    async def get_dev_cycle(cycle_id: str, s: Scope = Depends(scope_dep)) -> dict[str, Any]:  # noqa: B008
        st = _store()
        cycle = await asyncio.to_thread(st.get_cycle, tenant_id=s.tenant_id, owner_user_id=s.user_id, cycle_id=cycle_id)
        if cycle is None:
            raise HTTPException(404)
        designs = await asyncio.to_thread(
            st.list_design_decisions, tenant_id=s.tenant_id, owner_user_id=s.user_id, cycle_id=cycle_id
        )
        findings = await asyncio.to_thread(
            st.list_debug_findings, tenant_id=s.tenant_id, owner_user_id=s.user_id, cycle_id=cycle_id
        )
        return {"schemaVersion": "1.0", "cycle": cycle, "designDecisions": designs, "debugFindings": findings}

    @app.get("/v1/development/cycles/{cycle_id}/reviews")
    async def get_dev_cycle_reviews(cycle_id: str, s: Scope = Depends(scope_dep)) -> dict[str, Any]:  # noqa: B008
        cycle = await asyncio.to_thread(
            _store().get_cycle, tenant_id=s.tenant_id, owner_user_id=s.user_id, cycle_id=cycle_id
        )
        if cycle is None:
            raise HTTPException(404)
        reviews = cycle["reviews"]
        commit_sha = cycle["commitSha"]
        pinned = [r for r in reviews if isinstance(r, dict) and r.get("target_sha") == commit_sha]
        roles = {str(r.get("reviewer_role")) for r in pinned}
        open_p0_p1 = sum(
            1
            for r in pinned
            for f in (r.get("findings") or [])
            if isinstance(f, dict) and f.get("severity") in ("P0", "P1") and f.get("status") in ("OPEN", "UNVERIFIED")
        )
        return {
            "schemaVersion": "1.0",
            "cycleId": cycle_id,
            "commitSha": commit_sha,
            "allThreeRolesPresent": roles == {"functional", "security", "ui-e2e"},
            "openP0P1Count": open_p0_p1,
            "reviews": pinned,
        }

    # ------------------------------------------------------------------ Design Picker & Demo Approval (DEV-03 / R2-11)
    @app.post("/v1/development/cycles/{cycle_id}/design", status_code=201)
    async def create_dev_design(cycle_id: str, request: Request, s: Scope = Depends(scope_dep)) -> dict[str, Any]:  # noqa: B008
        body = await _parse_json_obj(request)
        if "demoChoice" in body:
            raise HTTPException(
                422,
                detail={"error": "DEPRECATED_DEMO_CHOICE_AUTO: use explicit designPolicy and demoAcceptancePolicy"},
            )
        cands = body.get("candidates")
        if not isinstance(cands, list):
            raise HTTPException(422, detail={"error": "CANDIDATES_LIST_REQUIRED"})
        try:
            dec = await asyncio.to_thread(
                _store().create_design_proposal,
                tenant_id=s.tenant_id,
                owner_user_id=s.user_id,
                cycle_id=cycle_id,
                candidates=cands,
                design_policy=body.get("designPolicy"),
                demo_acceptance_policy=body.get("demoAcceptancePolicy"),
                recommended_candidate_id=body.get("recommendedCandidateId"),
            )
        except DevelopmentError as exc:
            _raise_dev_http(exc)
        return {"schemaVersion": "1.0", "design": dec}

    @app.post("/v1/development/cycles/{cycle_id}/design-decision")
    async def decide_dev_design(cycle_id: str, request: Request, s: Scope = Depends(scope_dep)) -> dict[str, Any]:  # noqa: B008
        body = await _parse_json_obj(request)
        raw_rev = body.get("designRevision")
        if isinstance(raw_rev, bool) or not isinstance(raw_rev, int) or raw_rev < 1:
            raise HTTPException(422, detail={"error": "DESIGN_REVISION_REQUIRED"})
        cid = str(body.get("selectedCandidateId") or "").strip()
        if not cid:
            raise HTTPException(422, detail={"error": "SELECTED_CANDIDATE_REQUIRED"})
        try:
            dec = await asyncio.to_thread(
                _store().record_design_decision,
                tenant_id=s.tenant_id,
                owner_user_id=s.user_id,
                cycle_id=cycle_id,
                design_revision=raw_rev,
                selected_candidate_id=cid,
                feedback=body.get("feedback"),
                demo_artifact_ref=body.get("demoArtifactRef"),
                demo_artifact_sha256=body.get("demoArtifactSha256"),
            )
        except DevelopmentError as exc:
            _raise_dev_http(exc)
        return {"schemaVersion": "1.0", "design": dec}

    @app.post("/v1/development/cycles/{cycle_id}/demo-decision")
    async def decide_dev_demo(cycle_id: str, request: Request, s: Scope = Depends(scope_dep)) -> dict[str, Any]:  # noqa: B008
        body = await _parse_json_obj(request)
        raw_rev = body.get("designRevision")
        if isinstance(raw_rev, bool) or not isinstance(raw_rev, int) or raw_rev < 1:
            raise HTTPException(422, detail={"error": "DESIGN_REVISION_REQUIRED"})
        decision = str(body.get("decision") or "").strip()
        digest = str(body.get("demoSnapshotDigest") or "").strip()
        try:
            dec = await asyncio.to_thread(
                _store().record_demo_decision,
                tenant_id=s.tenant_id,
                owner_user_id=s.user_id,
                cycle_id=cycle_id,
                design_revision=raw_rev,
                decision=decision,
                demo_snapshot_digest=digest,
                feedback=body.get("feedback"),
            )
        except DevelopmentError as exc:
            _raise_dev_http(exc)
        return {
            "schemaVersion": "1.0",
            "design": dec,
            "grantsExternalApproval": False,
        }

    # ------------------------------------------------------------------ Preview Leases & Browser Debug (DEV-06 / R2-01 / R2-06)
    @app.post("/v1/development/cycles/{cycle_id}/previews", status_code=201)
    async def issue_dev_preview(cycle_id: str, request: Request, s: Scope = Depends(scope_dep)) -> dict[str, Any]:  # noqa: B008
        body = await _parse_json_obj(request)
        raw_port = body.get("targetPort")
        raw_ttl = body.get("ttlSeconds", 120)
        preview_instance_id = body.get("previewInstanceId")
        if isinstance(raw_port, bool) or not isinstance(raw_port, int):
            raise HTTPException(422, detail={"error": "TARGET_PORT_REQUIRED"})
        if isinstance(raw_ttl, bool) or not isinstance(raw_ttl, int):
            raise HTTPException(422, detail={"error": "INVALID_TTL"})
        if preview_instance_id is not None and not is_uuid7(str(preview_instance_id)):
            raise HTTPException(422, detail={"error": "INVALID_PREVIEW_INSTANCE_ID"})
        cy_dto = await asyncio.to_thread(
            _store().get_cycle, tenant_id=s.tenant_id, owner_user_id=s.user_id, cycle_id=cycle_id
        )
        if cy_dto is None:
            raise HTTPException(404)
        proj_scope = ProjectScope(
            tenant_id=s.tenant_id,
            owner_user_id=s.user_id,
            project_id=cy_dto["projectId"],
            workspace_id=cy_dto["workspaceId"],
            repository=cy_dto["repository"],
            branch=cy_dto["branch"],
            base_sha=cy_dto["baseSha"],
            workspace_revision="1",
        )
        try:
            cycle_obj = await asyncio.to_thread(PostgresCycleStore(get_db()).load, proj_scope, cycle_id)
            lease_id, raw_token = await asyncio.to_thread(
                _previews().issue_lease,
                cycle_obj,
                target_port=raw_port,
                ttl_s=raw_ttl,
                preview_instance_id=str(preview_instance_id) if preview_instance_id else None,
            )
        except DevelopmentError as exc:
            _raise_dev_http(exc)
        return {
            "schemaVersion": "1.0",
            "leaseId": lease_id,
            "previewToken": raw_token,
            "targetPort": raw_port,
            "commitSha": cy_dto["commitSha"],
            "mode": "preview:observe",
        }

    @app.post("/v1/development/previews/{lease_id}/commands")
    async def preview_command(lease_id: str, request: Request, s: Scope = Depends(scope_dep)) -> dict[str, Any]:  # noqa: B008
        if not is_uuid7(lease_id):
            raise HTTPException(404)
        body = await _parse_json_obj(request)
        action = str(body.get("action") or "").strip()
        if action == "revoke":
            revoked = await asyncio.to_thread(_previews().revoke_lease, s.tenant_id, s.user_id, lease_id)
            if not revoked:
                raise HTTPException(404)

            def _emit_revoked() -> None:
                with get_db().tx(tenant=s.tenant_id) as c:
                    append_event(
                        c,
                        tenant_id=s.tenant_id,
                        aggregate_type="workspace",
                        aggregate_id=s.workspace_id,
                        kind="dev.preview_revoked",
                        payload={"leaseId": lease_id},
                        actor={"type": "user", "id": s.user_id},
                        owner_user_id=s.user_id,
                    )

            await asyncio.to_thread(_emit_revoked)
            return {"schemaVersion": "1.0", "leaseId": lease_id, "status": "REVOKED"}

        token = str(body.get("previewToken") or "")
        raw_port = body.get("targetPort")
        expected_cycle_id = str(body["cycleId"]) if body.get("cycleId") else None
        if not token or isinstance(raw_port, bool) or not isinstance(raw_port, int):
            raise HTTPException(422, detail={"error": "PREVIEW_TOKEN_AND_PORT_REQUIRED"})
        try:
            commit_sha = await asyncio.to_thread(
                _previews().verify_lease,
                s.tenant_id,
                s.user_id,
                lease_id,
                token,
                raw_port,
                expected_cycle_id=expected_cycle_id,
            )
        except DevelopmentError as exc:
            _raise_dev_http(exc)

        if action == "observe":
            if body.get("input") is not None or body.get("syntheticAction") is not None:
                raise HTTPException(403, detail={"error": "OBSERVE_MODE_READ_ONLY"})
            return {
                "schemaVersion": "1.0",
                "leaseId": lease_id,
                "action": "observe",
                "commitSha": commit_sha,
                "cdpExposed": False,
            }
        if action == "synthetic_interact":
            syn = str(body.get("syntheticAction") or "").strip()
            if syn not in _SYNTHETIC_DEMO_ACTIONS:
                raise HTTPException(422, detail={"error": "INVALID_SYNTHETIC_ACTION"})
            return {
                "schemaVersion": "1.0",
                "leaseId": lease_id,
                "action": "synthetic_interact",
                "syntheticAction": syn,
                "commitSha": commit_sha,
                "syntheticFixtureOnly": True,
                "cdpExposed": False,
            }
        raise HTTPException(422, detail={"error": "UNSUPPORTED_PREVIEW_ACTION"})

    @app.post("/v1/development/cycles/{cycle_id}/debug", status_code=201)
    async def run_dev_debug(cycle_id: str, request: Request, s: Scope = Depends(scope_dep)) -> dict[str, Any]:  # noqa: B008
        body = await _parse_json_obj(request)
        lease_id = str(body.get("leaseId") or "")
        token = str(body.get("previewToken") or "")
        raw_port = body.get("targetPort")
        path = str(body.get("path") or "/")
        revoke_after = body.get("revokeAfter", True)
        if not is_uuid7(lease_id) or not token or isinstance(raw_port, bool) or not isinstance(raw_port, int):
            raise HTTPException(422, detail={"error": "LEASE_TOKEN_AND_PORT_REQUIRED"})
        if not isinstance(revoke_after, bool):
            raise HTTPException(422, detail={"error": "REVOKE_AFTER_MUST_BE_BOOLEAN"})
        cy_dto = await asyncio.to_thread(
            _store().get_cycle, tenant_id=s.tenant_id, owner_user_id=s.user_id, cycle_id=cycle_id
        )
        if cy_dto is None:
            raise HTTPException(404)
        dbg = AutonomousBrowserDebugger(_previews())
        try:
            report = await asyncio.to_thread(
                dbg.debug_preview,
                tenant_id=s.tenant_id,
                owner_user_id=s.user_id,
                cycle_id=cycle_id,
                lease_id=lease_id,
                token=token,
                port=raw_port,
                task_id=cy_dto["taskId"],
                run_id=cy_dto["runId"],
                path=path,
                revoke_after=revoke_after,
                probe_fn=dev_debug_probe_fn,
            )
            finding = await asyncio.to_thread(
                _store().record_debug_finding,
                tenant_id=s.tenant_id,
                owner_user_id=s.user_id,
                cycle_id=cycle_id,
                preview_lease_id=lease_id,
                report=report,
            )
        except DevelopmentError as exc:
            _raise_dev_http(exc)
        except RuntimeError as exc:
            if str(exc) == "BROWSER_DEBUG_UNAVAILABLE":
                raise HTTPException(503, detail={"error": "BROWSER_DEBUG_UNAVAILABLE"}) from None
            raise
        return {"schemaVersion": "1.0", "finding": finding}

    @app.get("/v1/development/cycles/{cycle_id}/debug-evidence")
    async def list_dev_debug_evidence(cycle_id: str, s: Scope = Depends(scope_dep)) -> dict[str, Any]:  # noqa: B008
        cycle = await asyncio.to_thread(
            _store().get_cycle, tenant_id=s.tenant_id, owner_user_id=s.user_id, cycle_id=cycle_id
        )
        if cycle is None:
            raise HTTPException(404)
        items = await asyncio.to_thread(
            _store().list_debug_findings, tenant_id=s.tenant_id, owner_user_id=s.user_id, cycle_id=cycle_id
        )
        return {"schemaVersion": "1.0", "items": items}

    # ------------------------------------------------------------------ PR Integrations, Reconcile & Guardian (R2-02..R2-13)
    @app.get("/v1/development/projects/{project_id}/integration-receipts")
    async def list_dev_integration_receipts(
        project_id: str,
        limit: int = 50,
        s: Scope = Depends(scope_dep),  # noqa: B008
    ) -> dict[str, Any]:
        st = _store()
        proj = await asyncio.to_thread(
            st.get_project, tenant_id=s.tenant_id, owner_user_id=s.user_id, project_id=project_id
        )
        if proj is None:
            raise HTTPException(404)
        items = await asyncio.to_thread(
            st.list_pr_records, tenant_id=s.tenant_id, owner_user_id=s.user_id, project_id=project_id, limit=limit
        )
        return {"schemaVersion": "1.0", "items": items}

    @app.post("/v1/development/projects/{project_id}/integrations", status_code=201)
    async def create_dev_pr_integration(
        project_id: str,
        request: Request,
        s: Scope = Depends(scope_dep),  # noqa: B008
    ) -> Any:
        body = await _parse_json_obj(request)
        cycle_id = str(body.get("cycleId") or "").strip()
        expected_head_sha = str(body.get("expectedHeadSha") or "").strip()
        intent_id = str(body.get("intentId") or "").strip()
        idem_key = str(request.headers.get("idempotency-key") or body.get("idempotencyKey") or "").strip()
        title = str(body.get("title") or "Autonomous development PR").strip()
        pr_body = str(body.get("body") or "Created under bounded development workflow.").strip()
        draft = body.get("draft", True)
        max_repairs = body.get("maxRepairAttempts", 3)
        raw_req_checks = body.get("requiredChecks") or []
        if not isinstance(draft, bool):
            raise HTTPException(422, detail={"error": "DRAFT_MUST_BE_BOOLEAN"})
        if isinstance(max_repairs, bool) or not isinstance(max_repairs, int) or not 1 <= max_repairs <= 10:
            raise HTTPException(422, detail={"error": "INVALID_MAX_REPAIR_ATTEMPTS"})
        if not isinstance(raw_req_checks, list) or any(not isinstance(x, str) or not x.strip() for x in raw_req_checks):
            raise HTTPException(422, detail={"error": "INVALID_REQUIRED_CHECKS"})
        if not cycle_id or not is_uuid7(intent_id) or not idem_key:
            raise HTTPException(422, detail={"error": "CYCLE_INTENT_AND_IDEMPOTENCY_REQUIRED"})
        try:
            require_sha(expected_head_sha)
        except DevelopmentError as exc:
            _raise_dev_http(exc)

        st = _store()
        proj = await asyncio.to_thread(
            st.get_project, tenant_id=s.tenant_id, owner_user_id=s.user_id, project_id=project_id
        )
        if proj is None:
            raise HTTPException(404)
        cycle = await asyncio.to_thread(st.get_cycle, tenant_id=s.tenant_id, owner_user_id=s.user_id, cycle_id=cycle_id)
        if cycle is None or cycle["projectId"] != project_id:
            raise HTTPException(404)
        if cycle["phase"] != "CODE_READY" or cycle["commitSha"] != expected_head_sha:
            raise HTTPException(409, detail={"error": "CYCLE_NOT_CODE_READY_OR_HEAD_MISMATCH"})

        expected_args = canonical_pr_request(
            project_id=project_id,
            cycle_id=cycle_id,
            repository=proj["repository"],
            branch=cycle["branch"],
            head_sha=expected_head_sha,
            base_branch=proj["defaultBranch"],
            title=title,
            body=pr_body,
            draft=draft,
        )
        expected_sha = pr_request_sha256(expected_args)

        # R2-02 + R2-13: Preflight binding & approved intent check BEFORE reserving dev_pr_records so invalid/tampered
        # requests NEVER consume or poison the intent slot!
        def _preflight() -> dict[str, Any] | None:
            with get_db().tx(tenant=s.tenant_id) as c:
                prior = c.execute(
                    "SELECT * FROM agent.dev_pr_records"
                    " WHERE tenant_id=%s AND (idempotency_key=%s OR intent_id=%s)"
                    " ORDER BY created_at ASC LIMIT 1",
                    (s.tenant_id, idem_key, intent_id),
                ).fetchone()
                if prior is not None:
                    if str(prior["owner_user_id"]) != s.user_id:
                        raise ScopeDenied("pr record belongs to another owner")
                    return {"existing": dict(prior)}
                if not proj.get("repositoryBindingId"):
                    raise ScopeDenied("REPOSITORY_BINDING_REQUIRED")
                bound = c.execute(
                    "SELECT * FROM agent.verify_dev_repository_binding(%s,%s,%s,%s,%s,%s)",
                    (
                        s.tenant_id,
                        s.user_id,
                        s.workspace_id,
                        proj["repositoryBindingId"],
                        proj["repository"],
                        cycle["branch"],
                    ),
                ).fetchone()
                if bound is None:
                    raise ScopeDenied("REPOSITORY_BINDING_UNAUTHORIZED")
                it = c.execute(
                    "SELECT i.task_id, i.run_id, i.tool_key, i.status_code, i.request_sha256,"
                    " i.safe_arguments_json, i.goal_revision AS intent_goal_rev,"
                    " t.owner_user_id, t.workspace_id, t.goal_revision,"
                    " a.decision_code, a.owner_user_id AS apr_owner, a.request_sha256 AS apr_sha,"
                    " a.goal_revision AS apr_goal, (a.expires_at > now()) AS apr_alive"
                    " FROM agent.tool_intents i"
                    " JOIN agent.tasks t ON t.tenant_id=i.tenant_id AND t.id=i.task_id"
                    " LEFT JOIN agent.approval_records a ON a.tenant_id=i.tenant_id AND a.tool_intent_id=i.id"
                    " WHERE i.tenant_id=%s AND i.id=%s",
                    (s.tenant_id, intent_id),
                ).fetchone()
                if (
                    it is None
                    or str(it["tool_key"]) != "dev_github_pr"
                    or str(it["owner_user_id"]) != s.user_id
                    or str(it["workspace_id"]) != s.workspace_id
                    or str(it["task_id"]) != cycle["taskId"]
                    or str(it["run_id"]) != cycle["runId"]
                    or int(it["intent_goal_rev"]) != cycle["goalRevision"]
                    or int(it["goal_revision"]) != cycle["goalRevision"]
                ):
                    raise ScopeDenied("tool intent missing or out of cycle scope")
                if str(it["status_code"]) == "UNKNOWN":
                    raise EffectUnknown("tool intent is UNKNOWN; reconcile before retry")
                safe_args = dict(it["safe_arguments_json"]) if isinstance(it["safe_arguments_json"], dict) else {}
                if safe_args != expected_args or str(it["request_sha256"]) != expected_sha:
                    raise ScopeDenied("PR request arguments or digest mismatch approved tool_intent")
                if (
                    str(it["decision_code"] or "") != "APPROVED"
                    or not it["apr_alive"]
                    or str(it["apr_owner"] or "") != s.user_id
                    or str(it["apr_sha"] or "") != expected_sha
                    or int(it["apr_goal"] or 0) != cycle["goalRevision"]
                ):
                    raise ScopeDenied("explicit owner approval required or expired for GitHub PR creation")
            return None

        try:
            pre = await asyncio.to_thread(_preflight)
        except DevelopmentError as exc:
            _raise_dev_http(exc)

        if pre is not None:
            prior_rec = await asyncio.to_thread(
                st.get_pr_record, tenant_id=s.tenant_id, owner_user_id=s.user_id, record_id=str(pre["existing"]["id"])
            )
            assert prior_rec is not None
            if prior_rec["effectStatus"] in ("EFFECT_UNKNOWN", "PENDING"):
                return JSONResponse(
                    {
                        "schemaVersion": "1.0",
                        "error": "EFFECT_UNKNOWN",
                        "prRecord": prior_rec,
                        "reconcileRequired": True,
                    },
                    status_code=409,
                )
            if prior_rec["effectStatus"] == "FAILED":
                raise HTTPException(409, detail={"error": "INTENT_ALREADY_FAILED", "prRecord": prior_rec})
            return {"schemaVersion": "1.0", "prRecord": prior_rec, "idempotentReplay": True}

        # R2-08: Required CI checks are union of authoritative project policy + stricter request checks
        policy_checks = [
            str(x).strip()
            for x in (proj.get("preferences", {}).get("requiredChecks") or [])
            if isinstance(x, str) and x.strip()
        ]
        merged_req_checks = list(dict.fromkeys([*policy_checks, *(x.strip() for x in raw_req_checks)]))

        try:
            record, created = await asyncio.to_thread(
                st.begin_pr_record,
                tenant_id=s.tenant_id,
                owner_user_id=s.user_id,
                project_id=project_id,
                cycle_id=cycle_id,
                repository=proj["repository"],
                branch=cycle["branch"],
                head_sha=expected_head_sha,
                idempotency_key=idem_key,
                intent_id=intent_id,
                git_push_receipt_ref=cycle.get("pushReceipt"),
                required_checks=merged_req_checks,
                max_repair_attempts=max_repairs,
            )
        except DevelopmentError as exc:
            _raise_dev_http(exc)
        if not created:
            if record["effectStatus"] in ("EFFECT_UNKNOWN", "PENDING"):
                return JSONResponse(
                    {"schemaVersion": "1.0", "error": "EFFECT_UNKNOWN", "prRecord": record, "reconcileRequired": True},
                    status_code=409,
                )
            if record["effectStatus"] == "FAILED":
                raise HTTPException(409, detail={"error": "INTENT_ALREADY_FAILED", "prRecord": record})
            return {"schemaVersion": "1.0", "prRecord": record, "idempotentReplay": True}

        try:
            gw_outcome = await _dispatch_pr_via_gateway(
                tenant_id=s.tenant_id,
                owner_user_id=s.user_id,
                workspace_id=s.workspace_id,
                intent_id=intent_id,
                expected_args=expected_args,
            )
        except RpcError as exc:
            await asyncio.to_thread(
                st.settle_pr_record,
                tenant_id=s.tenant_id,
                owner_user_id=s.user_id,
                record_id=record["id"],
                effect_status="FAILED",
                last_error_code=f"gateway:{exc.code}"[:120],
            )
            status_code = 409 if exc.code in ("INTENT_CONFLICT", "FENCED", "STALE_GOAL") else 403
            raise HTTPException(status_code, detail={"error": exc.code}) from None
        except HTTPException:
            await asyncio.to_thread(
                st.settle_pr_record,
                tenant_id=s.tenant_id,
                owner_user_id=s.user_id,
                record_id=record["id"],
                effect_status="FAILED",
                last_error_code="TOOL_GATEWAY_RPC_UNAVAILABLE",
            )
            raise
        except Exception as exc:
            unknown_rec = await asyncio.to_thread(
                st.settle_pr_record,
                tenant_id=s.tenant_id,
                owner_user_id=s.user_id,
                record_id=record["id"],
                effect_status="EFFECT_UNKNOWN",
                last_error_code=f"gateway_io:{type(exc).__name__}"[:120],
            )
            return JSONResponse(
                {"schemaVersion": "1.0", "error": "EFFECT_UNKNOWN", "prRecord": unknown_rec, "reconcileRequired": True},
                status_code=502,
            )

        gw_status = str(gw_outcome.get("status") or "")
        gw_result = dict(gw_outcome.get("result") or {})
        if gw_status == "CONFIRMED":
            settled = await asyncio.to_thread(
                st.settle_pr_record,
                tenant_id=s.tenant_id,
                owner_user_id=s.user_id,
                record_id=record["id"],
                effect_status="CONFIRMED",
                pr_number=gw_result.get("prNumber"),
                pr_url=gw_result.get("prUrl"),
                ci_state="PENDING",
            )
            return {"schemaVersion": "1.0", "prRecord": settled}
        if gw_status == "UNKNOWN":
            unknown_rec = await asyncio.to_thread(
                st.settle_pr_record,
                tenant_id=s.tenant_id,
                owner_user_id=s.user_id,
                record_id=record["id"],
                effect_status="EFFECT_UNKNOWN",
                last_error_code=str(gw_result.get("note") or "EFFECT_UNKNOWN")[:120],
            )
            return JSONResponse(
                {"schemaVersion": "1.0", "error": "EFFECT_UNKNOWN", "prRecord": unknown_rec, "reconcileRequired": True},
                status_code=502,
            )

        err_msg = str(gw_result.get("error") or gw_status or "DISPATCH_FAILED")[:120]
        await asyncio.to_thread(
            st.settle_pr_record,
            tenant_id=s.tenant_id,
            owner_user_id=s.user_id,
            record_id=record["id"],
            effect_status="FAILED",
            last_error_code=err_msg,
        )
        if "STALE_REMOTE_HEAD" in err_msg or gw_status in ("STALE_GOAL", "EXPIRED"):
            raise HTTPException(409, detail={"error": err_msg})
        raise HTTPException(403, detail={"error": err_msg})

    @app.post("/v1/development/prs/{pr_record_id}/reconcile")
    async def reconcile_dev_pr(pr_record_id: str, s: Scope = Depends(scope_dep)) -> dict[str, Any]:  # noqa: B008
        if not is_uuid7(pr_record_id):
            raise HTTPException(404)
        st = _store()
        rec = await asyncio.to_thread(
            st.get_pr_record, tenant_id=s.tenant_id, owner_user_id=s.user_id, record_id=pr_record_id
        )
        if rec is None:
            raise HTTPException(404)
        if not rec["intentId"]:
            raise HTTPException(409, detail={"error": "PR_RECONCILE_SCOPE_MISSING"})
        # R2-05: Never reconcile FAILED records; only reconcile legitimately dispatched PENDING / EFFECT_UNKNOWN records.
        if rec["effectStatus"] == "FAILED":
            raise HTTPException(409, detail={"error": "PR_RECORD_FAILED_NOT_RECONCILABLE"})
        if rec["effectStatus"] == "CONFIRMED":
            return {"schemaVersion": "1.0", "reconciled": True, "prRecord": rec}

        try:
            rec_out = await _reconcile_via_gateway(
                tenant_id=s.tenant_id,
                owner_user_id=s.user_id,
                intent_id=rec["intentId"],
            )
        except RpcError as exc:
            raise HTTPException(409 if exc.code == "NOT_IN_HEAD" else 403, detail={"error": exc.code}) from None

        if str(rec_out.get("status") or "") != "CONFIRMED":
            return {"schemaVersion": "1.0", "reconciled": False, "prRecord": rec}

        # Read canonical receipt & PR metadata settled by ToolGateway into tool_intents.result_json
        def _read_settled_intent() -> dict[str, Any] | None:
            with get_db().tx(tenant=s.tenant_id) as c:
                row = c.execute(
                    "SELECT status_code, result_json FROM agent.tool_intents WHERE tenant_id=%s AND id=%s",
                    (s.tenant_id, rec["intentId"]),
                ).fetchone()
                return dict(row) if row is not None else None

        it_row = await asyncio.to_thread(_read_settled_intent)
        res_json = dict(it_row["result_json"] or {}) if it_row and isinstance(it_row.get("result_json"), dict) else {}
        if it_row is None or str(it_row["status_code"]) != "CONFIRMED" or not res_json.get("prNumber"):
            return {"schemaVersion": "1.0", "reconciled": False, "prRecord": rec}

        updated = await asyncio.to_thread(
            st.settle_pr_record,
            tenant_id=s.tenant_id,
            owner_user_id=s.user_id,
            record_id=rec["id"],
            effect_status="CONFIRMED",
            pr_number=int(res_json["prNumber"]),
            pr_url=str(res_json.get("prUrl") or ""),
            ci_state="PENDING",
            event_kind="dev.pr_reconciled",
        )
        return {"schemaVersion": "1.0", "reconciled": True, "prRecord": updated}

    @app.post("/v1/development/prs/{pr_record_id}/guardian-check")
    async def guardian_check_dev_pr(
        pr_record_id: str,
        request: Request,
        s: Scope = Depends(scope_dep),  # noqa: B008
    ) -> dict[str, Any]:
        if not is_uuid7(pr_record_id):
            raise HTTPException(404)
        body = await _parse_json_obj(request)
        claim_repair = body.get("claimRepair", False)
        if not isinstance(claim_repair, bool):
            raise HTTPException(422, detail={"error": "CLAIM_REPAIR_MUST_BE_BOOLEAN"})
        st = _store()
        rec = await asyncio.to_thread(
            st.get_pr_record, tenant_id=s.tenant_id, owner_user_id=s.user_id, record_id=pr_record_id
        )
        if rec is None:
            raise HTTPException(404)
        if not rec["prNumber"] or not rec["intentId"]:
            raise HTTPException(409, detail={"error": "PR_NOT_CONFIRMED_YET"})
        proj = await asyncio.to_thread(
            st.get_project, tenant_id=s.tenant_id, owner_user_id=s.user_id, project_id=rec["projectId"]
        )
        if proj is None:
            raise HTTPException(404)
        proj_scope = ProjectScope(
            tenant_id=s.tenant_id,
            owner_user_id=s.user_id,
            project_id=rec["projectId"],
            workspace_id=s.workspace_id,
            repository=rec["repository"],
            branch=rec["branch"],
            base_sha=rec["headSha"],
            workspace_revision="1",
        )
        policy_checks = [
            str(x).strip()
            for x in (proj.get("preferences", {}).get("requiredChecks") or [])
            if isinstance(x, str) and x.strip()
        ]
        effective_required_checks = list(dict.fromkeys([*policy_checks, *rec["requiredChecks"]]))
        gh_reader = _github_reader(proj["githubApiBase"] or "", repository_binding_id=proj.get("repositoryBindingId"))
        guardian = PrGuardian(gh_reader.read_get)
        try:
            ev = await asyncio.to_thread(
                guardian.inspect_pr,
                scope=proj_scope,
                intent_ref=rec["intentId"],
                pr_number=rec["prNumber"],
                head_sha=rec["headSha"],
                repair_attempts=rec["repairAttempts"],
                max_repair_attempts=rec["maxRepairAttempts"],
                required_checks=effective_required_checks,
                base_branch=proj["defaultBranch"],
            )
        except DevelopmentError as exc:
            _raise_dev_http(exc)

        repair_claimed = False
        if claim_repair and ev.ci_state in ("FAIL", "CONFLICT"):
            updated, repair_claimed = await asyncio.to_thread(
                st.claim_guardian_repair_slot,
                tenant_id=s.tenant_id,
                owner_user_id=s.user_id,
                record_id=rec["id"],
                ci_state=ev.ci_state,
            )
        else:
            updated = await asyncio.to_thread(
                st.settle_pr_record,
                tenant_id=s.tenant_id,
                owner_user_id=s.user_id,
                record_id=rec["id"],
                effect_status=rec["effectStatus"],
                ci_state=ev.ci_state,
                event_kind="dev.guardian_escalated" if ev.escalated else "dev.pr_reconciled",
            )
        return {
            "schemaVersion": "1.0",
            "prRecord": updated,
            "repairClaimed": repair_claimed,
            "evaluation": {
                "ciState": updated["ciState"],
                "failingChecks": list(ev.failing_checks),
                "missingRequiredChecks": list(ev.missing_required_checks),
                "observedHeadSha": ev.observed_head_sha,
                "mergeableState": ev.mergeable_state,
                "shouldRepair": repair_claimed if claim_repair else ev.should_repair,
                "escalated": updated["ciState"] == "ESCALATED_LIMIT_REACHED",
            },
        }
