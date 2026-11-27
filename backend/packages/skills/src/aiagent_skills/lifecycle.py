"""Explicit, auditable release/installation state machines.

Install never enables. Enable requires its own exact-release approval. Update, rollback and removal are separate
operations with optimistic concurrency; a failed update leaves the previous active release untouched.
"""

from __future__ import annotations

from dataclasses import replace

from .contracts import DesiredState, InstallationState, ReleaseState, SkillInstallation, SkillRelease
from .errors import LifecycleConflict, ReleaseRevoked, StaleRevision

_RELEASE_EDGES: dict[ReleaseState, frozenset[ReleaseState]] = {
    ReleaseState.DISCOVERED: frozenset({ReleaseState.DOWNLOADING, ReleaseState.REJECTED}),
    ReleaseState.DOWNLOADING: frozenset({ReleaseState.QUARANTINED, ReleaseState.REJECTED}),
    ReleaseState.QUARANTINED: frozenset({ReleaseState.VALIDATING, ReleaseState.REJECTED}),
    ReleaseState.VALIDATING: frozenset({ReleaseState.VALIDATED, ReleaseState.REJECTED}),
    ReleaseState.VALIDATED: frozenset({ReleaseState.STAGED_FOR_REVIEW, ReleaseState.REJECTED}),
    ReleaseState.STAGED_FOR_REVIEW: frozenset({ReleaseState.RETIRED, ReleaseState.REVOKED, ReleaseState.REJECTED}),
    ReleaseState.REJECTED: frozenset({ReleaseState.REVOKED}),
    ReleaseState.RETIRED: frozenset({ReleaseState.REVOKED}),
    ReleaseState.REVOKED: frozenset(),
}

_INSTALLATION_EDGES: dict[InstallationState, frozenset[InstallationState]] = {
    InstallationState.NOT_INSTALLED: frozenset({InstallationState.INSTALLING}),
    InstallationState.INSTALLING: frozenset(
        {InstallationState.INSTALLED_DISABLED, InstallationState.REMOVED, InstallationState.REVOKED}
    ),
    InstallationState.INSTALLED_DISABLED: frozenset(
        {InstallationState.ENABLING, InstallationState.REMOVING, InstallationState.REVOKED}
    ),
    InstallationState.ENABLING: frozenset(
        {InstallationState.ENABLED, InstallationState.INSTALLED_DISABLED, InstallationState.REVOKED}
    ),
    InstallationState.ENABLED: frozenset(
        {
            InstallationState.SUSPENDED,
            InstallationState.UPDATING,
            InstallationState.ROLLING_BACK,
            InstallationState.REMOVING,
            InstallationState.REVOKED,
        }
    ),
    InstallationState.SUSPENDED: frozenset(
        {InstallationState.ENABLING, InstallationState.ENABLED, InstallationState.REMOVING, InstallationState.REVOKED}
    ),
    InstallationState.UPDATING: frozenset(
        {
            InstallationState.ENABLED,
            InstallationState.INSTALLED_DISABLED,
            InstallationState.SUSPENDED,
            InstallationState.REVOKED,
        }
    ),
    InstallationState.ROLLING_BACK: frozenset(
        {
            InstallationState.ENABLED,
            InstallationState.INSTALLED_DISABLED,
            InstallationState.SUSPENDED,
            InstallationState.REVOKED,
        }
    ),
    InstallationState.REMOVING: frozenset({InstallationState.REMOVED, InstallationState.REVOKED}),
    InstallationState.REMOVED: frozenset(),
    InstallationState.REVOKED: frozenset(),
}


def assert_release_transition(current: ReleaseState, target: ReleaseState) -> None:
    if target not in _RELEASE_EDGES[current]:
        raise LifecycleConflict(f"release cannot move from {current.value} to {target.value}")


def assert_installation_transition(current: InstallationState, target: InstallationState) -> None:
    if target not in _INSTALLATION_EDGES[current]:
        raise LifecycleConflict(f"installation cannot move from {current.value} to {target.value}")


def assert_expected_revision(installation: SkillInstallation, expected: int) -> None:
    if installation.record_version != expected:
        raise StaleRevision(
            "installation changed since it was read", expected=expected, actual=installation.record_version
        )


def assert_not_revoked(release: SkillRelease, installation: SkillInstallation | None = None) -> None:
    if release.state is ReleaseState.REVOKED:
        raise ReleaseRevoked("release is revoked")
    if installation is not None and installation.effective is InstallationState.REVOKED:
        raise ReleaseRevoked("installation is revoked")


