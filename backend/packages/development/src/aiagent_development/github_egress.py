"""Sentinel Egress Proxy-enforced GitHub PR creation, reconciliation, and read-only CI queries.

Complements PR #110's isolated Git CAS commit worker (`git_provider_adapter.py`, which handles
local/isolated commit CAS & receipt verification) by routing external GitHub REST API calls
exclusively through Sentinel authorization + EgressProxy forward-proxy with canonical
tool_intents / approval_records verification and EFFECT_UNKNOWN reconciliation.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from aiagent_core import control
from aiagent_core.db import Database
from aiagent_core.egress_http import ForwardProxyError, send_via_forward_proxy
from aiagent_domain import canonical_json, is_uuid7

from .contracts import EffectUnknown, ProjectScope, ScopeDenied, StateConflict, require_sha


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
        timeout_s: float = 15.0,
    ) -> None:
        self._db = db
        self._sentinel = sentinel
        self._github_api_base = github_api_base.rstrip("/")
        self._egress_proxy = egress_proxy
        self._token_provider = token_provider
        self._timeout_s = timeout_s

    def _verify_intent_and_claim(
        self,
        scope: ProjectScope,
        intent_id: str,
        *,
        require_owner_approval: bool,
    ) -> control.Claim:
        if not is_uuid7(intent_id):
            raise ScopeDenied("canonical uuid7 tool_intent_id required")
        with self._db.tx(tenant=scope.tenant_id) as c:
            if not control.scope_active(c, scope.tenant_id, scope.owner_user_id, scope.workspace_id):
                raise ScopeDenied("tenant/owner/workspace scope inactive")
            row = c.execute(
                "SELECT i.task_id, i.run_id, i.status_code, r.lease_epoch, r.lease_owner_ref,"
                " (r.lease_expires_at > now()) AS lease_alive, r.cancel_requested, r.pause_requested,"
                " t.owner_user_id, t.workspace_id, t.goal_revision, r.restore_incarnation"
                " FROM agent.tool_intents i"
                " JOIN agent.runs r ON r.tenant_id=i.tenant_id AND r.id=i.run_id"
                " JOIN agent.tasks t ON t.tenant_id=i.tenant_id AND t.id=i.task_id"
                " WHERE i.tenant_id=%s AND i.id=%s",
                (scope.tenant_id, intent_id),
            ).fetchone()
            if (
                row is None
                or str(row["owner_user_id"]) != scope.owner_user_id
                or str(row["workspace_id"]) != scope.workspace_id
            ):
                raise ScopeDenied("tool intent missing or out of scope")
            if require_owner_approval:
                if (
                    not row["lease_owner_ref"]
                    or not row["lease_alive"]
                    or row["cancel_requested"]
                    or row["pause_requested"]
                ):
                    raise ScopeDenied("run lease inactive or cancelled")
                if str(row["status_code"]) == "UNKNOWN":
                    raise EffectUnknown("tool intent is UNKNOWN; reconcile before retry")
                app = c.execute(
                    "SELECT decision_code, owner_user_id, (expires_at > now()) AS alive"
                    " FROM agent.approval_records"
                    " WHERE tenant_id=%s AND tool_intent_id=%s",
                    (scope.tenant_id, intent_id),
                ).fetchone()
                if (
                    app is None
                    or str(app["decision_code"]) != "APPROVED"
                    or str(app["owner_user_id"]) != scope.owner_user_id
                    or not app["alive"]
                ):
                    raise ScopeDenied("explicit owner approval required for external GitHub PR creation")
            return _claim_from_row(row, scope.tenant_id)

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
    def _parse_json_response(raw_body: str) -> dict[str, Any]:
        if not raw_body.strip():
            return {}
        obj = json.loads(raw_body)
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, list):
            return {"items": obj}
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
        base_override: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        base = (base_override or self._github_api_base).rstrip("/")
        proxy_url = self._egress_proxy or os.environ.get("AIAGENT_EGRESS_PROXY") or ""
        if not base or not proxy_url or self._sentinel is None:
            raise ScopeDenied("github_api_base, AIAGENT_EGRESS_PROXY, and Sentinel are required for GitHub egress")

        pu = urlsplit(base)
        if pu.scheme not in ("http", "https") or not pu.hostname:
            raise ScopeDenied("invalid github_api_base URL")
        port = pu.port or (443 if pu.scheme == "https" else 80)
        host_hdr = pu.hostname if pu.port is None else f"{pu.hostname}:{port}"

        claim = self._verify_intent_and_claim(scope, intent_id, require_owner_approval=not reconcile)
        raw_sentinel = getattr(self._sentinel, "s", self._sentinel)
        permit = (
            raw_sentinel.authorize_reconcile(scope.tenant_id, intent_id, pu.scheme, pu.hostname, port)
            if reconcile
            else raw_sentinel.authorize(claim, intent_id, pu.scheme, pu.hostname, port, method)
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
                raise EffectUnknown(f"GitHub request outcome uncertain ({exc.code})") from exc
            raise ScopeDenied(f"Sentinel proxy denied before send ({exc.code})") from exc

        return st, self._parse_json_response(raw_body)

    def read_get(
        self, scope: ProjectScope, intent_id: str, path: str, *, base_override: str | None = None
    ) -> tuple[int, dict[str, Any]]:
        return self._forward(
            scope=scope,
            intent_id=intent_id,
            method="GET",
            path=path,
            body_bytes=None,
            reconcile=True,
            base_override=base_override,
        )

    def create_pull_request(
        self,
        *,
        scope: ProjectScope,
        head_sha: str,
        intent_id: str,
        base_branch: str = "main",
        title: str = "Autonomous development change",
        body: str = "Generated under bounded development workflow.",
        draft: bool = True,
        base_override: str | None = None,
    ) -> dict[str, Any]:
        require_sha(head_sha)
        payload = canonical_json(
            {
                "title": title[:200],
                "body": body[:2000],
                "head": scope.branch,
                "head_sha": head_sha,
                "base": base_branch,
                "draft": bool(draft),
            }
        ).encode("utf-8")
        st, data = self._forward(
            scope=scope,
            intent_id=intent_id,
            method="POST",
            path=f"/repos/{scope.repository}/pulls",
            body_bytes=payload,
            reconcile=False,
            base_override=base_override,
        )
        if st in (200, 201) and isinstance(data.get("html_url"), str):
            raw_num = data.get("number")
            return {
                "prNumber": int(raw_num) if isinstance(raw_num, int) and raw_num >= 1 else None,
                "prUrl": str(data["html_url"]),
                "headSha": head_sha,
                "draft": bool(data.get("draft", draft)),
            }
        if st == 422:
            raise StateConflict(f"GitHub rejected PR creation (HTTP 422): {str(data.get('message', ''))[:120]}")
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
        st, data = self.read_get(
            scope,
            intent_id,
            f"/repos/{scope.repository}/pulls?state=open&head={owner}:{scope.branch}",
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
                return {
                    "prNumber": int(raw_num) if isinstance(raw_num, int) and raw_num >= 1 else None,
                    "prUrl": str(item.get("html_url") or ""),
                    "headSha": sha_val,
                    "state": str(item.get("state") or "open"),
                    "mergeable": item.get("mergeable"),
                }
        return None
