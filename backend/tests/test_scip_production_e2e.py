"""Opt-in real PostgreSQL/Tool Gateway/Supervisor/SCIP/B1 integration.

The container shares the host kernel and uses disposable test keys. This test
does not establish production VM isolation, image attestation or provisioning.
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from threading import Event
from typing import Any

import psycopg
import pytest
from aiagent_core import commands, control
from aiagent_core.commands import Scope
from aiagent_core.db import Database
from aiagent_development.code_intelligence.git_proof import build_git_tree_proof
from aiagent_development.code_intelligence.git_reader import GitSnapshot
from aiagent_development.code_intelligence.indexer_recipe import approved_recipe_digest, python_scip_recipe_digest
from aiagent_development.code_intelligence.models import AuthorizedSnapshot, Identity
from aiagent_development.code_intelligence.models import Limits as GitLimits
from aiagent_development.code_intelligence.projection import prepare_projection
from aiagent_development.code_intelligence.projection_stream import ProjectionEncoder
from aiagent_development.code_intelligence.scip_navigation import (
    IndexManifest,
    IndexStatus,
    SourceProvenance,
    load_index,
)
from aiagent_development.contracts import Cycle, Phase, ProjectScope, RunFence, TaskPlan
from aiagent_development.pg_store import PostgresCycleStore
from aiagent_development.workspace_authority import register_verified_snapshot
from aiagent_domain import sha256_hex, uuid7
from aiagent_domain.rpcwire import RpcError
from aiagent_gateways.context_keys import KeyRing
from aiagent_gateways.execution_artifacts import PostgresExecutionArtifactStore
from aiagent_gateways.execution_capture_proof import HmacCaptureVerifier
from aiagent_gateways.execution_lifecycle import ExecutionLifecycle
from aiagent_gateways.scip_dispatch import AuthorizedScipExecutor
from aiagent_gateways.scip_validation import SnapshotScipVerifier
from aiagent_gateways.tool_gateway import ToolGateway
from psycopg.types.json import Jsonb
from psycopg_pool import PoolTimeout
from stack import wait_port
from test_cell_e2e import _supervisor_answers

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(
        sys.platform != "linux" or os.environ.get("AIAGENT_SCIP_E2E") != "1", reason="opt-in Docker SCIP E2E"
    ),
]


def _crash_gateway_child(config: dict[str, Any]) -> None:
    """Spawned Gateway with its own DB pool and test-only fault injection."""
    claim = control.Claim(**config["claim"])
    db = Database(config["dsn"])
    verifier = HmacCaptureVerifier(db, b"c" * 32)
    lifecycle = ExecutionLifecycle(db, claim, verifier)
    if config["scenario"] == "crash_after_issue":
        original_issue = lifecycle.issue_source_session

        def die_after_issue(*args, **kwargs):
            original_issue(*args, **kwargs)
            os._exit(139)

        lifecycle.issue_source_session = die_after_issue
    manifest = config["manifest"]

    def snapshot_factory() -> GitSnapshot:
        return GitSnapshot(
            AuthorizedSnapshot(manifest.identity, config["repo"]), config["git"], GitLimits(total_seconds=120)
        )

    def store_factory(prepared):
        store = PostgresExecutionArtifactStore(
            db,
            claim,
            KeyRing.single(b"k" * 32, "fixture"),
            capture=verifier,
            index=SnapshotScipVerifier(claim, prepared, manifest, snapshot_factory),
        )
        if config["scenario"] == "crash_before_publish":

            def die_before_publish(*_args, **_kwargs):
                os._exit(138)

            store.publish = die_before_publish
        return store

    executor = AuthorizedScipExecutor(
        db,
        claim,
        config["snapshot_id"],
        config["recipe_id"],
        lifecycle,
        store_factory,
        manifest,
        config["receipt"],
        config["proof"],
        config["chunks"],
        config["endpoint"],
        b"r" * 32,
        b"t" * 32,
        b"g" * 32,
        config["private"],
    )
    if config["scenario"] == "crash_after_publish":
        os.environ["AIAGENT_FAULT"] = "gateway_after_send"
    gateway = ToolGateway(db, {"sandbox_cell": executor})
    asyncio.run(gateway.submit(claim, "scip-e2e-call", "sandbox_exec", config["args"]))
    os._exit(1)


@pytest.mark.parametrize(
    "scenario",
    [
        "success",
        "cancel_before_registration",
        "cancel_running",
        "cancel_capturing",
        "crash_running",
        "expire_running",
        "cancel_before_publish",
        "crash_after_publish",
        "crash_before_publish",
        "crash_after_issue",
        "disconnect_registration",
        "disconnect_artifact",
        "db_unavailable_running",
        "reconcile_running",
    ],
)
def test_canonical_gateway_to_real_scip_publication_and_b1(
    dbs, tenant, tmp_path: Path, scenario: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = shutil.which("git")
    assert git and shutil.which("docker")
    backend = Path(__file__).resolve().parents[1]
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", "codex-dev13-scip-cell-fixture:latest", "--format", "{{.Id}}"], text=True
    ).strip()
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
    name = "aiagent-scip-e2e-" + secrets.token_hex(5)
    started = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-p",
            "127.0.0.1::7000",
            "--cap-add",
            "SYS_ADMIN",
            "--security-opt",
            "seccomp=unconfined",
            "--security-opt",
            "apparmor=unconfined",
            "--read-only",
            "--tmpfs",
            "/tmp:exec,mode=1777,size=256m",
            "--pids-limit",
            "256",
            "--memory",
            "3g",
            "-v",
            f"{backend}:/w:ro",
            "-w",
            "/w",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-e",
            f"AIAGENT_SCIP_PAUSE_CAPTURE={int(scenario == 'cancel_capturing')}",
            "-e",
            f"AIAGENT_SCIP_PAUSE_ARTIFACT={int(scenario == 'disconnect_artifact')}",
            "-e",
            "PYTHONPATH=/w/packages/domain/src:/w/packages/core/src:/w/packages/gateways/src:/w/packages/sandbox/src:/w/packages/development/src",
            image_id,
            "python",
            "/w/packages/development/scripts/scip_cell_fixture/serve_source.py",
        ],
        capture_output=True,
        text=True,
    )
    if started.returncode != 0:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        pytest.fail(f"fixture container could not start: {started.stderr}")
    try:
        mapping = subprocess.check_output(["docker", "port", name, "7000/tcp"], text=True).strip()
        port = int(mapping.rsplit(":", 1)[1])
        wait_port(port, 60)
        assert _supervisor_answers(port, 30), subprocess.run(
            ["docker", "logs", name], capture_output=True, text=True
        ).stderr
        toolchain = subprocess.check_output(
            [
                "docker",
                "exec",
                name,
                "python",
                "-c",
                "from aiagent_development.code_intelligence.indexer_recipe import measure_python_scip_toolchain; "
                "print(measure_python_scip_toolchain())",
            ],
            text=True,
        ).strip()
        assert re.fullmatch(r"[0-9a-f]{64}", toolchain)
        repo = tmp_path / "repo"
        repo.mkdir()

        def git_run(*args: str) -> bytes:
            return subprocess.check_output([git, "-C", str(repo), *args], stderr=subprocess.DEVNULL)

        git_run("init")
        git_run("config", "user.name", "Fixture")
        git_run("config", "user.email", "fixture@example.invalid")
        (repo / "library.py").write_bytes(b"def greet(name: str) -> str:\n    return 'hello ' + name\n")
        (repo / "main.py").write_bytes(b"from library import greet\nresult = greet('world')\n")
        if scenario in (
            "cancel_running",
            "crash_running",
            "expire_running",
            "reconcile_running",
            "db_unavailable_running",
        ):
            generated = repo / "generated"
            generated.mkdir()
            for i in range(200):
                (generated / f"module_{i}.py").write_text(f"from library import greet\nvalue = greet('{i}')\n")
        git_run("add", ".")
        git_run("commit", "-m", "fixture")
        commit = git_run("rev-parse", "HEAD").decode().strip()
        scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
        ids = commands.submit_task(dbs["api"], scope, "fixture", profile="sandbox")
        claim = control.claim_run(dbs["runtime"], scope.tenant_id, ids["runId"], "scip-worker", lease_s=120)
        binding_id = uuid7()
        dbs["owner"].one(
            "INSERT INTO agent.repository_bindings(tenant_id,id,owner_user_id,workspace_id,repository_ref,approved_commit,"
            "workspace_revision,approval_ref) VALUES (%s,%s,%s,%s,%s,%s,1,%s) RETURNING id",
            (claim.tenant_id, binding_id, claim.owner_user_id, scope.workspace_id, "fixture/repo", commit, "fixture"),
        )
        identity = Identity(claim.tenant_id, claim.owner_user_id, binding_id, "1", commit)

        def snapshot_factory() -> GitSnapshot:
            return GitSnapshot(AuthorizedSnapshot(identity, repo), Path(git), GitLimits(total_seconds=120))

        source = snapshot_factory()
        manifest = prepare_projection(source, identity, source.tree)
        proof = build_git_tree_proof(source)
        encoder = ProjectionEncoder(source, manifest)
        chunks = list(encoder.chunks())
        assert encoder.receipt is not None
        private = tmp_path / "private"
        private.mkdir(mode=0o700)
        snapshot = register_verified_snapshot(
            dbs["tool_gateway"],
            claim,
            binding_id,
            manifest,
            proof,
            encoder.receipt,
            io.BytesIO(b"".join(chunks)),
            private,
            deadline=time.monotonic() + 20,
        )
        project = ProjectScope(
            claim.tenant_id,
            claim.owner_user_id,
            "project",
            snapshot.workspace_id,
            "fixture/repo",
            "feature",
            snapshot.commit,
            str(snapshot.revision),
        )
        fence = RunFence(
            claim.task_id,
            claim.run_id,
            claim.goal_revision,
            claim.lease_epoch,
            claim.restore_incarnation,
            claim.worker_ref,
        )
        plan = TaskPlan("plan", "index", ("unit",), claim.goal_revision)
        cycle = Cycle("scip-e2e-cycle", project, fence, Phase.PLANNED, "index", plan=plan)
        PostgresCycleStore(dbs["tool_gateway"]).create(cycle)
        args = {"argv": ["index"], "cwdRelative": "."}
        request_sha, plan_sha = sha256_hex({"tool": "sandbox_exec", "args": args}), sha256_hex(asdict(plan))
        recipe_id, template = uuid7(), python_scip_recipe_digest()
        image_digest = image_id
        recipe_sha = approved_recipe_digest(
            kind="index",
            test_id=None,
            plan_sha256=plan_sha,
            request_sha256=request_sha,
            template_sha256=template,
            toolchain_sha256=toolchain,
            image_digest=image_digest,
        )
        dbs["owner"].one(
            "INSERT INTO agent.execution_recipe_approvals(tenant_id,id,cycle_id,goal_revision,plan_sha256,plan_json,kind,"
            "test_id,request_sha256,template_sha256,recipe_sha256,toolchain_sha256,image_digest,approval_ref)"
            " VALUES (%s,%s,%s,%s,%s,%s,'index',NULL,%s,%s,%s,%s,%s,%s) RETURNING id",
            (
                claim.tenant_id,
                recipe_id,
                cycle.cycle_id,
                claim.goal_revision,
                plan_sha,
                Jsonb(asdict(plan)),
                request_sha,
                template,
                recipe_sha,
                toolchain,
                image_digest,
                "fixture",
            ),
        )
        with dbs["model_gateway"].tx() as c:
            control.append_head(
                c,
                claim,
                "assistant",
                {
                    "kind": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "scip-e2e-call", "name": "sandbox_exec", "args": args}],
                },
                writer="test",
                request_key=os.urandom(32).hex(),
            )
        verifier = HmacCaptureVerifier(dbs["tool_gateway"], b"c" * 32)
        lifecycle = ExecutionLifecycle(dbs["tool_gateway"], claim, verifier)
        stores = []
        artifact_keys = [KeyRing.single(b"k" * 32, "fixture")]
        publication_entered, publication_release = Event(), Event()

        def store_factory(prepared):
            store = PostgresExecutionArtifactStore(
                dbs["tool_gateway"],
                claim,
                artifact_keys[0],
                capture=verifier,
                index=SnapshotScipVerifier(claim, prepared, manifest, snapshot_factory),
            )
            if scenario == "cancel_before_publish":
                original_publish = store.publish

                def paused_publish(*call_args, **call_kwargs):
                    publication_entered.set()
                    if not publication_release.wait(15):
                        raise TimeoutError("publication fixture barrier expired")
                    return original_publish(*call_args, **call_kwargs)

                store.publish = paused_publish
            stores.append(store)
            return store

        executor = AuthorizedScipExecutor(
            dbs["tool_gateway"],
            claim,
            snapshot.id,
            recipe_id,
            lifecycle,
            store_factory,
            manifest,
            encoder.receipt,
            proof,
            chunks,
            f"tcp:127.0.0.1:{port}",
            b"r" * 32,
            b"t" * 32,
            b"g" * 32,
            private,
        )
        gateway = ToolGateway(dbs["tool_gateway"], {"sandbox_cell": executor})
        if scenario in ("crash_after_publish", "crash_before_publish", "crash_after_issue"):
            import multiprocessing
            from concurrent.futures import ThreadPoolExecutor

            config = {
                "dsn": dbs["tool_gateway"].url,
                "claim": asdict(claim),
                "manifest": manifest,
                "repo": repo,
                "git": Path(git),
                "snapshot_id": snapshot.id,
                "recipe_id": recipe_id,
                "receipt": encoder.receipt,
                "proof": proof,
                "chunks": chunks,
                "endpoint": f"tcp:127.0.0.1:{port}",
                "private": private,
                "scenario": scenario,
                "args": args,
            }
            child = multiprocessing.get_context("spawn").Process(target=_crash_gateway_child, args=(config,))
            child.start()
            child.join(timeout=50)
            if child.is_alive():
                child.kill()
                child.join(timeout=5)
                pytest.fail("Gateway child did not reach the crash point")
            assert (
                child.exitcode
                == {"crash_after_publish": 137, "crash_before_publish": 138, "crash_after_issue": 139}[scenario]
            )
            row = dbs["owner"].one(
                "SELECT id,state,tool_intent_id FROM agent.execution_instances WHERE tenant_id=%s AND run_id=%s",
                (claim.tenant_id, claim.run_id),
            )
            expected_before_reconcile = {
                "crash_after_publish": "PUBLISHED",
                "crash_before_publish": "FINISHED",
                "crash_after_issue": "RUNNING",
            }[scenario]
            assert row is not None and row["state"] == expected_before_reconcile
            assert asyncio.run(executor.query("other:" + str(row["tool_intent_id"]))) is None
            if scenario == "crash_after_publish":
                stored = dbs["owner"].one(
                    "SELECT id,ciphertext FROM agent.execution_artifacts WHERE tenant_id=%s AND execution_id=%s",
                    (claim.tenant_id, row["id"]),
                )
                assert stored is not None
                original = bytes(stored["ciphertext"])
                corrupt = bytearray(original)
                corrupt[-1] ^= 1
                dbs["owner"].one(
                    "UPDATE agent.execution_artifacts SET ciphertext=%s WHERE tenant_id=%s AND id=%s RETURNING id",
                    (bytes(corrupt), claim.tenant_id, stored["id"]),
                )
                assert asyncio.run(gateway.reconcile(claim.tenant_id, str(row["tool_intent_id"]))) == {
                    "status": "UNKNOWN"
                }
                dbs["owner"].one(
                    "UPDATE agent.execution_artifacts SET ciphertext=%s WHERE tenant_id=%s AND id=%s RETURNING id",
                    (original, claim.tenant_id, stored["id"]),
                )
                artifact_keys[0] = KeyRing.single(b"x" * 32, "rotated")
                assert asyncio.run(gateway.reconcile(claim.tenant_id, str(row["tool_intent_id"]))) == {
                    "status": "UNKNOWN"
                }
                artifact_keys[0] = KeyRing({"fixture": b"k" * 32, "rotated": b"x" * 32}, "rotated")

            def reconcile_once(_):
                return asyncio.run(gateway.reconcile(claim.tenant_id, str(row["tool_intent_id"])))

            with ThreadPoolExecutor(max_workers=3) as workers:
                results = list(workers.map(reconcile_once, range(3)))
            expected = "CONFIRMED" if scenario == "crash_after_publish" else "UNKNOWN"
            assert all(item["status"] in ("UNKNOWN", expected) for item in results)
            assert asyncio.run(gateway.reconcile(claim.tenant_id, str(row["tool_intent_id"]))) == {"status": expected}
            intent = dbs["owner"].one(
                "SELECT status_code,result_json FROM agent.tool_intents WHERE tenant_id=%s AND id=%s",
                (claim.tenant_id, row["tool_intent_id"]),
            )
            assert intent is not None
            if scenario == "crash_after_publish":
                assert intent["status_code"] == "CONFIRMED"
                assert intent["result_json"]["receipt"].startswith("scip-artifact:")
            else:
                after = dbs["owner"].one(
                    "SELECT state FROM agent.execution_instances WHERE tenant_id=%s AND id=%s",
                    (claim.tenant_id, row["id"]),
                )
                assert after is not None and after["state"] == "UNKNOWN"
            count = dbs["owner"].one(
                "SELECT count(*) AS n FROM agent.execution_instances WHERE tenant_id=%s AND run_id=%s",
                (claim.tenant_id, claim.run_id),
            )
            assert count is not None and count["n"] == 1
            return
        if scenario != "success":
            from concurrent.futures import ThreadPoolExecutor

            entered, release = Event(), Event()
            original = lifecycle.running_index_binding

            def paused_binding(*call_args, **call_kwargs):
                entered.set()
                if not release.wait(10):
                    raise TimeoutError("fixture barrier expired")
                return original(*call_args, **call_kwargs)

            if scenario in ("cancel_before_registration", "disconnect_registration"):
                lifecycle.running_index_binding = paused_binding
            probe = (
                "import glob\n"
                "for p in glob.glob('/proc/[0-9]*/cmdline'):\n"
                " try: a=open(p,'rb').read().split(b'\\0')\n"
                " except OSError: continue\n"
                " if a and a[0]==b'/usr/local/bin/node' and b'/usr/local/lib/scip-python/index.js' in a:\n"
                "  print('running'); break\n"
                "else: print('absent')\n"
            )
            capture_probe = (
                "import glob; print('capturing' if glob.glob('/tmp/aiagent-scip-rpc-*/capture-started') else 'absent')"
            )
            artifact_probe = "import glob; print('transferring' if glob.glob('/tmp/aiagent-scip-rpc-*/artifact-transfer-started') else 'absent')"
            with ThreadPoolExecutor(max_workers=1) as workers:
                future = workers.submit(
                    lambda: asyncio.run(gateway.submit(claim, "scip-e2e-call", "sandbox_exec", args))
                )
                if scenario in ("cancel_before_registration", "disconnect_registration"):
                    assert entered.wait(10)
                elif scenario in (
                    "cancel_running",
                    "crash_running",
                    "expire_running",
                    "reconcile_running",
                    "db_unavailable_running",
                ):
                    end = time.monotonic() + 30
                    while time.monotonic() < end:
                        state = subprocess.check_output(
                            ["docker", "exec", name, "python", "-c", probe], text=True
                        ).strip()
                        if state == "running":
                            break
                        if future.done():
                            pytest.fail(f"indexer ended before cancellation: {future.result()}")
                    else:
                        pytest.fail("real indexer did not start")
                elif scenario == "cancel_capturing":
                    end = time.monotonic() + 30
                    while time.monotonic() < end:
                        state = subprocess.check_output(
                            ["docker", "exec", name, "python", "-c", capture_probe], text=True
                        ).strip()
                        if state == "capturing":
                            break
                        if future.done():
                            pytest.fail(f"capture ended before cancellation: {future.result()}")
                    else:
                        pytest.fail("real index capture did not start")
                elif scenario == "disconnect_artifact":
                    end = time.monotonic() + 30
                    while time.monotonic() < end:
                        state = subprocess.check_output(
                            ["docker", "exec", name, "python", "-c", artifact_probe], text=True
                        ).strip()
                        if state == "transferring":
                            break
                        if future.done():
                            pytest.fail(f"artifact transfer ended before disconnect: {future.result()}")
                    else:
                        pytest.fail("real artifact transfer did not start")
                else:
                    assert publication_entered.wait(30)
                if scenario in ("crash_running", "disconnect_registration", "disconnect_artifact"):
                    subprocess.run(["docker", "kill", name], check=True, capture_output=True)
                elif scenario == "reconcile_running":
                    row = dbs["owner"].one(
                        "SELECT tool_intent_id FROM agent.execution_instances WHERE tenant_id=%s AND run_id=%s",
                        (claim.tenant_id, claim.run_id),
                    )
                    assert row is not None
                    assert asyncio.run(gateway.reconcile(claim.tenant_id, str(row["tool_intent_id"]))) == {
                        "status": "UNKNOWN"
                    }
                elif scenario == "db_unavailable_running":
                    from urllib.parse import urlsplit

                    from psycopg import sql

                    role = urlsplit(dbs["tool_gateway"].url).username
                    assert role
                    with dbs["owner"].conn() as c:
                        c.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(sql.Identifier(role)))
                        c.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename=%s", (role,))
                    try:
                        end = time.monotonic() + 15
                        while time.monotonic() < end:
                            state = subprocess.check_output(
                                ["docker", "exec", name, "python", "-c", probe], text=True
                            ).strip()
                            if state == "absent":
                                break
                        else:
                            pytest.fail("indexer survived database outage")
                    finally:
                        with dbs["owner"].conn() as c:
                            c.execute(sql.SQL("ALTER ROLE {} LOGIN").format(sql.Identifier(role)))
                elif scenario == "expire_running":
                    dbs["owner"].one(
                        "UPDATE agent.runs SET lease_expires_at=clock_timestamp()-interval '1 second' "
                        "WHERE tenant_id=%s AND id=%s RETURNING id",
                        (claim.tenant_id, claim.run_id),
                    )
                else:
                    commands.cancel(dbs["api"], scope, claim.task_id)
                release.set()
                publication_release.set()
                try:
                    result = future.result(
                        timeout=60 if scenario in ("db_unavailable_running", "disconnect_registration") else 20
                    )
                    assert result["status"] == "UNKNOWN", result
                except control.ControlError:
                    pass
                except (psycopg.Error, PoolTimeout, RpcError):
                    if scenario != "db_unavailable_running":
                        raise
            if scenario in ("cancel_running", "expire_running", "reconcile_running", "db_unavailable_running"):
                assert (
                    subprocess.check_output(["docker", "exec", name, "python", "-c", probe], text=True).strip()
                    == "absent"
                )
            row = dbs["owner"].one(
                "SELECT state,tool_intent_id FROM agent.execution_instances WHERE tenant_id=%s AND run_id=%s",
                (claim.tenant_id, claim.run_id),
            )
            assert row is not None
            if scenario == "db_unavailable_running":
                assert asyncio.run(gateway.reconcile(claim.tenant_id, str(row["tool_intent_id"]))) == {
                    "status": "UNKNOWN"
                }
                row = dbs["owner"].one(
                    "SELECT state,tool_intent_id FROM agent.execution_instances WHERE tenant_id=%s AND run_id=%s",
                    (claim.tenant_id, claim.run_id),
                )
                assert row is not None
            assert row["state"] == "UNKNOWN"
            count = dbs["owner"].one(
                "SELECT count(*) AS n FROM agent.execution_artifacts WHERE tenant_id=%s", (claim.tenant_id,)
            )
            assert count is not None and count["n"] == 0
            if scenario == "crash_running":
                replay = asyncio.run(gateway.submit(claim, "scip-e2e-call", "sandbox_exec", args))
                assert replay["status"] == "UNKNOWN"
                assert asyncio.run(gateway.reconcile(claim.tenant_id, str(row["tool_intent_id"]))) == {
                    "status": "UNKNOWN"
                }
            return
        result = asyncio.run(gateway.submit(claim, "scip-e2e-call", "sandbox_exec", args))
        assert result["status"] == "CONFIRMED", result
        row = dbs["owner"].one(
            "SELECT id,state,tool_attempt_id FROM agent.execution_instances WHERE tenant_id=%s AND run_id=%s",
            (claim.tenant_id, claim.run_id),
        )
        assert row is not None and row["state"] == "PUBLISHED" and len(stores) == 1
        reference, index = stores[0].fetch(str(row["id"]), deadline=time.monotonic() + 10)
        assert reference.coverage == "PARTIAL" and reference.byte_length == len(index)
        index_manifest = IndexManifest(
            identity,
            manifest.tree,
            reference.sha256,
            "scip-python",
            "0.6.8",
            0,
            tuple(SourceProvenance(f.path, f.blob_id, f.sha256) for f in manifest.files),
        )
        loaded = load_index(snapshot_factory(), index_manifest, index)
        assert loaded.status in (IndexStatus.PARTIAL, IndexStatus.VALIDATED) and loaded.graph is not None
        symbol = "scip-python python dev13-workspace 1.0 library/greet()."
        assert loaded.graph.definitions(symbol)[0].path == "library.py"
        assert {item.path for item in loaded.graph.references(symbol)} == {"main.py"}
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
