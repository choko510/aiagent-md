"""Canonical Tool Gateway executor for `dev_github_pr` (R2-02..R2-09 / R3-01 / R3-07).

Runs strictly inside the Tool Gateway process (owner of tool_intents / tool_attempts / side_effect_records):
- R3-01: Enforces full Development Cycle admission inside ToolGateway (for BOTH runtime `tool.submit` and API
  `tool.dev_pr_dispatch`): `dev_projects`, `verify_dev_repository_binding`, `dev_cycles` (`phase == 'CODE_READY'`),
  immutable `commit_sha == headSha`, `task_id`, `run_id`, `goal_revision`, `repository`, and `branch`.
- R2-02: Binds GitHub credentials exclusively to an active operator-provisioned `agent.repository_bindings` row
  matching (tenant_id, owner_user_id, workspace_id, repository_binding_id, repository, api_origin).
- R3-07: Embeds a deterministic intent-scoped correlation marker in the canonical PR body (independently derived
  by Sentinel for `body_sha256` binding), snapshots pre-existing PR numbers BEFORE POST /pulls, refuses if a
  matching marker already exists, and reconciles across ALL pages matching exact (repository, branch, headSha,
  baseBranch, draft, title, body+marker) while excluding pre-existing PRs.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from aiagent_core import config
from aiagent_core.control import Claim
from aiagent_core.db import Database
from aiagent_core.egress_http import ForwardProxyError, send_via_forward_proxy
from aiagent_domain import canonical_json
from psycopg.types.json import Jsonb

from .executors import Destination, ExecutorFailed, ExecutorUnknown

_MAX_RECONCILE_PAGES = 10
_PER_PAGE = 100


def pr_intent_correlation_marker(intent_id: str, request_sha256: str) -> str:
    return f"<!-- aiagent-pr-intent:{intent_id}:{request_sha256[:16]} -->"


def render_pr_body_with_marker(raw_body: str, *, intent_id: str, request_sha256: str) -> str:
    marker = pr_intent_correlation_marker(intent_id, request_sha256)
    base = raw_body[:1900]
    return f"{base}\n\n{marker}" if base else marker


def normalize_api_origin(url: str) -> tuple[str, str, int, str]:
    pu = urlsplit(url.strip().rstrip("/"))
    scheme = pu.scheme.lower()
    host = (pu.hostname or "").lower()
    if scheme not in ("http", "https") or not host or pu.username or pu.password:
        raise ExecutorFailed("INVALID_GITHUB_API_BASE")
    port = pu.port or (443 if scheme == "https" else 80)
    origin = f"{scheme}://{host}" if pu.port is None else f"{scheme}://{host}:{port}"
    return scheme, host, port, origin


def resolve_binding_github_token(
    *,
    tenant_id: str,
    workspace_id: str,
    repository_binding_id: str | None,
    repository: str,
    api_origin: str,
) -> str:
    """Resolve GitHub token strictly bound to (tenant, workspace, repository_binding_id, repository, api_origin) (R2-02)."""
    if not repository_binding_id:
        raise ExecutorFailed("REPOSITORY_BINDING_REQUIRED")
    raw = config.secret("AIAGENT_GITHUB_BINDING_TOKENS_JSON", required=False)
    if raw is None:
        raise ExecutorFailed("GITHUB_CREDENTIAL_BINDING_MISSING")
    try:
        parsed = json.loads(raw.reveal())
    except ValueError as exc:
        raise ExecutorFailed("GITHUB_CREDENTIAL_BINDING_INVALID") from exc
    if not isinstance(parsed, dict):
        raise ExecutorFailed("GITHUB_CREDENTIAL_BINDING_INVALID")
    entry = parsed.get(repository_binding_id)
    if not isinstance(entry, dict):
        raise ExecutorFailed("GITHUB_CREDENTIAL_BINDING_MISMATCH")
    tok = str(entry.get("token") or "").strip()
    if (
        not tok
        or str(entry.get("tenantId") or "") != tenant_id
        or str(entry.get("workspaceId") or "") != workspace_id
        or str(entry.get("repository") or "") != repository
        or str(entry.get("apiOrigin") or "").rstrip("/") != api_origin.rstrip("/")
    ):
        raise ExecutorFailed("GITHUB_CREDENTIAL_BINDING_MISMATCH")
    return tok


def canonical_github_pr_payload(
    args: Mapping[str, Any],
    *,
    intent_id: str,
    request_sha256: str,
) -> bytes:
    return canonical_json(
        {
            "title": str(args["title"])[:200],
            "body": render_pr_body_with_marker(str(args["body"]), intent_id=intent_id, request_sha256=request_sha256),
            "head": str(args["branch"]),
            "base": str(args["baseBranch"]),
            "draft": bool(args["draft"]),
        }
    ).encode("utf-8")


class DevGitHubPrExecutor:
    """Canonical `dev_github_pr` executor owned by ToolGateway."""

    def __init__(self, db: Database, *, proxy: str | None = None, timeout_s: float = 15.0) -> None:
        self._db = db
        self._proxy = proxy
        self._timeout_s = timeout_s
        self.context: dict[str, Any] = {}

    def destination(self) -> Destination | None:
        return None

    def _load_bound_project(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        project_id: str,
        repository: str,
        branch: str,
        base_branch: str,
    ) -> dict[str, Any]:
        with self._db.tx(tenant=tenant_id) as c:
            proj = c.execute(
                "SELECT id, owner_user_id, workspace_id, repository, default_branch, repository_binding_id, github_api_base"
                " FROM agent.dev_projects"
                " WHERE tenant_id=%s AND id=%s AND owner_user_id=%s",
                (tenant_id, project_id, owner_user_id),
            ).fetchone()
            if (
                proj is None
                or str(proj["repository"]) != repository
                or str(proj["default_branch"]) != base_branch
                or not proj["github_api_base"]
                or not proj["repository_binding_id"]
            ):
                raise ExecutorFailed("PROJECT_OR_BINDING_MISMATCH")
            binding = c.execute(
                "SELECT * FROM agent.verify_dev_repository_binding(%s,%s,%s,%s,%s,%s)",
                (
                    tenant_id,
                    owner_user_id,
                    str(proj["workspace_id"]),
                    str(proj["repository_binding_id"]),
                    repository,
                    branch,
                ),
            ).fetchone()
            if binding is None:
                raise ExecutorFailed("REPOSITORY_BINDING_UNAUTHORIZED")
            return {
                "workspace_id": str(proj["workspace_id"]),
                "repository_binding_id": str(proj["repository_binding_id"]),
                "github_api_base": str(proj["github_api_base"]).rstrip("/"),
            }

    def verify_cycle_admission(self, claim: Claim, args: Mapping[str, Any]) -> dict[str, Any]:
        """R3-01: Mandatory ToolGateway admission check binding Project, RepositoryBinding, Cycle CODE_READY, Commit & RunFence."""
        try:
            project_id = str(args["projectId"])
            cycle_id = str(args["cycleId"])
            repository = str(args["repository"])
            branch = str(args["branch"])
            head_sha = str(args["headSha"])
            base_branch = str(args["baseBranch"])
        except KeyError as exc:
            raise ExecutorFailed("INVALID_DEV_PR_ARGUMENTS") from exc

        proj = self._load_bound_project(
            tenant_id=claim.tenant_id,
            owner_user_id=claim.owner_user_id,
            project_id=project_id,
            repository=repository,
            branch=branch,
            base_branch=base_branch,
        )
        with self._db.tx(tenant=claim.tenant_id) as c:
            cy = c.execute(
                "SELECT c.id, c.project_id, c.workspace_id, c.owner_user_id, c.task_id, c.run_id,"
                " c.phase, c.cycle_json, t.goal_revision AS live_goal_revision"
                " FROM agent.dev_cycles c"
                " JOIN agent.tasks t ON t.tenant_id=c.tenant_id AND t.id=c.task_id"
                " WHERE c.tenant_id=%s AND c.id=%s",
                (claim.tenant_id, cycle_id),
            ).fetchone()
        if cy is None:
            raise ExecutorFailed("CYCLE_NOT_FOUND")
        if (
            str(cy["owner_user_id"]) != claim.owner_user_id
            or str(cy["project_id"]) != project_id
            or str(cy["workspace_id"]) != proj["workspace_id"]
            or str(cy["task_id"]) != claim.task_id
            or str(cy["run_id"]) != claim.run_id
        ):
            raise ExecutorFailed("CYCLE_SCOPE_OR_RUN_MISMATCH")
        if str(cy["phase"]) != "CODE_READY":
            raise ExecutorFailed("CYCLE_NOT_CODE_READY")
        cj = dict(cy["cycle_json"]) if isinstance(cy.get("cycle_json"), dict) else {}
        raw_commit = cj.get("commit")
        commit_sha = str(raw_commit.get("commit_sha") or "") if isinstance(raw_commit, dict) else ""
        raw_scope = cj.get("scope")
        scope_repo = str(raw_scope.get("repository") or "") if isinstance(raw_scope, dict) else ""
        scope_branch = str(raw_scope.get("branch") or "") if isinstance(raw_scope, dict) else ""
        raw_run = cj.get("run")
        cycle_goal_rev = int(raw_run.get("goal_revision", 0)) if isinstance(raw_run, dict) else 0
        if (
            commit_sha != head_sha
            or scope_repo != repository
            or scope_branch != branch
            or cycle_goal_rev != claim.goal_revision
            or int(cy["live_goal_revision"]) != claim.goal_revision
        ):
            raise ExecutorFailed("CYCLE_COMMIT_OR_GOAL_MISMATCH")
        return proj

    def destination_for_tool(
        self,
        tool: str,
        args: dict[str, Any],
        *,
        bound_form: dict[str, Any] | None = None,
        claim: Claim | None = None,
        intent: Mapping[str, Any] | None = None,
    ) -> Destination:
        del bound_form
        if tool != "dev_github_pr" or claim is None or intent is None:
            raise ExecutorFailed("INVALID_DEV_PR_TOOL_CALL")
        proj = self.verify_cycle_admission(claim, args)
        scheme, host, port, _ = normalize_api_origin(proj["github_api_base"])
        payload = canonical_github_pr_payload(
            args,
            intent_id=str(intent["id"]),
            request_sha256=str(intent["request_sha256"]),
        )
        return Destination(
            scheme=scheme,
            host=host,
            port=port,
            method="POST",
            target_path=f"/repos/{args['repository']}/pulls",
            body_sha256=hashlib.sha256(payload).hexdigest(),
        )

    def destination_for_reconcile(self, tenant_id: str, intent_row: Mapping[str, Any]) -> Destination:
        args = intent_row.get("safe_arguments_json")
        if not isinstance(args, dict):
            raise ExecutorFailed("INVALID_INTENT_ARGUMENTS")
        with self._db.tx(tenant=tenant_id) as c:
            task = c.execute(
                "SELECT owner_user_id FROM agent.tasks WHERE tenant_id=%s AND id=%s",
                (tenant_id, intent_row["task_id"]),
            ).fetchone()
        if task is None:
            raise ExecutorFailed("TASK_NOT_FOUND")
        proj = self._load_bound_project(
            tenant_id=tenant_id,
            owner_user_id=str(task["owner_user_id"]),
            project_id=str(args["projectId"]),
            repository=str(args["repository"]),
            branch=str(args["branch"]),
            base_branch=str(args["baseBranch"]),
        )
        scheme, host, port, _ = normalize_api_origin(proj["github_api_base"])
        owner_org = str(args["repository"]).split("/", 1)[0]
        return Destination(
            scheme=scheme,
            host=host,
            port=port,
            method="GET",
            target_path=(
                f"/repos/{args['repository']}/pulls?state=all&head={owner_org}:{args['branch']}"
                f"&per_page={_PER_PAGE}&page=1"
            ),
        )

    async def _list_all_branch_prs(
        self,
        *,
        proxy_url: str,
        scheme: str,
        host: str,
        port: int,
        origin: str,
        host_hdr: str,
        base_headers: dict[str, str],
        repository: str,
        branch: str,
        first_permit: str | None,
        permit_issuer: Any,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Paginate ALL PRs for head branch through Sentinel GET permits (R3-07)."""
        owner_org = repository.split("/", 1)[0]
        all_items: list[dict[str, Any]] = []
        for page in range(1, _MAX_RECONCILE_PAGES + 1):
            path = f"/repos/{repository}/pulls?state=all&head={owner_org}:{branch}&per_page={_PER_PAGE}&page={page}"
            if page == 1 and first_permit:
                page_permit = first_permit
            elif callable(permit_issuer):
                page_permit = await permit_issuer(Destination(scheme, host, port, "GET", target_path=path))
            else:
                return [], False
            try:
                st, _, raw = await send_via_forward_proxy(
                    proxy_url,
                    identity="tool_gateway",
                    permit=page_permit,
                    method="GET",
                    target_url=f"{origin}{path}",
                    host_header=host_hdr,
                    headers=base_headers,
                    body=None,
                    timeout_s=self._timeout_s,
                )
            except ForwardProxyError:
                return [], False
            if st != 200:
                return [], False
            try:
                items = json.loads(raw)
            except ValueError:
                return [], False
            if not isinstance(items, list):
                return [], False
            for item in items:
                if isinstance(item, dict):
                    all_items.append(item)
            if len(items) < _PER_PAGE:
                return all_items, True
        return [], False  # exceeded page ceiling without exhausting list

    async def execute(self, effect_key: str, args: dict[str, Any], permit: str | None) -> dict[str, Any]:
        proxy_url = self._proxy or os.environ.get("AIAGENT_EGRESS_PROXY") or ""
        ctx = self.context.get(effect_key)
        if not proxy_url or not permit or ctx is None:
            raise ExecutorFailed("EGRESS_OR_ADMISSION_CONTEXT_MISSING")

        intent_id = str(ctx["intent_id"])
        request_sha256 = str(ctx["request_sha256"])
        claim = Claim(
            tenant_id=str(ctx["tenant_id"]),
            owner_user_id=str(ctx["owner_user_id"]),
            task_id=str(ctx["task_id"]),
            run_id=str(ctx["run_id"]),
            lease_epoch=int(ctx["lease_epoch"]),
            goal_revision=int(ctx["goal_revision"]),
            worker_ref=str(ctx["worker_ref"]),
            restore_incarnation=int(ctx.get("restore_incarnation", 1)),
        )
        proj = self.verify_cycle_admission(claim, args)
        scheme, host, port, origin = normalize_api_origin(proj["github_api_base"])
        host_hdr = (
            host if (port == 80 and scheme == "http") or (port == 443 and scheme == "https") else f"{host}:{port}"
        )
        token = resolve_binding_github_token(
            tenant_id=claim.tenant_id,
            workspace_id=proj["workspace_id"],
            repository_binding_id=proj["repository_binding_id"],
            repository=str(args["repository"]),
            api_origin=origin,
        )
        base_headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "aiagent-tool-gateway-github/1.0",
            "Authorization": f"Bearer {token}",
        }

        reconcile_issuer = ctx.get("reconcile_permit_issuer")
        if not callable(reconcile_issuer):
            raise ExecutorFailed("REF_VERIFY_PERMIT_ISSUER_UNAVAILABLE")

        # P1-04: verify remote branch ref SHA via Sentinel-authorized GET BEFORE POST /pulls
        ref_path = f"/repos/{args['repository']}/git/ref/heads/{args['branch']}"
        ref_permit = await reconcile_issuer(Destination(scheme, host, port, "GET", target_path=ref_path))
        try:
            st_ref, _, raw_ref = await send_via_forward_proxy(
                proxy_url,
                identity="tool_gateway",
                permit=ref_permit,
                method="GET",
                target_url=f"{origin}{ref_path}",
                host_header=host_hdr,
                headers=base_headers,
                body=None,
                timeout_s=self._timeout_s,
            )
        except ForwardProxyError as exc:
            raise ExecutorFailed(f"REMOTE_REF_CHECK_FAILED:{exc.code}") from exc
        if st_ref != 200:
            raise ExecutorFailed(f"REMOTE_REF_UNAVAILABLE:{st_ref}")
        try:
            ref_obj = json.loads(raw_ref)
        except ValueError as exc:
            raise ExecutorFailed("REMOTE_REF_MALFORMED") from exc
        remote_sha = str((ref_obj.get("object") or {}).get("sha") or "") if isinstance(ref_obj, dict) else ""
        if remote_sha != str(args["headSha"]):
            raise ExecutorFailed("STALE_REMOTE_HEAD")

        # R3-07: Snapshot pre-existing PRs BEFORE POST /pulls so reconcile can NEVER confuse a pre-existing PR
        # with the outcome of this intent, and refuse if the same intent correlation marker already exists.
        marker = pr_intent_correlation_marker(intent_id, request_sha256)
        pre_items, pre_ok = await self._list_all_branch_prs(
            proxy_url=proxy_url,
            scheme=scheme,
            host=host,
            port=port,
            origin=origin,
            host_hdr=host_hdr,
            base_headers=base_headers,
            repository=str(args["repository"]),
            branch=str(args["branch"]),
            first_permit=None,
            permit_issuer=reconcile_issuer,
        )
        if not pre_ok:
            raise ExecutorFailed("PRE_SEND_PR_SNAPSHOT_UNAVAILABLE")
        pre_existing_nums: list[int] = []
        for item in pre_items:
            if marker in str(item.get("body") or ""):
                raise ExecutorFailed("DUPLICATE_PR_INTENT_MARKER_EXISTS")
            num = item.get("number")
            if isinstance(num, int) and num >= 1:
                pre_existing_nums.append(num)
        with self._db.tx() as c:
            c.execute(
                "UPDATE agent.tool_intents"
                " SET result_json = coalesce(result_json, '{}'::jsonb) || %s::jsonb"
                " WHERE tenant_id=%s AND id=%s AND status_code='SENDING'",
                (Jsonb({"preExistingPrNumbers": sorted(set(pre_existing_nums))}), claim.tenant_id, intent_id),
            )

        # Send canonical GitHub PR creation POST (with intent correlation marker in body) through Sentinel EgressProxy
        post_path = f"/repos/{args['repository']}/pulls"
        payload = canonical_github_pr_payload(args, intent_id=intent_id, request_sha256=request_sha256)
        try:
            st, _, raw_body = await send_via_forward_proxy(
                proxy_url,
                identity="tool_gateway",
                permit=permit,
                method="POST",
                target_url=f"{origin}{post_path}",
                host_header=host_hdr,
                headers={**base_headers, "Content-Type": "application/json"},
                body=payload,
                timeout_s=self._timeout_s,
            )
        except ForwardProxyError as exc:
            if exc.post_send:
                raise ExecutorUnknown(f"GITHUB_POST_UNCERTAIN:{exc.code}") from exc
            raise ExecutorFailed(f"SENTINEL_DENIED:{exc.code}") from exc

        try:
            data = json.loads(raw_body)
        except ValueError as exc:
            raise ExecutorUnknown("GITHUB_POST_MALFORMED_JSON") from exc
        if st not in (200, 201) or not isinstance(data, dict):
            raise ExecutorUnknown(f"GITHUB_POST_HTTP_{st}")

        pr_num = data.get("number")
        pr_url = str(data.get("html_url") or "")
        resp_head = data.get("head")
        resp_sha = str(resp_head.get("sha") or "") if isinstance(resp_head, dict) else ""
        if not isinstance(pr_num, int) or pr_num < 1 or not pr_url or resp_sha != str(args["headSha"]):
            raise ExecutorUnknown("GITHUB_POST_HEAD_OR_RECEIPT_MISMATCH")
        if pr_num in pre_existing_nums:
            raise ExecutorUnknown("GITHUB_POST_RETURNED_PREEXISTING_PR")

        receipt = f"github-pr:{args['repository']}#{pr_num}:{resp_sha[:12]}:{intent_id[:8]}"
        return {
            "receipt": receipt[:120],
            "prNumber": pr_num,
            "prUrl": pr_url,
            "headSha": resp_sha,
            "baseBranch": str(args["baseBranch"]),
            "draft": bool(args["draft"]),
        }

    async def query(self, effect_key: str, permit: str | None = None) -> dict[str, Any] | None:
        """Strict paginated reconciliation requiring exact body+intent marker match and excluding pre-existing PRs (R3-07)."""
        proxy_url = self._proxy or os.environ.get("AIAGENT_EGRESS_PROXY") or ""
        if not proxy_url or not permit or ":" not in effect_key:
            return None
        tenant_id, intent_id = effect_key.split(":", 1)
        with self._db.tx() as c:
            row = c.execute(
                "SELECT i.tool_key, i.status_code, i.request_sha256, i.safe_arguments_json, i.result_json,"
                " t.owner_user_id, se.outcome_code AS side_effect_outcome"
                " FROM agent.tool_intents i"
                " JOIN agent.tasks t ON t.tenant_id=i.tenant_id AND t.id=i.task_id"
                " JOIN agent.side_effect_records se ON se.tenant_id=i.tenant_id AND se.tool_intent_id=i.id"
                " WHERE i.tenant_id=%s AND i.id=%s",
                (tenant_id, intent_id),
            ).fetchone()
        if (
            row is None
            or str(row["tool_key"]) != "dev_github_pr"
            or str(row["status_code"]) not in ("SENDING", "UNKNOWN")
            or str(row["side_effect_outcome"]) not in ("SENDING", "UNKNOWN")
        ):
            return None
        args = row["safe_arguments_json"]
        if not isinstance(args, dict):
            return None
        request_sha256 = str(row["request_sha256"])
        res_json = dict(row["result_json"] or {}) if isinstance(row.get("result_json"), dict) else {}
        raw_pre = res_json.get("preExistingPrNumbers")
        if not isinstance(raw_pre, list) or any(type(x) is not int for x in raw_pre):
            # Without a verified pre-send snapshot we refuse to attribute any PR to this intent.
            return None
        pre_existing = set(raw_pre)

        try:
            proj = self._load_bound_project(
                tenant_id=tenant_id,
                owner_user_id=str(row["owner_user_id"]),
                project_id=str(args["projectId"]),
                repository=str(args["repository"]),
                branch=str(args["branch"]),
                base_branch=str(args["baseBranch"]),
            )
            scheme, host, port, origin = normalize_api_origin(proj["github_api_base"])
            token = resolve_binding_github_token(
                tenant_id=tenant_id,
                workspace_id=proj["workspace_id"],
                repository_binding_id=proj["repository_binding_id"],
                repository=str(args["repository"]),
                api_origin=origin,
            )
        except ExecutorFailed:
            return None

        host_hdr = (
            host if (port == 80 and scheme == "http") or (port == 443 and scheme == "https") else f"{host}:{port}"
        )
        base_headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "aiagent-tool-gateway-github/1.0",
            "Authorization": f"Bearer {token}",
        }
        ctx = self.context.get(effect_key) or {}
        items, complete = await self._list_all_branch_prs(
            proxy_url=proxy_url,
            scheme=scheme,
            host=host,
            port=port,
            origin=origin,
            host_hdr=host_hdr,
            base_headers=base_headers,
            repository=str(args["repository"]),
            branch=str(args["branch"]),
            first_permit=permit,
            permit_issuer=ctx.get("reconcile_permit_issuer"),
        )
        if not complete:
            return None

        expected_sha = str(args["headSha"])
        expected_branch = str(args["branch"])
        expected_base = str(args["baseBranch"])
        expected_draft = bool(args["draft"])
        expected_title = str(args["title"])[:200]
        expected_body = render_pr_body_with_marker(
            str(args["body"]), intent_id=intent_id, request_sha256=request_sha256
        )
        matches: list[dict[str, Any]] = []
        for item in items:
            hd = item.get("head")
            bs = item.get("base")
            num = item.get("number")
            if not isinstance(hd, dict) or not isinstance(bs, dict) or type(num) is not int or num < 1:
                continue
            if num in pre_existing:
                continue
            if (
                str(hd.get("sha") or "") == expected_sha
                and str(hd.get("ref") or "") == expected_branch
                and str(bs.get("ref") or "") == expected_base
                and bool(item.get("draft")) is expected_draft
                and str(item.get("title") or "")[:200] == expected_title
                and str(item.get("body") or "") == expected_body
                and isinstance(item.get("html_url"), str)
                and bool(item["html_url"])
            ):
                matches.append(item)
        if len(matches) != 1:
            return None
        found = matches[0]
        pr_num = int(found["number"])
        pr_url = str(found["html_url"])
        receipt = f"github-pr:{args['repository']}#{pr_num}:{expected_sha[:12]}:{intent_id[:8]}"
        return {
            "sent": True,
            "receipt": receipt[:120],
            "prResult": {
                "prNumber": pr_num,
                "prUrl": pr_url,
                "headSha": expected_sha,
                "baseBranch": expected_base,
                "draft": expected_draft,
            },
        }
