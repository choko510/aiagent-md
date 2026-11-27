"""Format detection across extension, declared MIME, magic and container (15-01 §5.3).

Detection never trusts the client's declared type. A file whose bytes are
ambiguous or polyglot is refused rather than guessed, and the decision records
the evidence that produced it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from .archive_gate import entry_names, read_entry
from .errors import MmiError
from .limits import MmiLimits
from .xml_gate import parse_xml

_GENERIC_MIME: Final[frozenset[str]] = frozenset(
    {"", "application/octet-stream", "binary/octet-stream", "application/unknown", "unknown"}
)
_EXTENSION: Final[dict[str, str]] = {
    ".pdf": "pdf",
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".webp": "webp",
    ".csv": "csv",
    ".tsv": "tsv",
    ".xlsx": "xlsx",
    ".docx": "docx",
    ".pptx": "pptx",
}
_ACTIVE_EXTENSION: Final[dict[str, str]] = {
    ".xlsm": "UNSUPPORTED_ACTIVE_CONTENT",
    ".xlsb": "UNSUPPORTED_ACTIVE_CONTENT",
    ".docm": "UNSUPPORTED_ACTIVE_CONTENT",
    ".pptm": "UNSUPPORTED_ACTIVE_CONTENT",
    ".xls": "UNSUPPORTED_FORMAT",
    ".doc": "UNSUPPORTED_FORMAT",
    ".ppt": "UNSUPPORTED_FORMAT",
    ".xltx": "UNSUPPORTED_FORMAT",
    ".dotx": "UNSUPPORTED_FORMAT",
}
_MIME: Final[dict[str, frozenset[str]]] = {
    "pdf": frozenset({"application/pdf"}),
    "png": frozenset({"image/png"}),
    "jpeg": frozenset({"image/jpeg"}),
    "webp": frozenset({"image/webp"}),
    "csv": frozenset({"text/csv", "application/csv", "text/plain"}),
    "tsv": frozenset({"text/tab-separated-values", "text/plain"}),
    "xlsx": frozenset({"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/zip"}),
    "docx": frozenset({"application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/zip"}),
    "pptx": frozenset({"application/vnd.openxmlformats-officedocument.presentationml.presentation", "application/zip"}),
}
_OOXML_MAIN: Final[dict[str, str]] = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml": "xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml": "pptx",
}
_REQUIRED_PART: Final[dict[str, str]] = {
    "docx": "word/document.xml",
    "xlsx": "xl/workbook.xml",
    "pptx": "ppt/presentation.xml",
}


@dataclass(frozen=True, slots=True)
class DeclaredInput:
    file_name: str = ""
    declared_mime: str = ""
    format_hint: str | None = None
    encoding_hint: str | None = None

    def extension(self) -> str:
        name = self.file_name.replace("\\", "/").rsplit("/", 1)[-1]
        dot = name.rfind(".")
        return name[dot:].lower() if dot > 0 else ""

    def normalized_mime(self) -> str:
        return self.declared_mime.split(";", 1)[0].strip().lower()


@dataclass(frozen=True, slots=True)
class Detection:
    format: str
    mime: str
    encoding: str | None = None
    delimiter: str | None = None
    evidence: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_text(self) -> bool:
        return self.format in {"csv", "tsv"}


def detect(data: bytes, declared: DeclaredInput, limits: MmiLimits) -> Detection:
    if not data:
        raise MmiError("MAX_INPUT", "empty file")
    extension = declared.extension()
    if extension in _ACTIVE_EXTENSION:
        raise MmiError(_ACTIVE_EXTENSION[extension], "macro-enabled or legacy Office format is not parsed")
    hint = (declared.format_hint or "").strip().lower() or None
    if hint is not None and hint not in {"pdf", "png", "jpeg", "webp", "csv", "tsv", "xlsx", "docx", "pptx"}:
        raise MmiError("UNSUPPORTED_FORMAT", "unsupported format hint")

    magic, zip_names = _sniff(data, limits)
    evidence: list[str] = [f"magic={magic}"]
    if magic == "zip":
        actual = _classify_ooxml(data, zip_names, limits)
        evidence.append(f"ooxml={actual}")
    elif magic in {"pdf", "png", "jpeg", "webp"}:
        actual = magic
    else:
        actual, text_meta = _classify_text(data, declared)
        evidence.extend(text_meta)
        _check_text_claims(actual, declared, extension, hint)
        mime = _mime_for(actual)
        return Detection(
            format=actual,
            mime=mime,
            encoding=_encoding_for(data, text_meta, declared),
            delimiter="\t" if actual == "tsv" else ",",
            evidence=tuple(evidence),
        )

    if magic == "pdf" and (b"PK\x03\x04" in data[:1024] or (b"%PDF-" in data[:8] and b"\x89PNG" in data[:1024])):
        raise MmiError("AMBIGUOUS_FORMAT", "polyglot container")

    _check_binary_claims(actual, declared, extension, hint, evidence)
    return Detection(format=actual, mime=_mime_for(actual), evidence=tuple(evidence))


def _sniff(data: bytes, limits: MmiLimits) -> tuple[str, tuple[str, ...]]:
    head = data[:4096]
    if head.startswith(b"%PDF-"):
        return "pdf", ()
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", ()
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg", ()
    if head.startswith(b"RIFF") and len(head) >= 12 and head[8:12] == b"WEBP":
        return "webp", ()
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "zip", entry_names(data, limits)
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        raise MmiError("UNSUPPORTED_FORMAT", "legacy OLE compound document is not parsed")
    return "text", ()


def _classify_ooxml(data: bytes, names: tuple[str, ...], limits: MmiLimits) -> str:
    if any(name.endswith("vbaProject.bin") for name in names):
        raise MmiError("UNSUPPORTED_ACTIVE_CONTENT", "macro project inside OOXML container")
    if "[Content_Types].xml" not in names:
        raise MmiError("AMBIGUOUS_FORMAT", "ZIP container is not OOXML")
    content_types = read_entry(data, "[Content_Types].xml", limits, max_bytes=1_048_576)
    root = parse_xml(content_types, limits, root_expected="Types")
    main: str | None = None
    for node in root.children_named("Override"):
        part = node.attrs.get("PartName", "")
        kind = node.attrs.get("ContentType", "")
        if part.endswith(".bin") and "macroEnabled" in kind:
            raise MmiError("UNSUPPORTED_ACTIVE_CONTENT", "macro-enabled OOXML content type")
        if kind in _OOXML_MAIN:
            main = _OOXML_MAIN[kind]
        elif "openxmlformats-officedocument" in kind and kind.endswith(".main+xml"):
            raise MmiError("UNSUPPORTED_FORMAT", "unsupported OOXML document type")
        elif kind in {
            "application/vnd.ms-excel.sheet.binary.macroEnabled.main",
            "application/vnd.ms-word.document.macroEnabled.main",
        }:
            raise MmiError("UNSUPPORTED_ACTIVE_CONTENT", "binary or macro-enabled OOXML content type")
    if main is None:
        raise MmiError("AMBIGUOUS_FORMAT", "OOXML container has no supported main part")
    required = _REQUIRED_PART[main]
    if required not in names:
        raise MmiError("AMBIGUOUS_FORMAT", f"OOXML container is missing {required}")
    return main


def _classify_text(data: bytes, declared: DeclaredInput) -> tuple[str, list[str]]:
    sample = data[: 128 * 1024]
    if b"\x00" in sample:
        raise MmiError("UNSUPPORTED_FORMAT", "binary content is not an allowed document format")
    encoding = _probe_encoding(sample, declared.encoding_hint)
    evidence = [f"encoding={encoding}"]
    try:
        text = sample.decode(encoding, "strict")
    except UnicodeDecodeError as exc:
        raise MmiError("INVALID_ENCODING", "text sample is not valid in the declared encoding") from exc
    bad = sum(1 for c in text if ord(c) < 0x20 and c not in "\t\r\n")
    if bad > max(2, len(text) // 200):
        raise MmiError("UNSUPPORTED_FORMAT", "binary/control content is not a text document")
    lines = [line for line in text.splitlines() if line.strip()][:50]
    comma = _consistency(lines, ",")
    tab = _consistency(lines, "\t")
    evidence.append(f"comma={comma:.2f}")
    evidence.append(f"tab={tab:.2f}")
    hint = (declared.format_hint or "").lower() or None
    ext = declared.extension()
    if tab >= 0.8 and tab > comma:
        return "tsv", evidence
    if comma >= 0.8:
        return "csv", evidence
    if comma > 0 or tab > 0:
        raise MmiError("AMBIGUOUS_FORMAT", "inconsistent text delimiter")
    if hint in {"csv", "tsv"}:
        return hint, evidence
    if ext in {".csv", ".tsv"}:
        return ("tsv" if ext == ".tsv" else "csv"), evidence
    mime = declared.normalized_mime()
    if mime in {"text/csv", "application/csv"}:
        return "csv", evidence
    if mime == "text/tab-separated-values":
        return "tsv", evidence
    raise MmiError("UNSUPPORTED_FORMAT", "plain text is not an allowed document format")


def _consistency(lines: list[str], delimiter: str) -> float:
    if not lines:
        return 0.0
    counts = [line.count(delimiter) for line in lines]
    positive = [c for c in counts if c > 0]
    if not positive:
        return 0.0
    modal = max(set(positive), key=positive.count)
    return sum(1 for c in positive if c == modal) / len(lines)


def _probe_encoding(sample: bytes, hint: str | None) -> str:
    if sample.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if hint:
        normalized = hint.lower().replace("-", "").replace("_", "")
        if normalized in {"utf8", "utf8sig"}:
            return "utf-8-sig" if normalized == "utf8sig" else "utf-8"
        if normalized in {"cp932", "shiftjis", "sjis", "windows31j"}:
            return "cp932"
        raise MmiError("INVALID_ENCODING", "unsupported explicit encoding")
    try:
        sample.decode("utf-8", "strict")
        return "utf-8"
    except UnicodeDecodeError as exc:
        raise MmiError("INVALID_ENCODING", "text is not UTF-8; an explicit encoding is required") from exc


def _encoding_for(data: bytes, evidence: list[str], declared: DeclaredInput) -> str:
    return _probe_encoding(data[: 128 * 1024], declared.encoding_hint)


def _check_text_claims(actual: str, declared: DeclaredInput, extension: str, hint: str | None) -> None:
    if hint is not None and hint not in {actual, "csv", "tsv"}:
        raise MmiError("MIME_MISMATCH", "explicit format hint disagrees with content")
    if extension and extension in _EXTENSION:
        declared_format = _EXTENSION[extension]
        if {declared_format, actual} != {"csv", "tsv"} and declared_format != actual:
            raise MmiError("MIME_MISMATCH", "file extension disagrees with content")
    mime = declared.normalized_mime()
    if mime and mime not in _GENERIC_MIME and mime not in _MIME[actual] and {actual, "csv"} != {"csv", "tsv"}:
        raise MmiError("MIME_MISMATCH", "declared MIME disagrees with content")


def _check_binary_claims(
    actual: str, declared: DeclaredInput, extension: str, hint: str | None, evidence: list[str]
) -> None:
    if hint is not None and hint != actual:
        raise MmiError("MIME_MISMATCH", "explicit format hint disagrees with content")
    if extension and extension in _EXTENSION:
        declared_format = _EXTENSION[extension]
        if declared_format != actual:
            raise MmiError("MIME_MISMATCH", "file extension disagrees with content")
    mime = declared.normalized_mime()
    allowed = _MIME[actual]
    if mime and mime not in _GENERIC_MIME and mime not in allowed:
        raise MmiError("MIME_MISMATCH", "declared MIME disagrees with content")
    if mime in _GENERIC_MIME:
        evidence.append("mime=unverified")


def _mime_for(fmt: str) -> str:
    return {
        "pdf": "application/pdf",
        "png": "image/png",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
        "csv": "text/csv",
        "tsv": "text/tab-separated-values",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }[fmt]
