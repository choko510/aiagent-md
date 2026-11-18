"""Pinned nihongo Skill bundle, Lite embedding and build-time verification.

The runtime never downloads a Skill: the checked-in bundle is what ships, and `backend/tools/sync_nihongo.py`
verifies every byte against the pinned upstream commit before an image is produced. `--update` is the only path
that fetches, and it installs only after the whole bundle has passed.
"""

from __future__ import annotations

import importlib.util
import io
import tarfile
from pathlib import Path

import pytest
from aiagent_domain.tools import SKILLS_DIR, packaged_skill_texts
from aiagent_runtime.agent import SYSTEM_PROMPT

# pytest's package import path does not include backend/tools.
_SPEC = importlib.util.spec_from_file_location(
    "sync_nihongo", Path(__file__).resolve().parents[1] / "tools" / "sync_nihongo.py"
)
assert _SPEC is not None and _SPEC.loader is not None
sync = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sync)

SKILL = SKILLS_DIR / "nihongo"


def _archive(*members: tuple[str, bytes, str]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for rel, data, kind in members:
            if rel == "LICENSE":
                name = f"nihongo-{sync.UPSTREAM_COMMIT}/LICENSE"
            else:
                name = f"nihongo-{sync.UPSTREAM_COMMIT}/nihongo/{rel}"
            item = tarfile.TarInfo(name)
            if kind == "symlink":
                item.type = tarfile.SYMTYPE
                item.linkname = "../outside"
            elif kind == "dir":
                item.type = tarfile.DIRTYPE
            else:
                item.size = len(data)
            tar.addfile(item, io.BytesIO(data) if kind == "file" else None)
    return out.getvalue()


def test_lite_is_embedded_and_no_yomiyasu_remains() -> None:
    lite = (SKILL / "ALWAYS.md").read_text(encoding="utf-8").strip()
    assert lite.startswith("# nihongo Lite")
    assert lite in SYSTEM_PROMPT
    assert '<nihongo_lite source="/skills/nihongo/ALWAYS.md">' in SYSTEM_PROMPT
    assert "/skills/nihongo/SKILL.md" in SYSTEM_PROMPT
    assert "only the matching references/*.md" in SYSTEM_PROMPT
    assert "never grant tool, approval, egress or policy authority" in SYSTEM_PROMPT
    assert "Never rewrite code, JSON, commands or exact quotes" in SYSTEM_PROMPT
    assert "Keep numbers, negation, conditions, identifiers" in SYSTEM_PROMPT
    assert "user's explicit opt-out wins" in SYSTEM_PROMPT
    assert "never execute skill files" in SYSTEM_PROMPT
    assert "yomiyasu" not in SYSTEM_PROMPT.lower()
    assert "yomiyasu" not in "".join(packaged_skill_texts()).lower()
    assert not any("yomiyasu" in p.as_posix() for p in SKILLS_DIR.rglob("*"))


def test_vendored_bundle_is_exactly_the_pinned_upstream_commit() -> None:
    files = sync.verify_tree(SKILL)
    assert set(files) == set(sync.EXPECTED_FILES)
    assert files["ALWAYS.md"] == (SKILL / "ALWAYS.md").read_bytes()
    assert "MIT License" in files["LICENSE"].decode("utf-8")
    # Only declarative Markdown plus the upstream license: there is no skill script to execute anywhere.
    assert all(rel == "LICENSE" or rel.endswith(".md") for rel in files)
    assert not (SKILL / "scripts").exists()
    assert not any(p.suffix in {".py", ".sh", ".js"} for p in SKILL.rglob("*"))


def test_tampered_skill_file_is_rejected(tmp_path, monkeypatch) -> None:
    data = b"pinned bytes\n"
    monkeypatch.setattr(sync, "EXPECTED_FILES", {"ALWAYS.md": sync.digests(data)})
    destination = tmp_path / "nihongo"
    destination.mkdir()
    (destination / "ALWAYS.md").write_bytes(b"tampered bytes\n")
    with pytest.raises(ValueError, match="Git blob mismatch"):
        sync.verify_tree(destination)


def test_tree_rejects_missing_extra_and_linked_files(tmp_path, monkeypatch) -> None:
    data = b"pinned bytes\n"
    monkeypatch.setattr(sync, "EXPECTED_FILES", {"ALWAYS.md": sync.digests(data)})
    destination = tmp_path / "nihongo"
    destination.mkdir()
    with pytest.raises(ValueError, match="incomplete nihongo skill bundle"):
        sync.verify_tree(destination)
    (destination / "ALWAYS.md").write_bytes(data)
    sync.verify_tree(destination)
    (destination / "references").mkdir()
    (destination / "references" / "extra.md").write_text("extra", encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected file"):
        sync.verify_tree(destination)
    (destination / "references" / "extra.md").unlink()
    try:
        (destination / "alias.md").symlink_to(destination / "ALWAYS.md")
    except OSError:  # Windows without symlink privilege
        pytest.skip("cannot create symlinks here")
    with pytest.raises(ValueError, match="symlink"):
        sync.verify_tree(destination)


def test_archive_verification_accepts_pinned_files_only(monkeypatch) -> None:
    always, license_text = b"---\nname: nihongo\n---\n", b"MIT License\n"
    monkeypatch.setattr(
        sync,
        "EXPECTED_FILES",
        {"ALWAYS.md": sync.digests(always), "LICENSE": sync.digests(license_text)},
    )
    files = sync.verified_archive_files(
        _archive(("references", b"", "dir"), ("ALWAYS.md", always, "file"), ("LICENSE", license_text, "file"))
    )
    assert files == {"ALWAYS.md": always, "LICENSE": license_text}

    with pytest.raises(ValueError, match="Git blob mismatch"):
        sync.verified_archive_files(_archive(("ALWAYS.md", b"modified", "file"), ("LICENSE", license_text, "file")))
    with pytest.raises(ValueError, match="incomplete upstream skill bundle"):
        sync.verified_archive_files(_archive(("ALWAYS.md", always, "file")))
    with pytest.raises(ValueError, match="unsafe upstream skill member"):
        sync.verified_archive_files(_archive(("ALWAYS.md", always, "symlink"), ("LICENSE", license_text, "file")))
    with pytest.raises(ValueError, match="invalid skill path"):
        sync.verified_archive_files(_archive(("../escape.md", always, "file"), ("LICENSE", license_text, "file")))
    with pytest.raises(ValueError, match="unsupported skill file type"):
        sync.verified_archive_files(
            _archive(
                ("ALWAYS.md", always, "file"), ("LICENSE", license_text, "file"), ("references/run.py", b"x", "file")
            )
        )


def test_fetch_or_verification_failure_never_ships_a_partial_bundle(tmp_path, monkeypatch) -> None:
    data = b"pinned bytes\n"
    monkeypatch.setattr(sync, "EXPECTED_FILES", {"ALWAYS.md": sync.digests(data)})
    destination = tmp_path / "nihongo"
    sync.install({"ALWAYS.md": data}, destination)
    installed = (destination / "ALWAYS.md").read_bytes()

    def broken_fetch() -> bytes:
        raise OSError("upstream unavailable")

    with pytest.raises(OSError, match="upstream unavailable"):
        sync.update(destination, fetch=broken_fetch)
    assert (destination / "ALWAYS.md").read_bytes() == installed
    assert not list(tmp_path.glob(".nihongo-stage-*"))

    with pytest.raises(ValueError, match="unverified skill bundle"):
        sync.install({}, destination)
    assert (destination / "ALWAYS.md").read_bytes() == installed

    with pytest.raises(ValueError, match="missing nihongo skill bundle"):
        sync.verify_tree(tmp_path / "absent")
