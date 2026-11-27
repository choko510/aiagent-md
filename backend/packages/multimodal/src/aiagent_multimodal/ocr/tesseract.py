"""Fixed-binary Tesseract adapter.

The command line is built only from constants and a validated language tag
list. No document text, file name or environment value is interpolated into
the command. Output and wall time are capped, and the language pack version is
recorded so cached coordinates are invalidated when it changes.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from hashlib import sha256
from pathlib import Path

from .base import OcrRegion, OcrResult

_LANGS = re.compile(r"^[a-z][a-z0-9_+]{0,63}$")
_MAX_OUTPUT = 4 * 1024 * 1024


class TesseractUnavailable(RuntimeError):
    pass


class TesseractOcr:
    def __init__(self, *, binary: str = "tesseract", timeout_seconds: int = 20, max_regions: int = 20) -> None:
        self._binary = binary
        self._timeout = timeout_seconds
        self._max_regions = max_regions
        self._version: str | None = None

    def available(self) -> bool:
        return shutil.which(self._binary) is not None

    def run(self, image: bytes, *, languages: str = "jpn+eng") -> OcrResult:
        if not _LANGS.fullmatch(languages):
            raise ValueError("invalid OCR language tag")
        if not self.available():
            raise TesseractUnavailable("tesseract binary is not installed")
        started = time.monotonic()
        try:
            proc = subprocess.run(  # noqa: S603 - fixed binary and constant arguments
                [self._binary, "stdin", "stdout", "-l", languages, "--psm", "6", "tsv"],
                input=image,
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("tesseract timeout") from exc
        if time.monotonic() - started > self._timeout:
            raise TimeoutError("tesseract timeout")
        if proc.returncode != 0:
            raise TesseractUnavailable(f"tesseract exited {proc.returncode}")
        if len(proc.stdout) > _MAX_OUTPUT:
            raise TesseractUnavailable("tesseract output exceeds limit")
        regions = _parse_tsv(proc.stdout.decode("utf-8", "replace"), self._max_regions)
        text = "\n".join(r.text for r in regions if r.text)
        return OcrResult(
            text=text,
            regions=tuple(regions),
            engine="tesseract",
            engine_version=self.version(),
            languages=languages,
            language_pack_digest=self.language_pack_digest(languages),
            transform_id="ocr:raw-pixels:1",
        )

    def version(self) -> str:
        if self._version is not None:
            return self._version
        try:
            proc = subprocess.run(  # noqa: S603
                [self._binary, "--version"], capture_output=True, timeout=5, check=False
            )
            first = proc.stdout.decode("utf-8", "replace").splitlines()[0] if proc.stdout else "unknown"
        except (OSError, subprocess.TimeoutExpired):
            first = "unknown"
        self._version = first[:120]
        return self._version

    def language_pack_digest(self, languages: str) -> str:
        root = os.environ.get("TESSDATA_PREFIX")
        if not root:
            return "unverified"
        digest = sha256()
        found = False
        for lang in sorted(set(languages.split("+"))):
            path = Path(root) / f"{lang}.traineddata"
            if not path.is_file():
                return "unverified"
            found = True
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        return f"sha256:{digest.hexdigest()}" if found else "unverified"


def _parse_tsv(tsv: str, max_regions: int) -> list[OcrRegion]:
    lines = tsv.splitlines()
    if not lines:
        return []
    header = lines[0].split("\t")
    index = {name: i for i, name in enumerate(header)}
    required = {"text", "conf", "left", "top", "width", "height"}
    if not required <= set(index):
        return []
    regions: list[OcrRegion] = []
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) < len(header):
            continue
        text = parts[index["text"]].strip()
        if not text:
            continue
        try:
            confidence = float(parts[index["conf"]])
            left = int(parts[index["left"]])
            top = int(parts[index["top"]])
            width = int(parts[index["width"]])
            height = int(parts[index["height"]])
        except ValueError:
            continue
        if confidence < 0:
            continue
        regions.append(
            OcrRegion(
                text=text,
                confidence=max(0.0, min(100.0, confidence)),
                left=left,
                top=top,
                width=width,
                height=height,
                line=int(parts[index["page_num"]]) if "page_num" in index and parts[index["page_num"]].isdigit() else 0,
            )
        )
        if len(regions) >= max_regions:
            break
    return regions
