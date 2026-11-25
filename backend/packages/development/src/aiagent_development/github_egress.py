"""Sentinel Egress Proxy-enforced GitHub PR creation, reconciliation, and read-only CI queries.

Hardens P0-01, P1-03, P1-04, P1-05, and P2-13:
- Canonical request digest & exact safe_arguments_json match against approved tool_intents / approval_records
  (project, cycle, task, run, goal_revision, repository, branch, headSha, baseBranch, title, body, draft).
- Sentinel permit binds exact target_path and body_sha256, verified again at EgressProxy send time.
- Verifies remote branch ref SHA (or attested push receipt) BEFORE POST /pulls, sends standard GitHub API
  fields (no synthetic head_sha override), and asserts response head.sha == expected_head_sha.
- Treats post-send disconnects, malformed JSON, and unexpected status codes strictly as EffectUnknown,
  and synchronizes canonical tool_intents / side_effect_records when tool_gateway_db is supplied.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from aiagent_core import config, control
from aiagent_core.db import Database
from aiagent_core.egress_http import ForwardProxyError, send_via_forward_proxy
from aiagent_domain import canonical_json, is_uuid7, sha256_hex, uuid7

from .contracts import EffectUnknown, ProjectScope, ScopeDenied, StaleReference, StateConflict, require_sha


def canonical_pr_request(
    *,
    project_id: str,
    cycle_id: str,
    repository: str,
    branch: str,
    head_sha: str,
    base_branch: str,
    title: str,
    body: str,
    draft: bool,
) -> dict[str, Any]:
    """Canonical argument object whose SHA-256 must match tool_intents & approval_records."""
    require_sha(head_sha)
    return {
        "projectId": project_id,
        "cycleId": cycle_id,
        "repository": repository,
        "branch": branch,
        "headSha": head_sha,
        "baseBranch": base_branch,
        "title": title[:200],
        "body": body[:2000],
        "draft": bool(draft),
    }


def pr_request_sha256(req: Mapping[str, Any]) -> str:
    return sha256_hex(dict(req))


def default_github_token_provider(repository: str, *, repository_binding_id: str | None = None) -> str | None:
    """Resolve GitHub token from repository-binding-scoped secret JSON or AIAGENT_GITHUB_TOKEN (P2-13)."""
    raw_map = config.secret("AIAGENT_GITHUB_TOKENS_JSON", required=False)
    if raw_map is not None:
        try:
            parsed = json.loads(raw_map.reveal())
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            if repository_binding_id and isinstance(parsed.get(repository_binding_id), str):
                tok = str(parsed[repository_binding_id]).strip()
                if tok:
                    return tok
            if isinstance(parsed.get(repository), str):
                tok = str(parsed[repository]).strip()
                if tok:
                    return tok
    single = config.secret("AIAGENT_GITHUB_TOKEN", required=False)
    if single is not None:
        tok = single.reveal().strip()
        if tok:
            return tok
    return None


def _claim_from_row(row: Mapping[str, Any], tenant_id: str) -> control.Claim:
    return control.Claim(
        tenant_id=tenant_id,
        owner_user_id=str(row["owner_user_id"]),
        task_id=str(row["task_id"]),
        run_id=str(row["run_id"]),
        lease_epoch=int(row["lease_epoch"]),
        goal_revision=int(row["goal_revision"]),
        worker_ref=str(row["lease_owner_ref"]),
        restore_incarnation=int(row["restore_incarnation"]),
    )


class GitHubEgressClient:
    """Strictly Sentinel-mediated GitHub REST transport (never direct internet, never auto-merge)."""

    def __init__(
        self,
        *,
        db: Database,
        sentinel: Any,
        github_api_base: str,
        egress_proxy: str | None = None,
        token_provider: Callable[[str], str | None] | None = None,
        tool_gateway_db: Database | None = None,
        timeout_s: float = 15.0,
    ) -> None:
        self._db = db
        self._sentinel = sentinel
        self._github_api_base = github_api_base.rstrip("/")
        self._egress_proxy = egress_proxy
        self._token_provider = token_provider or default_github_token_provider
        self._tool_gateway_db = tool_gateway_db
        self._timeout_s = timeout_s

    def _sync_canonical_ledger(
        self,
        *,
        tenant_id: str,
        intent_id: str,
        request_sha: str,
        intent_status: str,
        effect_outcome: str,
        receipt_ref: str | None = None,
    ) -> None:
        """Keep canonical tool_intents & side_effect_records synchronized when tool_gateway DB is wired."""
        if self._tool_gateway_db is None or not is_uuid7(intent_id):
            return
        with self._tool_gateway_db.tx() as c:
            c.execute(
                "UPDATE agent.tool_intents SET status_code=%s, updated_at=now(), record_version=record_version+1"
                " WHERE tenant_id=%s AND id=%s",
                (intent_status, tenant_id, intent_id),
            )
            c.execute(
                "INSERT INTO agent.side_effect_records(tenant_id, id, tool_intent_id, effect_key,"
                " request_sha256, outcome_code, attempt_fence, external_receipt_ref, last_checked_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,1,%s,now())"
                " ON CONFLICT (tenant_id, tool_intent_id) DO UPDATE"
                " SET outcome_code=EXCLUDED.outcome_code,"
                " external_receipt_ref=COALESCE(EXCLUDED.external_receipt_ref, agent.side_effect_records.external_receipt_ref),"
                " last_checked_at=now(), updated_at=now()",
                (tenant_id, uuid7(), intent_id, f"dev_github_pr:{intent_id}", request_sha, effect_outcome, receipt_ref),
            )

    def _verify_intent_and_claim(  # noqa: PLR0912
        self,
        scope: ProjectScope,
        intent_id: str,
        *,
        require_owner_approval: bool,
        expected_task_id: str | None = None,
        expected_run_id: str | None = None,
        expected_goal_revision: int | None = None,
        expected_args: Mapping[str, Any] | None = None,
    ) -> tuple[control.Claim, str]:
        if not is_uuid7(intent_id):
            raise ScopeDenied("canonical uuid7 tool_intent_id required")
        with self._db.tx(tenant=scope.tenant_id) as c:
            if not control.scope_active(c, scope.tenant_id, scope.owner_user_id, scope.workspace_id):
                raise ScopeDenied("tenant/owner/workspace scope inactive")
            row = c.execute(
                "SELECT i.task_id, i.run_id, i.tool_key, i.status_code, i.request_sha256,"
                " i.safe_arguments_json, i.goal_revision AS intent_goal_rev,"
                " r.lease_epoch, r.lease_owner_ref,"
                " (r.lease_expires_at > now()) AS lease_alive, r.cancel_requested, r.pause_requested,"
                " t.owner_user_id, t.workspace_id, t.goal_revision, r.restore_incarnation,"
                " se.outcome_code AS ledger_outcome"
                " FROM agent.tool_intents i"
                " JOIN agent.runs r ON r.tenant_id=i.tenant_id AND r.id=i.run_id"
                " JOIN agent.tasks t ON t.tenant_id=i.tenant_id AND t.id=i.task_id"
                " LEFT JOIN agent.side_effect_records se ON se.tenant_id=i.tenant_id AND se.tool_intent_id=i.id"
                " WHERE i.tenant_id=%s AND i.id=%s",
                (scope.tenant_id, intent_id),
            ).fetchone()
            if (
                row is None
                or str(row["tool_key"]) != "dev_github_pr"
                or str(row["owner_user_id"]) != scope.owner_user_id
                or str(row["workspace_id"]) != scope.workspace_id
            ):
                raise ScopeDenied("tool intent missing, wrong tool_key, or out of scope")

            req_sha = str(row["request_sha256"])
            if expected_task_id is not None and str(row["task_id"]) != expected_task_id:
                raise ScopeDenied("tool intent task_id does not match cycle")
            if expected_run_id is not None and str(row["run_id"]) != expected_run_id:
                raise ScopeDenied("tool intent run_id does not match cycle")
            if expected_goal_revision is not None and (
                int(row["intent_goal_rev"]) != expected_goal_revision
                or int(row["goal_revision"]) != expected_goal_revision
            ):
                raise ScopeDenied("tool intent goal_revision does not match cycle")

            if require_owner_approval:
                if (
                    not row["lease_owner_ref"]
                    or not row["lease_alive"]
                    or row["cancel_requested"]
                    or row["pause_requested"]
                ):
                    raise ScopeDenied("run lease inactive or cancelled")
                if str(row["status_code"]) == "UNKNOWN" or str(row.get("ledger_outcome") or "") == "UNKNOWN":
                    raise EffectUnknown("tool intent or side_effect_record is UNKNOWN; reconcile before retry")
                if str(row["status_code"]) != "SENDING":
                    raise ScopeDenied(f"tool intent status {row['status_code']} not admitted for PR creation")
                if expected_args is None:
                    raise ScopeDenied("canonical approved PR arguments required")
                expected_sha = pr_request_sha256(expected_args)
                safe_args = dict(row["safe_arguments_json"]) if isinstance(row["safe_arguments_json"], dict) else {}
                if safe_args != dict(expected_args) or req_sha != expected_sha:
                    raise ScopeDenied("PR request arguments or digest mismatch approved tool_intent")

                app = c.execute(
                    "SELECT decision_code, owner_user_id, request_sha256, goal_revision,"
                    " (expires_at > now()) AS alive"
                    " FROM agent.approval_records"
                    " WHERE tenant_id=%s AND tool_intent_id=%s",
                    (scope.tenant_id, intent_id),
                ).fetchone()
                if (
                    app is None
                    or str(app["decision_code"]) != "APPROVED"
                    or str(app["owner_user_id"]) != scope.owner_user_id
                    or not app["alive"]
                    or str(app["request_sha256"]) != expected_sha
                    or int(app["goal_revision"]) != int(row["goal_revision"])
                ):
                    raise ScopeDenied("explicit owner approval mismatch or expired for GitHub PR creation")
            return _claim_from_row(row, scope.tenant_id), req_sha

    def _request_headers(self, repository: str, has_body: bool) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "aiagent-dev-egress/1.0",
        }
        if has_body:
            headers["Content-Type"] = "application/json"
        if self._token_provider is not None:
            tok = self._token_provider(repository)
            if tok:
                headers["Authorization"] = f"Bearer {tok}"
        return headers

    @staticmethod
    def _parse_json_response(raw_body: str, *, post_send_strict: bool) -> dict[str, Any]:
        if not raw_body.strip():
            return {}
        try:
            obj = json.loads(raw_body)
        except ValueError as exc:
            if post_send_strict:
                raise EffectUnknown("malformed JSON response after outbound GitHub write; reconcile required") from exc
            raise EffectUnknown("malformed JSON response from GitHub") from exc
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, list):
            return {"items": obj}
        if post_send_strict:
            raise EffectUnknown("unexpected JSON root after outbound GitHub write; reconcile required")
        return {}

    def _forward(
        self,
        *,
        scope: ProjectScope,
        intent_id: str,
        method: str,
        path: str,
        body_bytes: bytes | None,
        reconcile: bool,
        expected_task_id: str | None = None,
        expected_run_id: str | None = None,
        expected_goal_revision: int | None = None,
        expected_args: Mapping[str, Any] | None = None,
        base_override: str | None = None,
    ) -> tuple[int, dict[str, Any], str]:
        base = (base_override or self._github_api_base).rstrip("/")
        proxy_url = self._egress_proxy or os.environ.get("AIAGENT_EGRESS_PROXY") or ""
        if not base or not proxy_url or self._sentinel is None:
            raise ScopeDenied("github_api_base, AIAGENT_EGRESS_PROXY, and Sentinel are required for GitHub egress")

        pu = urlsplit(base)
        if pu.scheme not in ("http", "https") or not pu.hostname:
            raise ScopeDenied("invalid github_api_base URL")
        port = pu.port or (443 if pu.scheme == "https" else 80)
        host_hdr = pu.hostname if pu.port is None else f"{pu.hostname}:{port}"

        claim, req_sha = self._verify_intent_and_claim(
            scope,
            intent_id,
            require_owner_approval=not reconcile,
            expected_task_id=expected_task_id,
            expected_run_id=expected_run_id,
            expected_goal_revision=expected_goal_revision,
            expected_args=expected_args,
        )
        raw_sentinel = getattr(self._sentinel, "s", self._sentinel)
        body_sha = hashlib.sha256(body_bytes or b"").hexdigest()
        permit = (
            raw_sentinel.authorize_reconcile(scope.tenant_id, intent_id, pu.scheme, pu.hostname, port, target_path=path)
            if reconcile
            else raw_sentinel.authorize(
                claim,
                intent_id,
                pu.scheme,
                pu.hostname,
                port,
                method,
                target_path=path,
                body_sha256=body_sha,
            )
        )
        try:
            st, _, raw_body = asyncio.run(
                send_via_forward_proxy(
                    proxy_url,
                    identity="tool_gateway",
                    permit=permit,
                    method=method,
                    target_url=f"{base}{path}",
                    host_header=host_hdr,
                    headers=self._request_headers(scope.repository, body_bytes is not None),
                    body=body_bytes,
                    timeout_s=self._timeout_s,
                )
            )
        except ForwardProxyError as exc:
            if exc.post_send:
                if not reconcile:
                    self._sync_canonical_ledger(
                        tenant_id=scope.tenant_id,
                        intent_id=intent_id,
                        request_sha=req_sha,
                        intent_status="UNKNOWN",
                        effect_outcome="UNKNOWN",
                    )
                raise EffectUnknown(f"GitHub request outcome uncertain ({exc.code})") from exc
            raise ScopeDenied(f"Sentinel proxy denied before send ({exc.code})") from exc

        parsed = self._parse_json_response(raw_body, post_send_strict=not reconcile)
        return st, parsed, req_sha

    def read_get(
        self, scope: ProjectScope, intent_id: str, path: str, *, base_override: str | None = None
    ) -> tuple[int, dict[str, Any]]:
        st, data, _ = self._forward(
            scope=scope,
            intent_id=intent_id,
            method="GET",
            path=path,
            body_bytes=None,
            reconcile=True,
            base_override=base_override,
        )
        return st, data

    def verify_remote_branch_head(
        self,
        *,
        scope: ProjectScope,
        intent_id: str,
        expected_head_sha: str,
        base_override: str | None = None,
    ) -> None:
        """Read-only Sentinel GET verifying remote refs/heads/<branch> points at exact verified commit SHA (P1-04)."""
        require_sha(expected_head_sha)
        st, data = self.read_get(
            scope,
            intent_id,
            f"/repos/{scope.repository}/git/ref/heads/{scope.branch}",
            base_override=base_override,
        )
        if st != 200:
            raise StaleReference(f"remote branch ref unavailable (HTTP {st}); refuse PR creation")
        obj = data.get("object")
        remote_sha = str(obj.get("sha") or "") if isinstance(obj, dict) else ""
        if remote_sha != expected_head_sha:
            raise StaleReference(
                f"remote branch head {remote_sha[:12]} does not match verified SHA {expected_head_sha[:12]}"
            )

    def create_pull_request(
        self,
        *,
        scope: ProjectScope,
        cycle_id: str,
        task_id: str,
        run_id: str,
        goal_revision: int,
        head_sha: str,
        intent_id: str,
        base_branch: str = "main",
        title: str = "Autonomous development change",
        body: str = "Generated under bounded development workflow.",
        draft: bool = True,
        base_override: str | None = None,
    ) -> dict[str, Any]:
        require_sha(head_sha)
        approved_args = canonical_pr_request(
            project_id=scope.project_id,
            cycle_id=cycle_id,
            repository=scope.repository,
            branch=scope.branch,
            head_sha=head_sha,
            base_branch=base_branch,
            title=title,
            body=body,
            draft=draft,
        )
        # Preflight approval & exact argument check BEFORE any remote ref query or POST
        _, req_sha = self._verify_intent_and_claim(
            scope,
            intent_id,
            require_owner_approval=True,
            expected_task_id=task_id,
            expected_run_id=run_id,
            expected_goal_revision=goal_revision,
            expected_args=approved_args,
        )
        # P1-04: verify remote branch HEAD matches the verified SHA before creating PR
        self.verify_remote_branch_head(
            scope=scope,
            intent_id=intent_id,
            expected_head_sha=head_sha,
            base_override=base_override,
        )

        # Standard GitHub REST API payload (no fake head_sha override field)
        payload = canonical_json(
            {
                "title": approved_args["title"],
                "body": approved_args["body"],
                "head": scope.branch,
                "base": base_branch,
                "draft": bool(draft),
            }
        ).encode("utf-8")
        try:
            st, data, req_sha = self._forward(
                scope=scope,
                intent_id=intent_id,
                method="POST",
                path=f"/repos/{scope.repository}/pulls",
                body_bytes=payload,
                reconcile=False,
                expected_task_id=task_id,
                expected_run_id=run_id,
                expected_goal_revision=goal_revision,
                expected_args=approved_args,
                base_override=base_override,
            )
        except EffectUnknown:
            self._sync_canonical_ledger(
                tenant_id=scope.tenant_id,
                intent_id=intent_id,
                request_sha=req_sha,
                intent_status="UNKNOWN",
                effect_outcome="UNKNOWN",
            )
            raise

        if st in (200, 201) and isinstance(data.get("html_url"), str):
            head_obj = data.get("head")
            actual_sha = str(head_obj.get("sha") or "") if isinstance(head_obj, dict) else ""
            if actual_sha != head_sha:
                self._sync_canonical_ledger(
                    tenant_id=scope.tenant_id,
                    intent_id=intent_id,
                    request_sha=req_sha,
                    intent_status="UNKNOWN",
                    effect_outcome="UNKNOWN",
                )
                raise EffectUnknown(
                    f"created PR head.sha {actual_sha[:12]} mismatched expected {head_sha[:12]}; reconcile required"
                )
            raw_num = data.get("number")
            pr_num = int(raw_num) if isinstance(raw_num, int) and raw_num >= 1 else None
            self._sync_canonical_ledger(
                tenant_id=scope.tenant_id,
                intent_id=intent_id,
                request_sha=req_sha,
                intent_status="CONFIRMED",
                effect_outcome="CONFIRMED",
                receipt_ref=f"pr:{pr_num}" if pr_num else str(data["html_url"])[:120],
            )
            return {
                "prNumber": pr_num,
                "prUrl": str(data["html_url"]),
                "headSha": actual_sha,
                "draft": bool(data.get("draft", draft)),
            }
        if st == 422:
            self._sync_canonical_ledger(
                tenant_id=scope.tenant_id,
                intent_id=intent_id,
                request_sha=req_sha,
                intent_status="FAILED",
                effect_outcome="FAILED",
            )
            raise StateConflict(f"GitHub rejected PR creation (HTTP 422): {str(data.get('message', ''))[:120]}")
        self._sync_canonical_ledger(
            tenant_id=scope.tenant_id,
            intent_id=intent_id,
            request_sha=req_sha,
            intent_status="UNKNOWN",
            effect_outcome="UNKNOWN",
        )
        raise EffectUnknown(f"unexpected GitHub PR status={st}; reconcile required")

    def reconcile_pull_request(
        self,
        *,
        scope: ProjectScope,
        head_sha: str,
        intent_id: str,
        base_override: str | None = None,
    ) -> dict[str, Any] | None:
        require_sha(head_sha)
        owner = scope.repository.split("/", 1)[0]
        st, data, req_sha = self._forward(
            scope=scope,
            intent_id=intent_id,
            method="GET",
            path=f"/repos/{scope.repository}/pulls?state=open&head={owner}:{scope.branch}",
            body_bytes=None,
            reconcile=True,
            base_override=base_override,
        )
        if st != 200:
            raise EffectUnknown(f"GitHub PR reconcile returned status={st}")
        items = data.get("items") or []
        if not isinstance(items, list):
            return None
        for item in items:
            if not isinstance(item, dict):
                continue
            head_obj = item.get("head")
            sha_val = str(head_obj.get("sha", "")) if isinstance(head_obj, dict) else ""
            if sha_val == head_sha:
                raw_num = item.get("number")
                pr_num = int(raw_num) if isinstance(raw_num, int) and raw_num >= 1 else None
                self._sync_canonical_ledger(
                    tenant_id=scope.tenant_id,
                    intent_id=intent_id,
                    request_sha=req_sha,
                    intent_status="CONFIRMED",
                    effect_outcome="RECONCILED_SENT",
                    receipt_ref=f"pr:{pr_num}" if pr_num else str(item.get("html_url") or "")[:120],
                )
                return {
                    "prNumber": pr_num,
                    "prUrl": str(item.get("html_url") or ""),
                    "headSha": sha_val,
                    "state": str(item.get("state") or "open"),
                    "mergeable": item.get("mergeable"),
                }
        return None
