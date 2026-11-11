"""Default Japanese-writing Skill and pinned upstream bundle safety tests."""

from __future__ import annotations

import hashlib
import io
import tarfile

import pytest
from aiagent_domain.tools import SKILLS_DIR, packaged_skill_texts
from aiagent_runtime.agent import SYSTEM_PROMPT
from tools import sync_yomiyasu as sync


def _blob_sha(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\\0".encode() + data).hexdigest()


def _archive(*members: tuple[str, bytes, str]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for path, data, kind in members:
            item = tarfile.TarInfo(f"yomiyasu-{sync.UPSTREAM_COMMIT}/skills/yomiyasu/{path}")
            if kind == "symlink":
                item.type = tarfile.SYMTYPE
                item.linkname = "../outside"
            else:
                item.size = len(data)
            tar.addfile(item, io.BytesIO(data) if kind == "file" else None)
    return out.getvalue()


def test_default_ja_skill_is_packaged_and_system_prompt_selects_it() -> None:
    skill = SKILLS_DIR / "yomiyasu" / "SKILL.md"
    assert skill.is_file()
    assert "name: yomiyasu" in skill.read_text(encoding="utf-8")
    assert "yomiyasu" in "".join(packaged_skill_texts())
    assert "/skills/yomiyasu/SKILL.md" in SYSTEM_PROMPT
    assert "never report" in SYSTEM_PROMPT


def test_pinned_bundle_verifies_git_blobs_then_installs(tmp_path, monkeypatch) -> None:
    data = b"---\\nname: yomiyasu\\n---\\n"
    monkeypatch.setattr(sync, "EXPECTED_BLOBS", {"SKILL.md": _blob_sha(data)})
    files = sync.verified_files(_archive(("SKILL.md", data, "file")))
    target = tmp_path / "yomiyasu"
    sync.install(files, target)
    assert (target / "SKILL.md").read_bytes() == data


def test_pinned_bundle_rejects_modified_bytes(tmp_path, monkeypatch) -> None:
    data = b"original"
    monkeypatch.setattr(sync, "EXPECTED_BLOBS", {"SKILL.md": _blob_sha(data)})
    with pytest.raises(ValueError, match="Git blob mismatch"):
        sync.verified_files(_archive(("SKILL.md", b"modified", "file")))
    assert not (tmp_path / "yomiyasu").exists()


@pytest.mark.parametrize(
    ("relpath", "kind"),
    [("SKILL.md", "symlink"), ("../escape.txt", "file"), ("unexpected.py", "file")],
)
def test_pinned_bundle_rejects_unsafe_members(monkeypatch, relpath, kind) -> None:
    data = b"content"
    monkeypatch.setattr(sync, "EXPECTED_BLOBS", {"SKILL.md": _blob_sha(data)})
    with pytest.raises(ValueError, match="invalid skill path|unsafe upstream skill member"):
        sync.verified_files(_archive((relpath, data, kind)))


def test_pinned_bundle_rejects_missing_file(monkeypatch) -> None:
    data = b"content"
    monkeypatch.setattr(sync, "EXPECTED_BLOBS", {"SKILL.md": _blob_sha(data), "LICENSE": _blob_sha(b"x")})
    with pytest.raises(ValueError, match="incomplete upstream skill bundle"):
        sync.verified_files(_archive(("SKILL.md", data, "file")))
