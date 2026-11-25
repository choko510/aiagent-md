"""Fresh test-profile Gateway adapter process for durable Git recovery E2E."""

from __future__ import annotations

import json
import os
import sys

from aiagent_core.control import Claim
from aiagent_core.db import Database
from aiagent_development.contracts import ProjectScope
from aiagent_development.pg_store import PostgresCycleStore, PostgresRunAuthority, PostgresWorkspacePort
from aiagent_development.workflow import DevelopmentWorkflow
from aiagent_gateways.context_keys import KeyRing
from aiagent_gateways.git_commit_evidence import GitCommitEvidenceReader
from aiagent_gateways.git_head_reader import RpcGitHeadReader
from aiagent_gateways.git_object_verifier import RpcGitObjectVerifier
from aiagent_gateways.git_provider_adapter import ToolGatewayGitProvider
from aiagent_gateways.git_receipts import GitReceiptStore


def main() -> None:
    if os.environ.get("AIAGENT_PROFILE") != "test":
        raise RuntimeError("fixture is test-only")
    data = json.load(sys.stdin)
    claim, scope = Claim.from_wire(data["claim"]), ProjectScope(**data["scope"])
    db = Database(data["dsn"])
    try:
        head = RpcGitHeadReader(
            db,
            claim,
            binding_id=data["binding"],
            generation=2,
            endpoint=data["endpoint"],
            rpc_key=b"r" * 32,
            grant_key=b"g" * 32,
            worker_id="fixture-worker",
            cycle_id=data["cycle"],
        )
        receipts = GitReceiptStore(
            db,
            claim,
            KeyRing.single(b"p" * 32),
            result_key=b"s" * 32,
            worker_id="fixture-worker",
            object_verifier=RpcGitObjectVerifier(data["endpoint"], b"r" * 32, b"g" * 32, "fixture-worker"),
            require_fenced=True,
            recovery_journal_key=b"j" * 32,
        )
        provider = ToolGatewayGitProvider(
            db,
            claim,
            endpoint="unix:/fixture-unused-gateway.sock",
            rpc_key=b"d" * 32,
            head_reader=head,
            receipt_reader=GitCommitEvidenceReader(db, claim, receipts),
        )
        store = PostgresCycleStore(db)
        workflow = DevelopmentWorkflow(
            runs=PostgresRunAuthority(db),
            workspaces=PostgresWorkspacePort(db),
            gateway=provider,
            git=provider,
            store=store,
            reviewers=None,
            sandbox=None,
        )  # type: ignore[arg-type]
        result = workflow.reconcile_commit(scope, data["cycle"])
        if result.commit is None:
            raise RuntimeError("recovery did not produce a commit")
        print(json.dumps({"phase": result.phase, "commit": result.commit.commit_sha}))
    finally:
        db.close()


if __name__ == "__main__":
    main()
