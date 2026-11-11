"""Fetch a pinned, verified upstream yomiyasu skill during image build (never at agent runtime).

No upstream Python code is executed. Each archived file is checked against its Git
blob SHA-1 from the pinned commit. The runtime exposes the result as read-only data.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath

UPSTREAM_COMMIT = "7b212ff2f037953b8c233ae315e5c08e7fc36da6"
UPSTREAM_URL = f"https://codeload.github.com/nanaism/yomiyasu/tar.gz/{UPSTREAM_COMMIT}"
MAX_ARCHIVE_BYTES = 12 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024

# The exact upstream blobs from this commit. Do not silently accept an updated "main".
EXPECTED_BLOBS: dict[str, str] = {
    ".codex-plugin/plugin.json": "5a19ff8aea13384325c12c2f8bd6c4720e88fb3e",
    "LICENSE": "f0d06f87f9b209d47c7f7963321ff71bab68c1e8",
    "SKILL.md": "a1cc491efb3e9185b5609be92003a7f87a63cf8a",
    "UNICODE-LICENSE.txt": "56da58912807030b451a4700687d5bd02c72d6ed",
    "assets/algo-artis.png": "21f28805968be9191621b00d64efe6b5f70c1ec6",
    "references/audience.md": "35513eb1192536e004e3e9f0412338fadb10735b",
    "references/calques.md": "db09300e85e20153b0c6a70d8ea10314b7fb46be",
    "references/doc-types/article.md": "5555ad6fac4139c443c30bcc71bd31b90b126787",
    "references/doc-types/essay.md": "7ab294be43477de5208f9f097efa4e5d689f5c63",
    "references/doc-types/formal.md": "38b2060b773b5a1c24e4e37926afd4c0ae5ccf76",
    "references/doc-types/incident.md": "287165f83caa12e08065a187d9c5754a91aee3c5",
    "references/doc-types/mail.md": "c468ae8cb53f182d65d301d6899384e30cc3172b",
    "references/doc-types/manual.md": "9e4ffe28d9f7066667971eecd6cc60a628ae79ce",
    "references/doc-types/minutes.md": "0e21a3edb43c7b3ecc1c613d1a6294322570e093",
    "references/doc-types/paper.md": "f1a42903983816d3fca513a0345d70ae2179f275",
    "references/doc-types/pr.md": "a49dccda870f97f8e2bbf3b437c1b5910f8fe00b",
    "references/doc-types/promo.md": "8c39d48e968faa7aa134f2d865c6258fff77badb",
    "references/doc-types/proposal.md": "cc03100c37ec983afc3cdc69cb48223a39c28fe2",
    "references/doc-types/script.md": "7d1f4b1de06a4670ff290f80e311df17fb9180bb",
    "references/doc-types/slides.md": "aa6f525994282cca0349cb72148a7970ddce8d3b",
    "references/domains/business.md": "5729c08fa97194506feb89ccf6744bf6ac427457",
    "references/domains/essay.md": "4d0bf1575d77ce4b7e9e7e73eeb3bc7bad7225d4",
    "references/domains/tech.md": "f804f769a608ac3ccb80a77b01dad101e317b87b",
    "references/fact-recovery-details.md": "f7f93668528e37ad567be613964f0fa4513a56f0",
    "references/fact-recovery.md": "54f4c20e28afc8dad38132fae1eb6aa50bcada04",
    "references/gemini-syntax.md": "178ee5270750518c8bd38831368ca7f89925a603",
    "references/lists/fact-gaps.txt": "2a178e536400ac3a8d0e356a5f73a40ae00a51fd",
    "references/lists/feelings.txt": "c6066da2f3d0eb0838ff92a329a5a26a1664cb7b",
    "references/lists/hedges.txt": "2a20cefc2886ce03116eef8743d84b62523e911e",
    "references/lists/intensifiers.txt": "6433f1e4334133714ec7569c43d7fefca2dbcaa2",
    "references/lists/katakana-terms.txt": "1db55310330d55604d40caccdaa4765193b550a2",
    "references/lists/nominalization.txt": "68ea6918ea1e09efa6a4baad7918a492d937dd29",
    "references/lists/renyo-nouns.txt": "158c9f7eee9f3e768228cf6fbd97e7e9a93c2802",
    "references/lists/thin-evaluation.txt": "a89b3600c90dcb787ce84d61fee5a8aaf82d0767",
    "references/persona.md": "6c23b65e2f31fd978837fad7a47060eab4cff544",
    "references/rephrase-test.md": "f31b995b9ba7b844a4a2414d5ce45ee68ccdd567",
    "references/review-points.md": "03e138b0fe490413794b3f319cc306e2408bff6c",
    "references/slop-catalog.md": "75fb321c872814247607a3be73fac89dd6c4d706",
    "scripts/markdown_visibility.py": "82f8282b8cc4187deaeedc1daad8eef6df53f0a9",
    "scripts/yomiyasu_check.py": "b57255eefafcdfc5fec871dbb06026f4725707fa",
    "scripts/yomiyasu_diff.py": "0ca497d7f9c18f655b8e1f41e39980d8551207b8",
    "scripts/yomiyasu_hook.py": "3011306bca55624905b5c9cd342e2b8abf521933",
    "scripts/yomiyasu_lint.py": "9670465b81281e1172a8141eabbfcb228363c755"
}


def download_archive() -> bytes:
    req = urllib.request.Request(UPSTREAM_URL, headers={"User-Agent": "aiagent-md-build/1"})
    with urllib.request.urlopen(req, timeout=60) as response:  # noqa: S310 - fixed GitHub URL
        payload = response.read(MAX_ARCHIVE_BYTES + 1)
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise ValueError("yomiyasu archive exceeds size limit")
    return payload


def verified_files(archive: bytes) -> dict[str, bytes]:
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise ValueError("yomiyasu archive exceeds size limit")
    files: dict[str, bytes] = {}
    total_bytes = 0
    prefix = f"yomiyasu-{UPSTREAM_COMMIT}/skills/yomiyasu/"
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar:
            # Do not extract or execute anything outside the requested skill tree.
            if not member.name.startswith(prefix):
                continue
            rel = member.name[len(prefix):]
            parts = PurePosixPath(rel).parts
            if not rel or rel.startswith("/") or ".." in parts or "\\ " in rel or "\\x00" in rel:
                raise ValueError(f"invalid skill path: {rel!r}")
            if member.isdir():
                continue
            if not member.isfile() or rel not in EXPECTED_BLOBS or rel in files:
                raise ValueError(f"unexpected or unsafe upstream skill member: {rel}")
            if member.size > MAX_FILE_BYTES or member.size < 0:
                raise ValueError(f"oversized upstream file: {rel}")
            stream = tar.extractfile(member)
            if stream is None:
                raise ValueError(f"missing upstream file data: {rel}")
            data = stream.read(MAX_FILE_BYTES + 1)
            if len(data) != member.size:
                raise ValueError(f"invalid upstream file length: {rel}")
            digest = hashlib.sha1(f"blob {len(data)}\\0".encode() + data).hexdigest()  # noqa: S324 - Git object digest
            if digest != EXPECTED_BLOBS[rel]:
                raise ValueError(f"upstream Git blob mismatch: {rel}")
            total_bytes += len(data)
            if total_bytes > MAX_TOTAL_BYTES:
                raise ValueError("unpacked yomiyasu exceeds size limit")
            files[rel] = data
    if set(files) != set(EXPECTED_BLOBS):
        missing = sorted(set(EXPECTED_BLOBS) - set(files))
        raise ValueError(f"incomplete upstream skill bundle: {missing}")
    return files


def install(files: dict[str, bytes], destination: Path) -> None:
    """Stage fully validated files first; do not change an existing skill on validation failure."""
    if set(files) != set(EXPECTED_BLOBS):
        raise ValueError("unverified skill bundle")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".yomiyasu-stage-", dir=destination.parent))
    try:
        for rel, data in files.items():
            path = staging.joinpath(*PurePosixPath(rel).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        if destination.is_symlink():
            raise ValueError("refusing to replace a symlink")
        if destination.exists():
            shutil.rmtree(destination)
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main() -> None:
    parser = argparse.ArgumentParser(description="Install pinned upstream yomiyasu into the built-in skill catalog")
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "packages/domain/src/aiagent_domain/skills/yomiyasu",
    )
    args = parser.parse_args()
    files = verified_files(download_archive())
    install(files, args.destination)
    print(f"Verified and installed upstream yomiyasu {UPSTREAM_COMMIT} ({len(files)} files)")


if __name__ == "__main__":
    main()