def new_installation(
    *,
    installation_id: str,
    tenant_id: str,
    owner_user_id: str,
    release: SkillRelease,
    policy_revision: str,
    now: str,
) -> SkillInstallation:
    if release.tenant_id not in (None, tenant_id):
        raise LifecycleConflict("release belongs to another tenant")
    return SkillInstallation(
        id=installation_id,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        skill_name=release.manifest.name,
        release_id=release.id,
        desired=DesiredState.DISABLED,
        effective=InstallationState.INSTALLING,
        record_version=1,
        activation_generation=0,
        policy_revision=policy_revision,
        approved_release_digest=release.content_digest,
        created_at=now,
        updated_at=now,
    )


# begin/finish pairs are persisted once, at finish, so they share one record_version bump.
def finish_install(installation: SkillInstallation, *, now: str) -> SkillInstallation:
    assert_installation_transition(installation.effective, InstallationState.INSTALLED_DISABLED)
    return replace(
        installation,
        effective=InstallationState.INSTALLED_DISABLED,
        updated_at=now,
    )


def begin_enable(
    installation: SkillInstallation,
    release: SkillRelease,
    *,
    expected_revision: int,
    approved_release_digest: str,
    policy_revision: str,
    now: str,
) -> SkillInstallation:
    assert_expected_revision(installation, expected_revision)
    assert_not_revoked(release, installation)
    if installation.release_id != release.id or approved_release_digest != release.content_digest:
        raise LifecycleConflict("enable approval does not bind the installed release")
    assert_installation_transition(installation.effective, InstallationState.ENABLING)
    return replace(
        installation,
        desired=DesiredState.ENABLED,
        effective=InstallationState.ENABLING,
        approved_release_digest=approved_release_digest,
        policy_revision=policy_revision,
        record_version=installation.record_version + 1,
        updated_at=now,
    )


def finish_enable(installation: SkillInstallation, *, now: str) -> SkillInstallation:
    assert_installation_transition(installation.effective, InstallationState.ENABLED)
    return replace(
        installation,
        desired=DesiredState.ENABLED,
        effective=InstallationState.ENABLED,
        activation_generation=installation.activation_generation + 1,
        updated_at=now,
    )


def begin_disable(
    installation: SkillInstallation,
    *,
    expected_revision: int,
    now: str,
) -> SkillInstallation:
    assert_expected_revision(installation, expected_revision)
    if installation.effective is not InstallationState.ENABLED:
        raise LifecycleConflict("only an enabled installation can be disabled")
    return replace(
        installation,
        desired=DesiredState.DISABLED,
        effective=InstallationState.SUSPENDED,
        record_version=installation.record_version + 1,
        revocation_epoch=installation.revocation_epoch + 1,
        updated_at=now,
    )


def begin_update(
    installation: SkillInstallation,
    current: SkillRelease,
    target: SkillRelease,
    *,
    expected_revision: int,
    now: str,
) -> SkillInstallation:
    assert_expected_revision(installation, expected_revision)
    assert_not_revoked(target, installation)
    if target.manifest.name != installation.skill_name or target.manifest.name != current.manifest.name:
        raise LifecycleConflict("update must keep the same Skill identity")
    if target.content_digest == current.content_digest:
        raise LifecycleConflict("target release is identical to the current release")
    if installation.effective not in (
        InstallationState.ENABLED,
        InstallationState.INSTALLED_DISABLED,
        InstallationState.SUSPENDED,
    ):
        raise LifecycleConflict("installation is not in a state that can be updated")
    return replace(
        installation,
        effective=InstallationState.UPDATING,
        record_version=installation.record_version + 1,
        updated_at=now,
    )


def finish_update(
    installation: SkillInstallation,
    target: SkillRelease,
    *,
    enable: bool,
    approved_release_digest: str,
    policy_revision: str,
    now: str,
    suspended: bool = False,
) -> SkillInstallation:
    if approved_release_digest != target.content_digest:
        raise LifecycleConflict("update approval does not bind the target release")
    if enable:
        effective = InstallationState.ENABLED
    elif suspended:
        effective = InstallationState.SUSPENDED
    else:
        effective = InstallationState.INSTALLED_DISABLED
    assert_installation_transition(installation.effective, effective)
    return replace(
        installation,
        release_id=target.id,
        desired=DesiredState.ENABLED if enable else DesiredState.DISABLED,
        effective=effective,
        activation_generation=installation.activation_generation + (1 if enable else 0),
        policy_revision=policy_revision,
        approved_release_digest=target.content_digest,
        updated_at=now,
    )


