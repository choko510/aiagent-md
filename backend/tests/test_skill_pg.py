"""PostgreSQL persistence, RLS and least-privilege checks for Skill lifecycle."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import psycopg
import pytest
from aiagent_core import admin
from aiagent_core.db import Database
from aiagent_skills.contracts import InstallationState, ReleaseState
from aiagent_skills.digest import capability_digest
from aiagent_skills.errors import SkillNotFound
from aiagent_skills.pg_store import PostgresSkillStore
from aiagent_skills.policy import validate_requested
from aiagent_skills.quarantine import QuarantineStore
from aiagent_skills.registry import SkillRegistry, make_approval
from aiagent_skills.sources import upload_source
from conftest import PgEnv

pytestmark = pytest.mark.pg


def make_zip(name: str, version: str) -> bytes:
    body = (
        "---\n"
        f"name: {name}\n"
        "description: PostgreSQL lifecycle test Skill.\n"
        f"version: {version}\n"
        "allowed-tools: tool:web.search\n"
        "---\n\n# Test\n\nContent is data.\n"
    ).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("SKILL.md", body)
        archive.writestr("references/notes.md", b"notes")
    return buffer.getvalue()


def test_postgres_lifecycle_rls_and_least_privilege(
    memory_pg: PgEnv,
    memory_dbs: dict[str, Database],
    memory_tenant: dict[str, str],
    tmp_path: Path,
) -> None:
    tenant_id = memory_tenant["tenantId"]
    owner_id = memory_tenant["userId"]
    store = PostgresSkillStore(memory_dbs["api"])
    quarantine = QuarantineStore(tmp_path / "quarantine")
    registry = SkillRegistry(store, quarantine)
    name = f"pg-skill-{tenant_id[-8:]}"

    archive = make_zip(name, "1.0.0")
    ingested = registry.ingest(
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        actor_user_id=owner_id,
        source=upload_source(archive).source,
        archive=archive,
        request_id="pg-ingest-1",
    )
    assert ingested.release_id is not None
    release = store.get_release(tenant_id, ingested.release_id)
    assert release.state is ReleaseState.STAGED_FOR_REVIEW
    assert len(release.content_digest) == 64

    with memory_dbs["owner"].tx(tenant_id) as conn:
        row = conn.execute(
            "SELECT tenant_id, content_digest, scan_digest FROM agent.skill_releases WHERE id=%s",
            (release.id,),
        ).fetchone()
    assert row is not None
    assert str(row["tenant_id"]) == tenant_id
    assert row["content_digest"] == release.content_digest
    assert row["scan_digest"] == release.scan_digest
    assert len(row["scan_digest"]) == 64

    other = admin.provision_tenant(memory_pg.owner)
    with pytest.raises(SkillNotFound):
        store.get_release(other["tenantId"], release.id)
    with memory_dbs["api"].tx(other["tenantId"]) as conn:
        assert conn.execute("SELECT count(*) AS n FROM agent.skill_releases").fetchone()["n"] == 0

    capability = capability_digest(validate_requested(release.manifest.requested_capabilities))
    approval = make_approval(
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        actor_user_id=owner_id,
        action="install",
        release=release,
        capability_digest_value=capability,
        policy_revision="policy:pg",
        request_id="pg-install-1",
        expected_installation_revision=0,
    )
    installed = registry.install(
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        actor_user_id=owner_id,
        release_id=release.id,
        approval=approval,
        capability_digest_value=capability,
        policy_revision="policy:pg",
        request_id="pg-install-1",
    )
    assert installed.installation_id is not None
    installation = store.get_installation(tenant_id, installed.installation_id)
    assert installation.effective is InstallationState.INSTALLED_DISABLED

    enable = make_approval(
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        actor_user_id=owner_id,
        action="enable",
        release=release,
        capability_digest_value=capability,
        policy_revision="policy:pg",
        request_id="pg-enable-1",
        expected_installation_revision=installation.record_version,
    )
    registry.enable(
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        actor_user_id=owner_id,
        installation_id=installation.id,
        approval=enable,
        capability_digest_value=capability,
        policy_revision="policy:pg",
        expected_revision=installation.record_version,
        request_id="pg-enable-1",
    )
    enabled = store.get_installation(tenant_id, installation.id)
    assert enabled.effective is InstallationState.ENABLED

    pin = registry.pin_for_run(
        tenant_id=tenant_id,
        run_id="018f0000-0000-7000-8000-0000000000bb",
        installation_id=installation.id,
        capability_digest_value=capability,
        dependency_digest_value=capability_digest(frozenset()),
    )
    assert store.get_pin(tenant_id, pin.run_id, installation.id) == pin

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with memory_dbs["runtime"].tx(tenant_id) as conn:
            conn.execute(
                "INSERT INTO agent.skill_releases (id, tenant_id, owner_user_id, skill_name, version,"
                " content_digest, state, trust_level, scan_digest, scan, manifest, source_kind, created_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,now())",
                (
                    "018f0000-0000-7000-8000-0000000000cc",
                    tenant_id,
                    owner_id,
                    "forbidden",
                    "1.0.0",
                    "a" * 64,
                    "STAGED_FOR_REVIEW",
                    "third_party_untrusted",
                    "b" * 64,
                    "{}",
                    "{}",
                    "upload",
                ),
            )

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with memory_dbs["api"].tx(tenant_id) as conn:
            conn.execute("UPDATE agent.skill_run_pins SET policy_revision='changed' WHERE run_id=%s", (pin.run_id,))
