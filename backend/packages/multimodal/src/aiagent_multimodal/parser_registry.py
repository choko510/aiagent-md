"""Fixed parser registry and build-digest binding (15-01 §6.1).

There is no dynamic import from file content, extension or user option. The
registry is a module-level constant, and the build digest is a hash of the
parser sources so an outcome cannot be replayed across parser revisions.
"""

from __future__ import annotations

from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Final

from .contracts import FORMATS
from .errors import MmiError
from .parsers.base import Document, ParseInput, Parser
from .parsers.csv import CsvParser
from .parsers.docx import DocxParser
from .parsers.image import ImageParser
from .parsers.pdf import PdfParser
from .parsers.pptx import PptxParser
from .parsers.xlsx import XlsxParser

_KEY: Final[dict[str, Parser]] = {
    "pdf": PdfParser(),
    "png": ImageParser(),
    "jpeg": ImageParser(),
    "webp": ImageParser(),
    "csv": CsvParser(),
    "tsv": CsvParser(),
    "xlsx": XlsxParser(),
    "docx": DocxParser(),
    "pptx": PptxParser(),
}


@lru_cache(maxsize=1)
def parser_build_digest() -> str:
    root = Path(__file__).resolve().parent
    digest = sha256()
    for path in sorted(root.glob("*.py")):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(path.read_bytes())
    return f"sha256:{digest.hexdigest()}"


def supported_formats() -> frozenset[str]:
    return frozenset(_KEY)


class ParserRegistry:
    def __init__(self) -> None:
        self._parsers = _KEY
        self.build_digest = parser_build_digest()

    def parse(self, inp: ParseInput) -> Document:
        if inp.format not in FORMATS:
            raise MmiError("UNSUPPORTED_FORMAT", "format is not allowlisted")
        parser = self._parsers.get(inp.format)
        if parser is None:
            raise MmiError("UNSUPPORTED_FORMAT", "no parser is registered for this format")
        if inp.build_digest and inp.build_digest != self.build_digest:
            # A result produced by another parser build is a different result.
            raise MmiError("SOURCE_MISMATCH", "parser build digest does not match the running build")
        try:
            return parser.parse(inp)
        except MmiError:
            raise
        except Exception as exc:
            raise MmiError("PARSER_CRASH", f"parser failed ({type(exc).__name__})") from exc


DEFAULT_REGISTRY: Final[ParserRegistry] = ParserRegistry()
