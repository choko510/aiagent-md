"""Public HTTP API (CHAT_08 subset for STEP 3). Authentication: opaque bearer token -> (tenant, user, workspace) via
`agent.api_credentials` (SHA-256 only). Every request runs with RLS scoped to the authenticated tenant, and owner
predicates in SQL; another owner's resources are indistinguishable from missing ones (404). Request bodies are
validated with the generated strict contracts. The API never performs agent work inside a request.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any

from aiagent_core import commands, config, memory_tombstones, memory_writer
from aiagent_core.commands import CommandError, Scope
from aiagent_core.db import Database
from aiagent_core.migrate import check_compatible
from aiagent_core.step4_gate import capability_snapshot
from aiagent_domain.canonical import sha256_bytes
from aiagent_domain.generated import (
    ApprovalDecisionCommandV1,
    ContextPolicyCommandV1,
    MemoryCreateCommandV1,
    MemoryUpdateCommandV1,
    TaskSteerCommandV1,
    TaskSubmitCommandV1,
)
from aiagent_domain.rpcwire import RpcError
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError


def create_app(db: Database | None = None) -> FastAPI:
    state: dict[str, Database] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        state["db"] = db or Database(config.secret("AIAGENT_API_DSN").reveal(), max_size=16, name="api")  # type: ignore[union-attr]
        yield
        if db is None:
            state["db"].close()

    app = FastAPI(title="aiagent-md API", version="0.3.0", lifespan=lifespan)

    def get_db() -> Database:
        return state["db"]

    async def scope(authorization: Annotated[str | None, Header()] = None) -> Scope:
        if not authorization or not authorization.startswith("Bearer ") or len(authorization) > 400:
            raise HTTPException(401, "unauthenticated")
        h = sha256_bytes(authorization[7:].encode())
        row = await asyncio.to_thread(get_db().one, "SELECT * FROM agent.resolve_api_token(%s)", (h,))
        if row is None:
            raise HTTPException(401, "unauthenticated")
        return Scope(row["tenant_id"], row["user_id"], row["workspace_id"])

    S = Annotated[Scope, Depends(scope)]

    @app.exception_handler(CommandError)
    async def _cmd(_: Request, e: CommandError) -> JSONResponse:
        return JSONResponse({"error": e.code}, status_code=e.http)

    @app.exception_handler(ValidationError)
    async def _val(_: Request, e: ValidationError) -> JSONResponse:
        return JSONResponse(
            {"error": "INVALID_REQUEST", "fields": [".".join(map(str, x["loc"])) for x in e.errors()]}, status_code=422
        )

    async def body_of(request: Request, model: Any) -> Any:
        raw = await request.body()
        if len(raw) > 64 * 1024:
            raise HTTPException(413, "too large")
        return model.model_validate_json(raw)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        def check() -> bool:
            with get_db().conn() as c:
                check_compatible(c, writer=True)
            return True

        try:
            await asyncio.to_thread(check)
        except Exception:
            return JSONResponse({"ready": False}, status_code=503)
        return JSONResponse({"ready": True})

    @app.get("/v1/step4/capabilities")
    async def step4_capabilities(_scope: S) -> dict[str, Any]:
        """Authenticated, read-only projection. An entry never grants a device or tool capability."""
        return {"schemaVersion": "1.0", "capabilities": capability_snapshot()}

    @app.post("/v1/tasks", status_code=201)
    async def submit(request: Request, s: S, idempotency_key: Annotated[str | None, Header()] = None) -> dict[str, str]:
        cmd: TaskSubmitCommandV1 = await body_of(request, TaskSubmitCommandV1)
        return await asyncio.to_thread(
            commands.submit_task, get_db(), s, cmd.goal, profile=cmd.profile, idempotency_key=idempotency_key
        )

    @app.get("/v1/tasks/{task_id}")
    async def get_task(task_id: str, s: S) -> dict[str, Any]:
        def q() -> dict[str, Any] | None:
            with get_db().tx(tenant=s.tenant_id) as c:
                t = c.execute(
                    "SELECT t.id, t.status_code, t.goal_revision, t.active_run_id, t.record_version, r.status_code AS run_state,"
                    " r.result_text, r.lease_epoch, r.head_revision, r.attempt_no, r.error_code FROM agent.tasks t"
                    " LEFT JOIN agent.runs r ON r.tenant_id=t.tenant_id AND r.id=t.active_run_id"
                    " WHERE t.tenant_id=%s AND t.id=%s AND t.owner_user_id=%s",
                    (s.tenant_id, task_id, s.user_id),
                ).fetchone()
                if t is None:
                    return None
                approvals = c.execute(
                    "SELECT id, tool_key, safe_summary, request_sha256, decision_code, record_version, goal_revision"
                    " FROM agent.approval_records WHERE tenant_id=%s AND task_id=%s ORDER BY requested_at",
                    (s.tenant_id, task_id),
                ).fetchall()
                effects = c.execute(
                    "SELECT tool_key, status_code FROM agent.tool_intents WHERE tenant_id=%s AND task_id=%s"
                    " ORDER BY created_at",
                    (s.tenant_id, task_id),
                ).fetchall()
            return {
                "schemaVersion": "1.0",
                "id": t["id"],
                "state": t["status_code"],
                "goalRevision": str(t["goal_revision"]),
                "recordVersion": str(t["record_version"]),
                "run": {
                    "id": t["active_run_id"],
                    "state": t["run_state"],
                    "attemptNo": t["attempt_no"],
                    "leaseEpoch": str(t["lease_epoch"]),
                    "headRevision": str(t["head_revision"]),
                    "result": t["result_text"],
                    "errorCode": t["error_code"],
                },
                "approvals": [
                    {
                        "id": a["id"],
                        "tool": a["tool_key"],
                        "summary": a["safe_summary"],
                        "requestSha256": a["request_sha256"],
                        "decision": a["decision_code"],
                        "recordVersion": str(a["record_version"]),
                        "goalRevision": str(a["goal_revision"]),
                    }
                    for a in approvals
                ],
                "effects": [{"tool": e["tool_key"], "status": e["status_code"]} for e in effects],
            }

        out = await asyncio.to_thread(q)
        if out is None:
            raise HTTPException(404, "not found")
        return out

    @app.get("/v1/tasks/{task_id}/stream")
    async def stream_text(task_id: str, s: S) -> dict[str, Any]:
        """Provisional (not committed) public text deltas, already filtered by the Stream Privacy Gate."""

        def q() -> list[str]:
            with get_db().tx(tenant=s.tenant_id) as c:
                rows = c.execute(
                    "SELECT ch.text_delta FROM agent.run_stream_chunks ch JOIN agent.tasks t ON t.tenant_id=ch.tenant_id"
                    " AND t.active_run_id=ch.run_id WHERE t.tenant_id=%s AND t.id=%s AND t.owner_user_id=%s"
                    " ORDER BY ch.created_at, ch.seq",
                    (s.tenant_id, task_id, s.user_id),
                ).fetchall()
            return [r["text_delta"] for r in rows]

        return {"provisional": True, "deltas": await asyncio.to_thread(q)}

    @app.post("/v1/tasks/{task_id}/steer")
    async def steer(task_id: str, request: Request, s: S) -> dict[str, str]:
        cmd: TaskSteerCommandV1 = await body_of(request, TaskSteerCommandV1)
        return await asyncio.to_thread(commands.steer, get_db(), s, task_id, cmd.goal, cmd.expected_goal_revision)

    @app.post("/v1/tasks/{task_id}/cancel")
    async def cancel(task_id: str, s: S) -> dict[str, str]:
        return await asyncio.to_thread(commands.cancel, get_db(), s, task_id)

    @app.post("/v1/tasks/{task_id}/pause")
    async def pause(task_id: str, s: S) -> dict[str, str]:
        return await asyncio.to_thread(commands.pause, get_db(), s, task_id)

    @app.post("/v1/tasks/{task_id}/resume")
    async def resume(task_id: str, s: S) -> dict[str, str]:
        return await asyncio.to_thread(commands.resume, get_db(), s, task_id)

    @app.post("/v1/approvals/{approval_id}/decision")
    async def decide(approval_id: str, request: Request, s: S) -> dict[str, str]:
        cmd: ApprovalDecisionCommandV1 = await body_of(request, ApprovalDecisionCommandV1)
        return await asyncio.to_thread(
            commands.decide_approval,
            get_db(),
            s,
            approval_id,
            cmd.decision,
            cmd.request_sha256,
            cmd.expected_record_version,
        )

    # ------------------------------------------------------------------ Memory (owner-authenticated)
    def memory_ingest_client() -> Any:
        """Internal authenticated RPC to the Model Gateway (the only component holding the key)."""
        endpoint = os.environ.get("AIAGENT_MODEL_GATEWAY_ENDPOINT")
        key = config.secret("AIAGENT_API_MODEL_RPC_KEY", required=False)
        if not endpoint or key is None:
            raise HTTPException(503, "MEMORY_INGEST_UNAVAILABLE")
        from aiagent_core.rpc import RpcClient

        return RpcClient(endpoint, "api", bytes.fromhex(key.reveal()))

    @app.post("/v1/tasks/{task_id}/memory/source", status_code=201)
    async def put_memory_source(task_id: str, request: Request, s: S) -> dict[str, Any]:
        """Store the user's original text as an encrypted artifact (plaintext never persists in the API)."""
        raw = await request.body()
        if not raw or len(raw) > 2_000_000:
            raise HTTPException(413, "source too large")
        try:
            text = raw.decode("utf-8", "strict")
        except UnicodeError:
            raise HTTPException(422, "source must be UTF-8 text") from None
        client = memory_ingest_client()
        body = {
            "tenantId": s.tenant_id,
            "ownerUserId": s.user_id,
            "workspaceId": s.workspace_id,
            "taskId": task_id,
            "sourceRef": f"user:{s.user_id}",
            "locatorRef": "bytes:0",
            "observedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "content": text,
        }
        try:
            # RpcClient.call is async; run it on this loop (it is network I/O, not blocking SQL).
            result = await client.call("memory.artifact.put", body)
        except RpcError as exc:
            raise HTTPException(409 if exc.code == "MEMORY_SCOPE_DENIED" else 422, exc.code) from None
        return {"schemaVersion": "1.0", "artifactId": result["artifactId"]}

    # Memory plaintext never passes through the API: creation accepts encrypted-artifact
    # references only, and every operation is owner-scoped (another owner gets 404).
    def tombstone_log() -> Any:
        try:
            return memory_tombstones.log_from_env()
        except (memory_tombstones.TombstoneError, config.ConfigError):
            if config.profile() == "test":
                return None
            raise HTTPException(503, "MEMORY_TOMBSTONE_UNAVAILABLE") from None

    @app.post("/v1/tasks/{task_id}/memory", status_code=201)
    async def create_memory(
        task_id: str,
        request: Request,
        s: S,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        cmd: MemoryCreateCommandV1 = await body_of(request, MemoryCreateCommandV1)
        log = tombstone_log()
        memory_id, epoch = await asyncio.to_thread(
            memory_writer.create_memory,
            get_db(),
            s,
            task_id=task_id,
            source_ref=cmd.source_artifact_ref,
            projection_ref=cmd.projection_artifact_ref,
            recipient=cmd.recipient,
            observed_at=datetime.fromisoformat(cmd.observed_at.replace("Z", "+00:00")),
            expires_at=datetime.fromisoformat(cmd.expires_at.replace("Z", "+00:00")),
            consent_basis=cmd.consent_basis,
            idempotency_key=idempotency_key or f"memory:{task_id}:{cmd.source_artifact_ref}",
        )
        if log is not None:
            await asyncio.to_thread(
                log.append,
                kind="created",
                tenant_id=s.tenant_id,
                owner_id=s.user_id,
                task_id=task_id,
                memory_id=memory_id,
                revision=1,
                recorded_at=datetime.now(UTC).isoformat(),
            )
        return {
            "schemaVersion": "1.0",
            "memoryId": memory_id,
            "revision": "1",
            "sourceEpoch": str(epoch),
        }

    @app.get("/v1/tasks/{task_id}/memory")
    async def list_memory(task_id: str, s: S) -> dict[str, Any]:
        rows = await asyncio.to_thread(memory_writer.list_memories, get_db(), s, task_id=task_id)
        return {"schemaVersion": "1.0", "items": rows}

    @app.get("/v1/tasks/{task_id}/memory/{memory_id}/revisions")
    async def memory_revisions(task_id: str, memory_id: str, s: S) -> dict[str, Any]:
        rows = await asyncio.to_thread(
            memory_writer.memory_revisions, get_db(), s, task_id=task_id, memory_id=memory_id
        )
        return {"schemaVersion": "1.0", "items": rows}

    @app.patch("/v1/tasks/{task_id}/memory/{memory_id}")
    async def update_memory(task_id: str, memory_id: str, request: Request, s: S) -> dict[str, Any]:
        cmd: MemoryUpdateCommandV1 = await body_of(request, MemoryUpdateCommandV1)
        log = tombstone_log()
        revision = await asyncio.to_thread(
            memory_writer.update_memory,
            get_db(),
            s,
            task_id=task_id,
            memory_id=memory_id,
            recipient=cmd.recipient,
            expires_at=(
                datetime.fromisoformat(cmd.expires_at.replace("Z", "+00:00")) if cmd.expires_at is not None else None
            ),
            expected_revision=int(cmd.expected_revision),
            tombstone_log=log,
        )
        return {"schemaVersion": "1.0", "memoryId": memory_id, "revision": str(revision)}

    @app.delete("/v1/tasks/{task_id}/memory/{memory_id}")
    async def revoke_memory(task_id: str, memory_id: str, s: S) -> dict[str, Any]:
        log = tombstone_log()
        epoch = await asyncio.to_thread(
            memory_writer.revoke_memory, get_db(), s, task_id=task_id, memory_id=memory_id, tombstone_log=log
        )
        return {"schemaVersion": "1.0", "memoryId": memory_id, "sourceEpoch": str(epoch)}

    @app.get("/v1/memory/consents")
    async def list_consents(s: S) -> dict[str, Any]:
        rows = await asyncio.to_thread(memory_writer.memory_consents, get_db(), s)
        return {"schemaVersion": "1.0", "items": rows}

    @app.post("/v1/memory/consents/{recipient}", status_code=201)
    async def grant_consent(recipient: str, s: S) -> dict[str, Any]:
        revision = await asyncio.to_thread(memory_writer.grant_memory_consent, get_db(), s, recipient=recipient)
        return {
            "schemaVersion": "1.0",
            "recipient": recipient,
            "granted": True,
            "recordVersion": str(revision),
        }

    @app.get("/v1/memory/consents/{recipient}/revisions")
    async def consent_revisions(recipient: str, s: S) -> dict[str, Any]:
        rows = await asyncio.to_thread(memory_writer.memory_consent_revisions, get_db(), s, recipient=recipient)
        return {"schemaVersion": "1.0", "items": rows}

    @app.delete("/v1/memory/consents/{recipient}")
    async def revoke_consent(recipient: str, s: S) -> dict[str, Any]:
        log = tombstone_log()
        revision = await asyncio.to_thread(
            memory_writer.revoke_memory_consent,
            get_db(),
            s,
            recipient=recipient,
            tombstone_log=log,
        )
        return {
            "schemaVersion": "1.0",
            "recipient": recipient,
            "granted": False,
            "recordVersion": str(revision),
        }

    @app.get("/v1/tasks/{task_id}/context-policy")
    async def get_context_policy(task_id: str, s: S) -> dict[str, Any]:
        policy = await asyncio.to_thread(memory_writer.context_policy, get_db(), s, task_id=task_id)
        return {"schemaVersion": "1.0", "policy": policy}

    @app.put("/v1/tasks/{task_id}/context-policy")
    async def set_context_policy(task_id: str, request: Request, s: S) -> dict[str, Any]:
        body = await body_of(request, ContextPolicyCommandV1)
        version = await asyncio.to_thread(
            memory_writer.set_context_policy,
            get_db(),
            s,
            task_id=task_id,
            recipient=body.recipient,
            model=body.model,
            enabled=body.enabled,
            expected_record_version=(
                int(body.expected_record_version) if body.expected_record_version is not None else None
            ),
        )
        return {"schemaVersion": "1.0", "taskId": task_id, "recordVersion": str(version)}

    return app


def main() -> None:
    import uvicorn
    from aiagent_core.logging import setup

    setup("api")
    config.require_isolation("api")
    uvicorn.run(
        create_app(),
        host=os.environ.get("AIAGENT_API_HOST", "127.0.0.1"),
        port=config.env_int("AIAGENT_API_PORT", 8080),
        log_config=None,
        timeout_graceful_shutdown=20,
    )


if __name__ == "__main__":
    main()
