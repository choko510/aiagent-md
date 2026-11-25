"""Deterministic progressive-disclosure budget check (not a model-token measurement)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from aiagent_skills.contracts import InstallationState
from aiagent_skills.pinning import EnabledSkillCatalog, disclose_text

TENANT = "018f0000-0000-7000-8000-000000000001"


def test_catalog_surface_is_much_smaller_than_full_bodies() -> None:
    catalog = EnabledSkillCatalog()
    bodies: dict[str, bytes] = {}
    for index in range(40):
        name = f"skill-{index:02d}"
        body = (f"# {name}\n\n" + ("procedure line\n" * 16_000)).encode()
        bodies[name] = body
        manifest = SimpleNamespace(
            name=name,
            description=f"Deterministic procedure for {name}; load only when the task matches.",
            version="1.0.0",
            compatibility=SimpleNamespace(harness_api="1"),
        )
        release = SimpleNamespace(id=f"release-{index}", tenant_id=TENANT, manifest=manifest)
        installation = SimpleNamespace(
            id=f"installation-{index}",
            tenant_id=TENANT,
            release_id=release.id,
            effective=InstallationState.ENABLED,
        )
        catalog.add(installation, release)  # type: ignore[arg-type]

    surface = json.dumps([entry.to_wire() for entry in catalog.search("", limit=50)]).encode()
    full = b"".join(bodies.values())
    assert len(surface) < len(full) // 100, (len(surface), len(full))

    first = disclose_text(
        skill_id="release-0",
        path="SKILL.md",
        body=bodies["skill-00"],
        content_digest_value="a" * 64,
    )
    assert first.truncated is True
    assert len(first.text.encode()) <= 16 * 1024
    assert first.next_offset is not None


def test_disclosure_respects_utf8_boundaries() -> None:
    body = ("日本語の手順\n" * 10_000).encode()
    result = disclose_text(
        skill_id="release-ja",
        path="SKILL.md",
        body=body,
        content_digest_value="b" * 64,
        offset=0,
        max_bytes=1_000,
    )
    assert len(result.text.encode()) <= 1_000
    assert result.next_offset is not None
    assert body[: result.next_offset].decode() == result.text
