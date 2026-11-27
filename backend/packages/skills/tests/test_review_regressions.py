from __future__ import annotations

import io
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from aiagent_skills.contracts import InstallationState, SkillDependency
from aiagent_skills.dependency import DependencyError, DependencyResolver, VersionRange
from aiagent_skills.digest import capability_digest, dependency_digest
from aiagent_skills.errors import SkillError
from aiagent_skills.identity import assert_same_lineage
from aiagent_skills.pinning import disclose_text
from aiagent_skills.policy import validate_requested
from aiagent_skills.quarantine import QuarantineStore
from aiagent_skills.registry import InMemorySkillStore, SkillRegistry, make_approval
from aiagent_skills.sources import _rewrite_github_archive
from test_lifecycle import OWNER, TENANT, ingest, install_and_enable, make_zip, registry, skill_md


def test_disclosure_slices_are_utf8_boundaries() -> None:
    body = "あいう".encode()
    first = disclose_text(skill_id="s", path="SKILL.md", body=body, content_digest_value="d" * 64, max_bytes=4)
    assert (first.text, first.next_offset, first.truncated) == ("あ", 3, True)
    second = disclose_text(
        skill_id="s", path="SKILL.md", body=body, content_digest_value="d" * 64, offset=3, max_bytes=4
    )
    assert (second.text, second.next_offset, second.truncated) == ("い", 6, True)
    third = disclose_text(
        skill_id="s", path="SKILL.md", body=body, content_digest_value="d" * 64, offset=6, max_bytes=4
    )
    assert (third.text, third.next_offset, third.truncated) == ("う", None, False)
    with pytest.raises(SkillError):
        disclose_text(skill_id="s", path="SKILL.md", body=body, content_digest_value="d" * 64, offset=1, max_bytes=4)
    with pytest.raises(SkillError):
        disclose_text(skill_id="s", path="SKILL.md", body="あ".encode(), content_digest_value="d" * 64, max_bytes=1)
    with pytest.raises(SkillError):
        disclose_text(skill_id="s", path="SKILL.md", body=b"x", content_digest_value="d" * 64, max_bytes=16 * 1024 + 1)


def test_semver_build_metadata_and_exact_prerelease_are_preserved() -> None:
    resolver = DependencyResolver({"dep": ["1.0.0+build.1", "1.0.0+build.2", "1.2.3-alpha.1"]}, lambda _n, _v: ())
    resolution = resolver.resolve([SkillDependency("dep", "=1.0.0+build.1")])
    assert resolution.pins["dep"] == "1.0.0+build.1"
    pre = resolver.resolve([SkillDependency("dep", "=1.2.3-alpha.1")])
    assert pre.pins["dep"] == "1.2.3-alpha.1"
    with pytest.raises(DependencyError):
        VersionRange.parse("^1.0.0 || ")
    with pytest.raises(DependencyError):
        VersionRange.parse("1.x.2")


def test_same_name_requires_same_stable_lineage() -> None:
    from aiagent_skills.contracts import SkillSource, SourceKind

    upload = SkillSource(kind=SourceKind.UPLOAD)
    github_a = SkillSource(kind=SourceKind.GITHUB, publisher_id="1", catalog_key="github:a/demo")
    github_b = SkillSource(kind=SourceKind.GITHUB, publisher_id="2", catalog_key="github:b/demo")
    assert_same_lineage(upload, upload, OWNER)
    with pytest.raises(SkillError):
        assert_same_lineage(github_a, github_b, OWNER)


def test_suspended_installation_can_be_reenabled(tmp_path: Path) -> None:
    reg, store, _ = registry(tmp_path)
    v1 = ingest(reg, make_zip({"SKILL.md": skill_md(version="1.0.0")}))
    installation_id = install_and_enable(reg, store, v1)
    installation = store.get_installation(TENANT, installation_id)
    disabled = reg.disable(
        tenant_id=TENANT,
        owner_user_id=OWNER,
        actor_user_id=OWNER,
        installation_id=installation_id,
        expected_revision=installation.record_version,
        request_id="suspend-1",
    )
    assert disabled.result["installation"]["effective"] == "SUSPENDED"
    suspended = store.get_installation(TENANT, installation_id)
    release = store.get_release(TENANT, v1)
    capability = capability_digest(validate_requested(release.manifest.requested_capabilities))
    approval = make_approval(
        tenant_id=TENANT,
        owner_user_id=OWNER,
        actor_user_id=OWNER,
        action="enable",
        release=release,
        capability_digest_value=capability,
        policy_revision="policy:1",
        request_id="resume-1",
        expected_installation_revision=suspended.record_version,
        installation_id=installation_id,
    )
    reg.enable(
        tenant_id=TENANT,
        owner_user_id=OWNER,
        actor_user_id=OWNER,
        installation_id=installation_id,
        approval=approval,
        capability_digest_value=capability,
        policy_revision="policy:1",
        expected_revision=suspended.record_version,
        request_id="resume-1",
    )
    assert store.get_installation(TENANT, installation_id).effective is InstallationState.ENABLED


