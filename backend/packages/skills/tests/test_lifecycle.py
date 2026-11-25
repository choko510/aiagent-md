from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiagent_skills.bundle import inspect_zip
from aiagent_skills.contracts import InstallationState, ReleaseState
from aiagent_skills.digest import capability_digest, dependency_digest
from aiagent_skills.errors import ArchiveRejected, FrontmatterInvalid, SkillError
from aiagent_skills.frontmatter import parse_skill_markdown
from aiagent_skills.policy import validate_requested
from aiagent_skills.quarantine import QuarantineStore
from aiagent_skills.registry import InMemorySkillStore, SkillRegistry, make_approval
from aiagent_skills.sources import upload_source

TENANT = "018f0000-0000-7000-8000-000000000001"
OWNER = "018f0000-0000-7000-8000-000000000002"


def make_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def skill_md(name: str = "demo-skill", version: str = "1.0.0", extra: str = "") -> bytes:
    return (
        "---\n"
        f"name: {name}\n"
        "description: A small deterministic Skill used by lifecycle tests.\n"
        f"version: {version}\n"
        "allowed-tools: tool:web.search\n"
        f"{extra}"
        "---\n\n"
        "# Demo\n\nRead this only when the user asks for the demo procedure.\n"
    ).encode()


def registry(tmp_path: Path) -> tuple[SkillRegistry, InMemorySkillStore, QuarantineStore]:
    store = InMemorySkillStore()
    quarantine = QuarantineStore(tmp_path / "quarantine")
    return SkillRegistry(store, quarantine=quarantine), store, quarantine


def ingest(reg: SkillRegistry, archive: bytes, request_id: str | None = None) -> str:
    request_id = request_id or f"ingest-{hashlib.sha256(archive).hexdigest()[:16]}"
    receipt = reg.ingest(
        tenant_id=TENANT,
        owner_user_id=OWNER,
        actor_user_id=OWNER,
        source=upload_source(archive).source,
        archive=archive,
        request_id=request_id,
    )
    assert receipt.release_id is not None
    return receipt.release_id


def test_lifecycle_install_enable_pin_and_load(tmp_path: Path) -> None:
    reg, store, _ = registry(tmp_path)
    release_id = ingest(reg, make_zip({"SKILL.md": skill_md(), "references/notes.md": b"note"}))
    release = store.get_release(TENANT, release_id)
    assert release.state is ReleaseState.STAGED_FOR_REVIEW
    assert release.scan is not None and not release.scan.blocking

    capability = capability_digest(validate_requested(release.manifest.requested_capabilities))
    install_approval = make_approval(
        tenant_id=TENANT,
        owner_user_id=OWNER,
        actor_user_id=OWNER,
        action="install",
        release=release,
        capability_digest_value=capability,
        policy_revision="policy:1",
        request_id="req-install",
        expected_installation_revision=0,
    )
    installed = reg.install(
        tenant_id=TENANT,
        owner_user_id=OWNER,
        actor_user_id=OWNER,
        release_id=release_id,
        approval=install_approval,
        capability_digest_value=capability,
        policy_revision="policy:1",
        request_id="req-install",
    )
    assert installed.installation_id is not None
    installation = store.get_installation(TENANT, installed.installation_id)
    assert installation.effective is InstallationState.INSTALLED_DISABLED

    enable_approval = make_approval(
        tenant_id=TENANT,
        owner_user_id=OWNER,
        actor_user_id=OWNER,
        action="enable",
        release=release,
        capability_digest_value=capability,
        policy_revision="policy:1",
        request_id="req-enable",
        expected_installation_revision=installation.record_version,
    )
    reg.enable(
        tenant_id=TENANT,
        owner_user_id=OWNER,
        actor_user_id=OWNER,
        installation_id=installation.id,
        approval=enable_approval,
        capability_digest_value=capability,
        policy_revision="policy:1",
        expected_revision=installation.record_version,
        request_id="req-enable",
    )
    enabled = store.get_installation(TENANT, installation.id)
    assert enabled.effective is InstallationState.ENABLED

    catalog = reg.catalog(TENANT, OWNER)
    assert [entry.name for entry in catalog.all_entries()] == ["demo-skill"]
    assert "Read this only" not in str([entry.to_wire() for entry in catalog.all_entries()])

    pin = reg.pin_for_run(
        tenant_id=TENANT,
        run_id="run-1",
        installation_id=enabled.id,
        capability_digest_value=capability,
        dependency_digest_value=dependency_digest({}),
    )
    assert pin.content_digest == release.content_digest
    text = reg.load_pinned_body(
        tenant_id=TENANT,
        run_id="run-1",
        installation_id=enabled.id,
        max_bytes=32,
    )
    assert text.content_digest == release.content_digest
    assert text.text.startswith("---")
    assert text.truncated is True
    assert text.next_offset is not None

    disabled = reg.disable(
        tenant_id=TENANT,
        owner_user_id=OWNER,
        actor_user_id=OWNER,
        installation_id=enabled.id,
        expected_revision=enabled.record_version,
        request_id="req-disable",
    )
    assert disabled.result["installation"]["effective"] == "SUSPENDED"
    assert reg.catalog(TENANT, OWNER).all_entries() == ()


