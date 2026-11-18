"""Verify canonical index authority before deriving descriptor-specific argv.

This authenticates approved data and a fixed recipe, not the Cell image or a
live lease. The caller must enforce both separately.
"""

from __future__ import annotations

import hmac
import time
from typing import Any

from aiagent_development.code_intelligence.indexer_recipe import (
    PYTHON_SCIP_GRANT_ARGV,
    approved_recipe_digest,
    python_scip_recipe_digest,
)
from aiagent_domain import sha256_hex, uuid7

from .grant import Grant, sign
from .source_ticket import verify_source_ticket


def verify_index_binding(ticket: dict[str, Any], binding: dict[str, Any]) -> None:
    """Bind the fixed SCIP template to the complete DB-authenticated Execution."""
    try:
        expected = {
            "execution": ticket["execution"],
            "tenant": ticket["tenant"],
            "owner": ticket["owner"],
            "task": ticket["task"],
            "run": ticket["run"],
            "goal_revision": ticket["goal"],
            "lease_epoch": ticket["lease"],
            "attempt": ticket["attempt"],
            "snapshot": ticket["snapshot"],
            "recipe_id": ticket["recipe"],
            "repository_binding": ticket["repository_binding"],
            "commit": ticket["commit"],
            "tree": ticket["tree"],
            "manifest_sha256": ticket["manifest_sha256"],
            "source_sha256": ticket["source_sha256"],
        }
        if (
            not isinstance(binding, dict)
            or any(binding.get(k) != v for k, v in expected.items())
            or str(binding["workspace_revision"]) != ticket["revision"]
            or not hmac.compare_digest(sha256_hex(binding), ticket["binding_sha256"])
            or binding["test_id"] is not None
            or any(
                type(binding[k]) is not str or not binding[k]
                for k in (
                    "intent",
                    "plan_sha256",
                    "request_sha256",
                    "recipe_sha256",
                    "toolchain_sha256",
                    "image_digest",
                )
            )
        ):
            raise ValueError("index binding rejected")
        recipe = approved_recipe_digest(
            kind="index",
            test_id=None,
            plan_sha256=binding["plan_sha256"],
            request_sha256=binding["request_sha256"],
            template_sha256=python_scip_recipe_digest(),
            toolchain_sha256=binding["toolchain_sha256"],
            image_digest=binding["image_digest"],
        )
        if not hmac.compare_digest(recipe, binding["recipe_sha256"]):
            raise ValueError("index recipe rejected")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("index authority rejected") from exc


def issue_index_grant(
    ticket_key: bytes,
    grant_key: bytes,
    ticket: dict[str, Any],
    binding: dict[str, Any],
) -> dict[str, Any]:
    """Sign only the fixed recipe marker, never caller-selected argv."""
    p = verify_source_ticket(ticket_key, ticket)
    verify_index_binding(p, binding)
    if len(grant_key) < 32 or p["expires_at"] <= time.time():
        raise ValueError("index authority rejected")
    grant = Grant(
        uuid7(),
        p["tenant"],
        p["task"],
        p["run"],
        binding["intent"],
        p["lease"],
        PYTHON_SCIP_GRANT_ARGV,
        ".",
        30000,
        2 * 1024**3,
        p["expires_at"],
        attempt_id=p["attempt"],
        attempt_binding_version=1,
        execution_binding_version=1,
        execution_id=p["execution"],
        execution_binding_sha256=p["binding_sha256"],
    )
    return sign(grant_key, grant)
