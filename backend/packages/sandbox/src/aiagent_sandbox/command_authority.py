"""Exact approved-command binding; does not itself authorize or execute a process."""

from __future__ import annotations

import hmac
import time
from pathlib import PurePosixPath
from typing import Any

from aiagent_development.code_intelligence.indexer_recipe import approved_recipe_digest
from aiagent_domain import sha256_hex, uuid7

from .grant import Grant, sign
from .scip_authority import verify_execution_binding
from .source_ticket import verify_source_ticket


def command_template_digest(descriptor: dict[str, Any]) -> str:
    """Commit to a bounded exact argv, current directory, operation and Test ID."""
    if not isinstance(descriptor, dict) or set(descriptor) != {"kind", "test_id", "argv", "cwdRelative"}:
        raise ValueError("invalid command descriptor")
    kind, test_id, argv, cwd = (descriptor[k] for k in ("kind", "test_id", "argv", "cwdRelative"))
    if (
        kind not in ("build", "static-check", "test")
        or cwd != "."
        or (kind == "test") != (test_id is not None)
        or (
            test_id is not None
            and (
                type(test_id) is not str
                or not 1 <= len(test_id) <= 256
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in test_id)
            )
        )
        or not isinstance(argv, list)
        or not 1 <= len(argv) <= 128
        or any(
            type(item) is not str
            or not item
            or len(item.encode("utf-8")) > 4096
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in item)
            for item in argv
        )
        or sum(len(item.encode("utf-8")) for item in argv) > 32768
    ):
        raise ValueError("invalid command descriptor")
    executable = argv[0]
    path = PurePosixPath(executable)
    if (
        not path.is_absolute()
        or str(path) != executable
        or any(part in (".", "..") for part in path.parts)
        or not (executable.startswith("/usr/bin/") or executable.startswith("/usr/local/bin/"))
    ):
        raise ValueError("command executable is not allowlist eligible")
    return sha256_hex({"version": "dev-command-template-v1", **descriptor})


def verify_command_binding(ticket: dict[str, Any], binding: dict[str, Any], descriptor: dict[str, Any]) -> None:
    """Require the exact command and planned Test ID committed by the canonical recipe."""
    try:
        verify_execution_binding(ticket, binding)
        template = command_template_digest(descriptor)
        if binding["test_id"] != descriptor["test_id"]:
            raise ValueError("command test ID mismatch")
        request_digest = sha256_hex(
            {
                "tool": "sandbox_exec",
                "args": {
                    "argv": descriptor["argv"],
                    "cwdRelative": descriptor["cwdRelative"],
                },
            }
        )
        if not hmac.compare_digest(request_digest, binding["request_sha256"]):
            raise ValueError("command request mismatch")
        recipe = approved_recipe_digest(
            kind=descriptor["kind"],
            test_id=descriptor["test_id"],
            plan_sha256=binding["plan_sha256"],
            request_sha256=request_digest,
            template_sha256=template,
            toolchain_sha256=binding["toolchain_sha256"],
            image_digest=binding["image_digest"],
        )
        if not hmac.compare_digest(recipe, binding["recipe_sha256"]):
            raise ValueError("command recipe mismatch")
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("command authority rejected") from exc


def issue_command_grant(
    ticket_key: bytes,
    grant_key: bytes,
    ticket: dict[str, Any],
    binding: dict[str, Any],
    descriptor: dict[str, Any],
) -> dict[str, Any]:
    """Sign only the exact approved argv; Supervisor still enforces its allowlist."""
    p = verify_source_ticket(ticket_key, ticket)
    verify_command_binding(p, binding, descriptor)
    if len(grant_key) < 32 or p["expires_at"] <= time.time():
        raise ValueError("command authority rejected")
    grant = Grant(
        uuid7(),
        p["tenant"],
        p["task"],
        p["run"],
        binding["intent"],
        p["lease"],
        tuple(descriptor["argv"]),
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