def test_idempotency_and_approval_binding(tmp_path: Path) -> None:
    reg, store, _ = registry(tmp_path)
    archive = make_zip({"SKILL.md": skill_md()})
    first = reg.ingest(
        tenant_id=TENANT,
        owner_user_id=OWNER,
        actor_user_id=OWNER,
        source=upload_source(archive).source,
        archive=archive,
        request_id="same-key",
    )
    second = reg.ingest(
        tenant_id=TENANT,
        owner_user_id=OWNER,
        actor_user_id=OWNER,
        source=upload_source(archive).source,
        archive=archive,
        request_id="same-key",
    )
    assert first.id == second.id
    with pytest.raises(SkillError):
        reg.ingest(
            tenant_id=TENANT,
            owner_user_id=OWNER,
            actor_user_id=OWNER,
            source=upload_source(make_zip({"SKILL.md": skill_md(version="1.0.1")})).source,
            archive=make_zip({"SKILL.md": skill_md(version="1.0.1")}),
            request_id="same-key",
        )

    release = store.get_release(TENANT, first.release_id or "")
    capability = capability_digest(validate_requested(release.manifest.requested_capabilities))
    approval = make_approval(
        tenant_id=TENANT,
        owner_user_id=OWNER,
        actor_user_id=OWNER,
        action="install",
        release=release,
        capability_digest_value=capability,
        policy_revision="policy:1",
        request_id="install-1",
        expected_installation_revision=0,
    )
    with pytest.raises(SkillError):
        reg.install(
            tenant_id=TENANT,
            owner_user_id=OWNER,
            actor_user_id=OWNER,
            release_id=release.id,
            approval=approval,
            capability_digest_value=capability_digest(frozenset()),
            policy_revision="policy:1",
            request_id="install-1",
        )


@pytest.mark.parametrize(
    "files",
    [
        {"../escape.md": b"x", "SKILL.md": skill_md()},
        {"SKILL.md": skill_md(), "run.exe": b"MZ"},
        {"SKILL.md": skill_md(), "nested.zip": b"PK\x03\x04"},
        {"SKILL.md": skill_md(), "dir/../../escape.md": b"x"},
        {"skill.md": skill_md(), "SKILL.md": skill_md()},
    ],
)
def test_archive_attacks_are_rejected(files: dict[str, bytes]) -> None:
    with pytest.raises(ArchiveRejected):
        inspect_zip(make_zip(files))


def test_yaml_object_and_alias_attacks_are_rejected() -> None:
    with pytest.raises(FrontmatterInvalid):
        parse_skill_markdown(b"---\nname: !!python/object/apply:os.system ['calc']\n---\n")
    with pytest.raises(FrontmatterInvalid):
        parse_skill_markdown(b"---\nname: demo\nname: other\n---\n")
    with pytest.raises(FrontmatterInvalid):
        parse_skill_markdown(b"---\na: &x [*x]\n---\n")


def test_builtin_name_is_reserved(tmp_path: Path) -> None:
    reg, _, _ = registry(tmp_path)
    archive = make_zip({"SKILL.md": skill_md(name="nihongo")})
    with pytest.raises(SkillError):
        ingest(reg, archive)


