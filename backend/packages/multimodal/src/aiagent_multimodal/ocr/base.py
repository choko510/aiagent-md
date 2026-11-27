"""Local OCR port. Recognition confidence is not a truth probability."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class OcrRegion:
    text: str
    confidence: float
    left: int
    top: int
    width: int
    height: int
    line: int = 0
    page: int = 0


@dataclass(frozen=True, slots=True)
class OcrResult:
    text: str
    regions: tuple[OcrRegion, ...]
    engine: str
    engine_version: str
    languages: str
    language_pack_digest: str
    transform_id: str


class OcrPort(Protocol):
    def available(self) -> bool: ...

    def run(self, image: bytes, *, languages: str = "jpn+eng") -> OcrResult: ...
