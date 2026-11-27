"""Sentinel-mediated GitHub PR read transport & canonical request helpers (DEV-05 / DEV-11 / R2-02 / R2-03 / R2-04).

All mutating GitHub PR dispatches and reconciliations are owned exclusively by ToolGateway
(`aiagent_gateways.dev_github_executor.DevGitHubPrExecutor`), which owns the canonical
`tool_intents` / `tool_attempts` / `side_effect_records` CAS state machine.

This module provides:
- `canonical_pr_request` & `pr_request_sha256` (unified with `TOOLS["dev_github_pr"]` and ToolGateway hash)
- `resolve_binding_github_token` (strictly scoped to tenant + workspace + repository_binding_id + repo + apiOrigin)
- `GitHubReadClient` (read-only Sentinel `authorize_dev_pr_read` GET transport for PR Guardian)
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from aiagent_core import config
from aiagent_core.db import Database
from aiagent_core.egress_http import ForwardProxyError, send_via_forward_proxy
from aiagent_core.rpc import RpcClient
from aiagent_domain import is_uuid7, sha256_hex
from aiagent_domain.rpcwire import RpcError

from .contracts import EffectUnknown, ProjectScope, ScopeDenied, require_sha


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
    """Canonical argument object matching `TOOLS['dev_github_pr'].parameters` (R2-03)."""
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
    """Unified ToolGateway request digest: sha256_hex({'tool': 'dev_github_pr', 'args': req}) (R2-03)."""
    return sha256_hex({"tool": "dev_github_pr", "args": dict(req)})


def resolve_binding_github_token(
    *,
    tenant_id: str,
    workspace_id: str,
    repository_binding_id: str | None,
    repository: str,
    api_origin: str,
) -> str:
    """Resolve GitHub token strictly bound to (tenant, workspace, repository_binding_id, repository, api_origin) (R2-02).

    Never falls back to repository-name matching or a shared global AIAGENT_GITHUB_TOKEN.
    """
    if not repository_binding_id:
        raise ScopeDenied("REPOSITORY_BINDING_REQUIRED")
    raw = config.secret("AIAGENT_GITHUB_BINDING_TOKENS_JSON", required=False)
    if raw is None:
        raise ScopeDenied("GITHUB_CREDENTIAL_BINDING_MISSING")
    try:
        parsed = json.loads(raw.reveal())
    except ValueError as exc:
        raise ScopeDenied("GITHUB_CREDENTIAL_BINDING_INVALID") from exc
    if not isinstance(parsed, dict):
        raise ScopeDenied("GITHUB_CREDENTIAL_BINDING_INVALID")
    entry = parsed.get(repository_binding_id)
    if not isinstance(entry, dict):
        raise ScopeDenied("GITHUB_CREDENTIAL_BINDING_MISMATCH")
    tok = str(entry.get("token") or "").strip()
    if (
        not tok
        or str(entry.get("tenantId") or "") != tenant_id
        or str(entry.get("workspaceId") or "") != workspace_id
        or str(entry.get("repository") or "") != repository
        or str(entry.get("apiOrigin") or "").rstrip("/") != api_origin.rstrip("/")
    ):
        raise ScopeDenied("GITHUB_CREDENTIAL_BINDING_MISMATCH")
    return tok


class GitHubReadClient:
    """Read-only Sentinel-mediated GitHub GET client for PR Guardian (uses Sentinel RPC in production)."""

    def __init__(
        self,
        *,
        db: Database,
        github_api_base: str,
        repository_binding_id: str | None,
        sentinel: Any = None,
        egress_proxy: str | None = None,
        timeout_s: float = 15.0,
    ) -> None:
        self._db = db
        self._github_api_base = github_api_base.rstrip("/")
        self._repository_binding_id = repository_binding_id
        self._sentinel = sentinel
        self._egress_proxy = egress_proxy
        self._timeout_s = timeout_s

    def _issue_read_permit(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        intent_id: str,
        scheme: str,
        host: str,
        port: int,
        target_path: str,
    ) -> str:
        if self._sentinel is not None:
            raw_sentinel = getattr(self._sentinel, "s", self._sentinel)
            try:
                return str(
                    raw_sentinel.authorize_dev_pr_read(
                        tenant_id,
                        owner_user_id,
                        intent_id,
                        scheme,
                        host,
                        port,
                        target_path=target_path,
                    )
                )
            except RpcError as exc:
                raise ScopeDenied(f"sentinel:{exc.code}") from exc

        endpoint = (os.environ.get("AIAGENT_SENTINEL_ENDPOINT") or "").strip()
        rpc_key = config.secret("AIAGENT_API_SENTINEL_RPC_KEY", required=False)
        if not endpoint or rpc_key is None:
            raise ScopeDenied("SENTINEL_RPC_UNAVAILABLE")
        cli = RpcClient(endpoint, "api", bytes.fromhex(rpc_key.reveal().strip()))
        try:
            res = asyncio.run(
                cli.call(
                    "sentinel.authorize_dev_pr_read",
                    {
                        "tenantId": tenant_id,
                        "ownerUserId": owner_user_id,
                        "intentId": intent_id,
                        "scheme": scheme,
                        "host": host,
                        "port": port,
                        "targetPath": target_path,
                    },
                )
            )
        except RpcError as exc:
            raise ScopeDenied(f"sentinel:{exc.code}") from exc
        except Exception as exc:
            raise ScopeDenied("SENTINEL_UNAVAILABLE") from exc
        return str(res["permit"])

    def read_get(self, scope: ProjectScope, intent_id: str, path: str) -> tuple[int, dict[str, Any]]:
        if not is_uuid7(intent_id):
            raise ScopeDenied("canonical uuid7 tool_intent_id required")
        base = self._github_api_base
        proxy_url = self._egress_proxy or os.environ.get("AIAGENT_EGRESS_PROXY") or ""
        if not base or not proxy_url:
            raise ScopeDenied("github_api_base and AIAGENT_EGRESS_PROXY are required")
        pu = urlsplit(base)
        scheme = pu.scheme.lower()
        host = (pu.hostname or "").lower()
        if scheme not in ("http", "https") or not host or pu.username or pu.password:
            raise ScopeDenied("invalid github_api_base URL")
        port = pu.port or (443 if scheme == "https" else 80)
        origin = f"{scheme}://{host}" if pu.port is None else f"{scheme}://{host}:{port}"
        host_hdr = host if pu.port is None else f"{host}:{port}"

        # Verify active repository_binding in PostgreSQL before reading credential (R2-02)
        if not self._repository_binding_id:
            raise ScopeDenied("REPOSITORY_BINDING_REQUIRED")
        with self._db.tx(tenant=scope.tenant_id) as c:
            binding = c.execute(
                "SELECT * FROM agent.verify_dev_repository_binding(%s,%s,%s,%s,%s,%s)",
                (
                    scope.tenant_id,
                    scope.owner_user_id,
                    scope.workspace_id,
                    self._repository_binding_id,
                    scope.repository,
                    scope.branch,
                ),
            ).fetchone()
        if binding is None:
            raise ScopeDenied("REPOSITORY_BINDING_UNAUTHORIZED")

        token = resolve_binding_github_token(
            tenant_id=scope.tenant_id,
            workspace_id=scope.workspace_id,
            repository_binding_id=self._repository_binding_id,
            repository=scope.repository,
            api_origin=origin,
        )
        permit = self._issue_read_permit(
            tenant_id=scope.tenant_id,
            owner_user_id=scope.owner_user_id,
            intent_id=intent_id,
            scheme=scheme,
            host=host,
            port=port,
            target_path=path,
        )
        try:
            st, _, raw_body = asyncio.run(
                send_via_forward_proxy(
                    proxy_url,
                    identity="tool_gateway",
                    permit=permit,
                    method="GET",
                    target_url=f"{origin}{path}",
                    host_header=host_hdr,
                    headers={
                        "Accept": "application/vnd.github+json",
                        "User-Agent": "aiagent-dev-guardian/1.0",
                        "Authorization": f"Bearer {token}",
                    },
                    body=None,
                    timeout_s=self._timeout_s,
                )
            )
        except ForwardProxyError as exc:
            if exc.post_send:
                raise EffectUnknown(f"GitHub read failed ({exc.code})") from exc
            raise ScopeDenied(f"Sentinel proxy denied read ({exc.code})") from exc

        if not raw_body.strip():
            return st, {}
        try:
            obj = json.loads(raw_body)
        except ValueError as exc:
            raise EffectUnknown("malformed JSON response from GitHub") from exc
        if isinstance(obj, dict):
            return st, obj
        if isinstance(obj, list):
            return st, {"items": obj}
        return st, {}