def install_and_enable(reg: SkillRegistry, store: InMemorySkillStore, release_id: str) -> str:
    release = store.get_release(TENANT, release_id)
    capability = capability_digest(validate_requested(release.manifest.requested_capabilities))
    approval = make_approval(
        tenant_id=TENANT,
        owner_user_id=OWNER,
        actor_user_id=OWNER,
        action="install",
        release=release,
        capability_digest_value=capability,
        policy_revision="policy:1",
        request_id=f"install-{release_id}",
        expected_installation_revision=0,
    )
    receipt = reg.install(
        tenant_id=TENANT,
        owner_user_id=OWNER,
        actor_user_id=OWNER,
        release_id=release_id,
        approval=approval,
        capability_digest_value=capability,
        policy_revision="policy:1",
        request_id=f"install-{release_id}",
    )
    installation = store.get_installation(TENANT, receipt.installation_id or "")
    enable_approval = make_approval(
        tenant_id=TENANT,
        owner_user_id=OWNER,
        actor_user_id=OWNER,
        action="enable",
        release=release,
        capability_digest_value=capability,
        policy_revision="policy:1",
        request_id=f"enable-{release_id}",
        expected_installation_revision=installation.record_version,
    )
    reg.enable(
        tenant_id=TENANT,
        owner_user_id=OWNER,
        actor_user_id=OWNER,
        installation_id=installation.id,
        approval=enable_approval,
        capability_digest_value=capability,
        policy_revision="policy:1",
        expected_revision=installation.record_version,
        request_id=f"enable-{release_id}",
    )
    return installation.id


def test_update_rollback_and_revoke(tmp_path: Path) -> None:
    reg, store, _ = registry(tmp_path)
    v1 = ingest(reg, make_zip({"SKILL.md": skill_md(version="1.0.0")}))
    installation_id = install_and_enable(reg, store, v1)
    v2 = ingest(reg, make_zip({"SKILL.md": skill_md(version="1.1.0")}))
    installation = store.get_installation(TENANT, installation_id)
    target = store.get_release(TENANT, v2)
    capability = capability_digest(validate_requested(target.manifest.requested_capabilities))
    approval = make_approval(
        tenant_id=TENANT,
        owner_user_id=OWNER,
        actor_user_id=OWNER,
        action="update",
        release=target,
        capability_digest_value=capability,
        policy_revision="policy:1",
        request_id="update-1",
        expected_installation_revision=installation.record_version,
    )
    reg.update(
        tenant_id=TENANT,
        owner_user_id=OWNER,
        actor_user_id=OWNER,
        installation_id=installation_id,
        target_release_id=v2,
        approval=approval,
        capability_digest_value=capability,
        policy_revision="policy:1",
        expected_revision=installation.record_version,
        enable_after=True,
        request_id="update-1",
    )
    updated = store.get_installation(TENANT, installation_id)
    assert updated.effective is InstallationState.ENABLED
    assert updated.release_id == v2

    back = store.get_release(TENANT, v1)
    rollback_approval = make_approval(
        tenant_id=TENANT,
        owner_user_id=OWNER,
        actor_user_id=OWNER,
        action="rollback",
        release=back,
        capability_digest_value=capability,
        policy_revision="policy:1",
        request_id="rollback-1",
        expected_installation_revision=updated.record_version,
    )
    reg.rollback(
        tenant_id=TENANT,
        owner_user_id=OWNER,
        actor_user_id=OWNER,
        installation_id=installation_id,
        target_release_id=v1,
        approval=rollback_approval,
        capability_digest_value=capability,
        policy_revision="policy:1",
        expected_revision=updated.record_version,
        reenable=True,
        request_id="rollback-1",
    )
    rolled = store.get_installation(TENANT, installation_id)
    assert rolled.release_id == v1
    assert rolled.effective is InstallationState.ENABLED

    reg.revoke(
        tenant_id=TENANT,
        owner_user_id=OWNER,
        actor_user_id=OWNER,
        installation_id=installation_id,
        request_id="revoke-1",
    )
    assert store.get_installation(TENANT, installation_id).effective is InstallationState.REVOKED
    assert reg.catalog(TENANT, OWNER).all_entries() == ()
    with pytest.raises(SkillError):
        reg.pin_for_run(
            tenant_id=TENANT,
            run_id="run-revoked",
            installation_id=installation_id,
            capability_digest_value=capability,
            dependency_digest_value=dependency_digest({}),
        )


def test_install_cannot_cross_owner(tmp_path: Path) -> None:
    reg, store, _ = registry(tmp_path)
    release_id = ingest(reg, make_zip({"SKILL.md": skill_md()}))
    release = store.get_release(TENANT, release_id)
    capability = capability_digest(validate_requested(release.manifest.requested_capabilities))
    approval = make_approval(
        tenant_id=TENANT,
        owner_user_id=OWNER,
        actor_user_id=OWNER,
        action="install",
        release=release,
        capability_digest_value=capability,
        policy_revision="policy:1",
        request_id="cross-owner",
        expected_installation_revision=0,
    )
    with pytest.raises(SkillError):
        reg.install(
            tenant_id=TENANT,
            owner_user_id="018f0000-0000-7000-8000-000000000099",
            actor_user_id="018f0000-0000-7000-8000-000000000099",
            release_id=release_id,
            approval=approval,
            capability_digest_value=capability,
            policy_revision="policy:1",
            request_id="cross-owner",
        )