def abort_update(
    installation: SkillInstallation,
    *,
    now: str,
    restore: InstallationState = InstallationState.INSTALLED_DISABLED,
) -> SkillInstallation:
    """Restore the pre-update activation state; a failed update must not silently disable a live Skill."""
    if restore not in (
        InstallationState.ENABLED,
        InstallationState.INSTALLED_DISABLED,
        InstallationState.SUSPENDED,
    ):
        raise LifecycleConflict("failed update can only restore enabled, suspended or disabled state")
    assert_installation_transition(installation.effective, restore)
    return replace(
        installation,
        desired=DesiredState.ENABLED if restore is InstallationState.ENABLED else DesiredState.DISABLED,
        effective=restore,
        updated_at=now,
    )


def begin_rollback(
    installation: SkillInstallation,
    target: SkillRelease,
    *,
    expected_revision: int,
    now: str,
) -> SkillInstallation:
    assert_expected_revision(installation, expected_revision)
    assert_not_revoked(target, installation)
    if target.manifest.name != installation.skill_name:
        raise LifecycleConflict("rollback target has a different Skill identity")
    if target.id == installation.release_id:
        raise LifecycleConflict("rollback target is already the current release")
    if installation.effective not in (
        InstallationState.ENABLED,
        InstallationState.INSTALLED_DISABLED,
        InstallationState.SUSPENDED,
    ):
        raise LifecycleConflict("installation is not in a state that can be rolled back")
    return replace(
        installation,
        effective=InstallationState.ROLLING_BACK,
        record_version=installation.record_version + 1,
        updated_at=now,
    )


def finish_rollback(
    installation: SkillInstallation,
    target: SkillRelease,
    *,
    reenable: bool,
    policy_revision: str,
    now: str,
    suspended: bool = False,
) -> SkillInstallation:
    if reenable:
        effective = InstallationState.ENABLED
    elif suspended:
        effective = InstallationState.SUSPENDED
    else:
        effective = InstallationState.INSTALLED_DISABLED
    assert_installation_transition(installation.effective, effective)
    return replace(
        installation,
        release_id=target.id,
        desired=DesiredState.ENABLED if reenable else DesiredState.DISABLED,
        effective=effective,
        activation_generation=installation.activation_generation + (1 if reenable else 0),
        policy_revision=policy_revision,
        approved_release_digest=target.content_digest,
        updated_at=now,
    )


def begin_remove(installation: SkillInstallation, *, expected_revision: int, now: str) -> SkillInstallation:
    assert_expected_revision(installation, expected_revision)
    if installation.effective in (InstallationState.REMOVED, InstallationState.REVOKED):
        raise LifecycleConflict("removed or revoked installations cannot be removed")
    assert_installation_transition(installation.effective, InstallationState.REMOVING)
    return replace(
        installation,
        desired=DesiredState.DISABLED,
        effective=InstallationState.REMOVING,
        record_version=installation.record_version + 1,
        revocation_epoch=installation.revocation_epoch + 1,
        updated_at=now,
    )


def finish_remove(installation: SkillInstallation, *, now: str) -> SkillInstallation:
    assert_installation_transition(installation.effective, InstallationState.REMOVED)
    return replace(
        installation,
        desired=DesiredState.DISABLED,
        effective=InstallationState.REMOVED,
        updated_at=now,
    )


def revoke_installation(installation: SkillInstallation, *, now: str) -> SkillInstallation:
    if installation.effective is InstallationState.REVOKED:
        return installation
    return replace(
        installation,
        desired=DesiredState.DISABLED,
        effective=InstallationState.REVOKED,
        revoked_at=now,
        record_version=installation.record_version + 1,
        revocation_epoch=installation.revocation_epoch + 1,
        updated_at=now,
    )


def assert_update_allowed(installation: SkillInstallation, current: SkillRelease, target: SkillRelease) -> None:
    if installation.release_id != current.id:
        raise LifecycleConflict("installation no longer points at the release being updated")
    assert_not_revoked(current, installation)
    assert_not_revoked(target, installation)


def assert_rollback_history(history: tuple[str, ...], target_release_id: str) -> None:
    if target_release_id not in history:
        raise LifecycleConflict("rollback target is not in this installation history")