def test_repeated_revoke_is_idempotent_and_does_not_bump_revision(tmp_path: Path) -> None:
    reg, store, _ = registry(tmp_path)
    release_id = ingest(reg, make_zip({"SKILL.md": skill_md()}))
    installation_id = install_and_enable(reg, store, release_id)
    first = reg.revoke(
        tenant_id=TENANT,
        owner_user_id=OWNER,
        actor_user_id=OWNER,
        installation_id=installation_id,
        request_id="revoke-a",
    )
    revoked = store.get_installation(TENANT, installation_id)
    second = reg.revoke(
        tenant_id=TENANT,
        owner_user_id=OWNER,
        actor_user_id=OWNER,
        installation_id=installation_id,
        request_id="revoke-b",
    )
    assert first.installation_id == second.installation_id
    assert store.get_installation(TENANT, installation_id).record_version == revoked.record_version


def test_pinned_body_survives_update(tmp_path: Path) -> None:
    reg, store, _ = registry(tmp_path)
    v1 = ingest(reg, make_zip({"SKILL.md": skill_md(version="1.0.0")}))
    installation_id = install_and_enable(reg, store, v1)
    installation = store.get_installation(TENANT, installation_id)
    release1 = store.get_release(TENANT, v1)
    capability = capability_digest(validate_requested(release1.manifest.requested_capabilities))
    pin = reg.pin_for_run(
        tenant_id=TENANT,
        run_id="run-survives-update",
        installation_id=installation_id,
        capability_digest_value=capability,
        dependency_digest_value=dependency_digest({}),
    )
    v2 = ingest(reg, make_zip({"SKILL.md": skill_md(version="1.1.0")}))
    target = store.get_release(TENANT, v2)
    target_capability = capability_digest(validate_requested(target.manifest.requested_capabilities))
    approval = make_approval(
        tenant_id=TENANT,
        owner_user_id=OWNER,
        actor_user_id=OWNER,
        action="update",
        release=target,
        capability_digest_value=target_capability,
        policy_revision="policy:1",
        request_id="update-after-pin",
        expected_installation_revision=installation.record_version,
        installation_id=installation_id,
        target_release_id=v2,
        enable_after=True,
    )
    reg.update(
        tenant_id=TENANT,
        owner_user_id=OWNER,
        actor_user_id=OWNER,
        installation_id=installation_id,
        target_release_id=v2,
        approval=approval,
        capability_digest_value=target_capability,
        policy_revision="policy:1",
        expected_revision=installation.record_version,
        enable_after=True,
        request_id="update-after-pin",
    )
    loaded = reg.load_pinned_body(
        tenant_id=TENANT,
        run_id="run-survives-update",
        installation_id=installation_id,
        max_bytes=64,
    )
    assert pin.release_id == v1
    assert loaded.content_digest == release1.content_digest


def test_pin_requires_run_authorization(tmp_path: Path) -> None:
    store = InMemorySkillStore()
    reg = SkillRegistry(store, quarantine=QuarantineStore(tmp_path / "q"), run_authorizer=lambda _t, _r, _o: False)
    release_id = ingest(reg, make_zip({"SKILL.md": skill_md()}))
    installation_id = install_and_enable(reg, store, release_id)
    release = store.get_release(TENANT, release_id)
    capability = capability_digest(validate_requested(release.manifest.requested_capabilities))
    with pytest.raises(SkillError):
        reg.pin_for_run(
            tenant_id=TENANT,
            run_id="unowned-run",
            installation_id=installation_id,
            capability_digest_value=capability,
            dependency_digest_value=dependency_digest({}),
        )


def test_github_zipball_wrapper_and_subdir_are_normalized() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("owner-repo-sha/README.md", "readme")
        archive.writestr("owner-repo-sha/skills/demo/SKILL.md", "---\nname: demo\n---")
        archive.writestr("owner-repo-sha/skills/demo/reference.md", "ref")
    rewritten = _rewrite_github_archive(buffer.getvalue(), "skills/demo")
    with zipfile.ZipFile(io.BytesIO(rewritten)) as archive:
        assert sorted(archive.namelist()) == ["SKILL.md", "reference.md"]


def test_catalog_does_not_truncate_at_fifty(tmp_path: Path) -> None:
    reg, store, _ = registry(tmp_path)
    release_id = ingest(reg, make_zip({"SKILL.md": skill_md()}))
    installation_id = install_and_enable(reg, store, release_id)
    release = store.get_release(TENANT, release_id)
    installation = store.get_installation(TENANT, installation_id)
    for index in range(51):
        name = f"skill-{index:02d}"
        store.put_release(replace(release, id=f"release-{index:02d}", manifest=replace(release.manifest, name=name)))
        store.put_installation(
            replace(installation, id=f"install-{index:02d}", skill_name=name, release_id=f"release-{index:02d}")
        )
    catalog = reg.catalog(TENANT, OWNER)
    assert len(catalog.all_entries()) == 52


def test_in_memory_atomic_rolls_back_partial_state() -> None:
    store = InMemorySkillStore()
    store.record_audit(TENANT, OWNER, "before", installation_id=None, release_id=None, detail={})
    with pytest.raises(RuntimeError):
        with store.atomic(TENANT):
            store.record_audit(TENANT, OWNER, "partial", installation_id=None, release_id=None, detail={})
            raise RuntimeError("rollback")
    assert [event["action"] for event in store._audit] == ["before"]