def test_failed_update_restores_enabled_state() -> None:
    from aiagent_skills.lifecycle import abort_update, begin_update

    release = SimpleNamespace(
        id="r1", content_digest="a" * 64, manifest=SimpleNamespace(name="x"), state=ReleaseState.STAGED_FOR_REVIEW
    )
    target = SimpleNamespace(
        id="r2", content_digest="b" * 64, manifest=SimpleNamespace(name="x"), state=ReleaseState.STAGED_FOR_REVIEW
    )
    install = store_installation()
    started = begin_update(
        install, release, target, expected_revision=install.record_version, now="2026-01-01T00:00:00Z"
    )
    restored = abort_update(started, now="2026-01-01T00:00:01Z", restore=InstallationState.ENABLED)
    assert restored.effective is InstallationState.ENABLED
    assert restored.release_id == install.release_id


def store_installation():
    from aiagent_skills.contracts import DesiredState, SkillInstallation

    return SkillInstallation(
        id="i1",
        tenant_id=TENANT,
        owner_user_id=OWNER,
        skill_name="x",
        release_id="r1",
        desired=DesiredState.ENABLED,
        effective=InstallationState.ENABLED,
        record_version=2,
        activation_generation=1,
        policy_revision="policy:1",
        approved_release_digest="a" * 64,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


def test_same_skill_version_is_immutable(tmp_path: Path) -> None:
    reg, _, _ = registry(tmp_path)
    first = ingest(reg, make_zip({"SKILL.md": skill_md(version="1.0.0")}))
    assert first
    assert len(reg.store.list_releases(TENANT)) == 1, [r.manifest.version for r in reg.store.list_releases(TENANT)]
    with pytest.raises(SkillError):
        ingest(reg, make_zip({"SKILL.md": skill_md(version="1.0.0")}), request_id="second-ingest")


def test_rollback_to_current_release_is_rejected(tmp_path: Path) -> None:
    reg, store, _ = registry(tmp_path)
    release_id = ingest(reg, make_zip({"SKILL.md": skill_md()}))
    installation_id = install_and_enable(reg, store, release_id)
    installation = store.get_installation(TENANT, installation_id)
    release = store.get_release(TENANT, release_id)
    capability = capability_digest(validate_requested(release.manifest.requested_capabilities))
    approval = make_approval(
        tenant_id=TENANT,
        owner_user_id=OWNER,
        actor_user_id=OWNER,
        action="rollback",
        release=release,
        capability_digest_value=capability,
        policy_revision="policy:1",
        request_id="rollback-current",
        expected_installation_revision=installation.record_version,
    )
    with pytest.raises(SkillError):
        reg.rollback(
            tenant_id=TENANT,
            owner_user_id=OWNER,
            actor_user_id=OWNER,
            installation_id=installation_id,
            target_release_id=release_id,
            approval=approval,
            capability_digest_value=capability,
            policy_revision="policy:1",
            expected_revision=installation.record_version,
            reenable=True,
            request_id="rollback-current",
        )


def test_required_dependency_must_be_enabled_and_is_pinned(tmp_path: Path) -> None:
    reg, store, _ = registry(tmp_path)
    base = ingest(reg, make_zip({"SKILL.md": skill_md(name="base-skill")}))
    dependent_md = skill_md(
        name="dependent-skill",
        extra="dependencies:\n  - skillName: base-skill\n    range: ^1.0.0\n",
    )
    dependent = ingest(reg, make_zip({"SKILL.md": dependent_md}))

    with pytest.raises(SkillError):
        install_and_enable(reg, store, dependent)
    install_and_enable(reg, store, base)
    dependent_installation = install_and_enable(reg, store, dependent)

    release = store.get_release(TENANT, dependent)
    resolution = reg.resolve_dependencies(TENANT, OWNER, release)
    assert resolution.pins == {"base-skill": "1.0.0"}
    capability = capability_digest(validate_requested(release.manifest.requested_capabilities))
    pin = reg.pin_for_run(
        tenant_id=TENANT,
        run_id="run-dependency",
        installation_id=dependent_installation,
        capability_digest_value=capability,
        dependency_digest_value=resolution.digest,
    )
    assert pin.dependency_digest == resolution.digest
