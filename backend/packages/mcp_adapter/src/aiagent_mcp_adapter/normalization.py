"""Normalize untrusted `tools/call` results before anything reaches the model or Evidence.

Binary payloads are never inlined into model context: only kind/mime/size/digest is kept. Text is
control-character-stripped and bounded; resource links are carried as inert data and never fetched.
A pinned output schema is re-verified against the exact schema digest, so a server cannot silently
change the shape it returns.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from typing import Any

from .canonical import canonical_json, digest
from .config import AdapterLimits
from .contracts import ContentBlock, NormalizedResult, ToolEntry
from .errors import ErrorCode, McpError
from .schema_validation import CompiledSchema, compile_schema, validate_output

_CONTROL_ALLOWED = {"\n": "\n", "\r": "\r", "\t": "\t"}


def _clean_text(value: str, limit: int) -> tuple[str, bool]:
    out: list[str] = []
    used = 0
    truncated = False
    for ch in value:
        if ch in _CONTROL_ALLOWED:
            piece = _CONTROL_ALLOWED[ch]
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            piece = " "
        else:
            piece = ch
        encoded = len(piece.encode("utf-8"))
        if used + encoded > limit:
            truncated = True
            break
        out.append(piece)
        used += encoded
    return "".join(out), truncated


def normalize_result(
    raw: Any,
    *,
    entry: ToolEntry,
    limits: AdapterLimits,
    output_schema: dict[str, Any] | None = None,
) -> NormalizedResult:
    if not isinstance(raw, dict):
        raise McpError(ErrorCode.RESULT_UNSUPPORTED, "result_not_object")
    blocks_raw = raw.get("content", [])
    if blocks_raw is None:
        blocks_raw = []
    if not isinstance(blocks_raw, list):
        raise McpError(ErrorCode.RESULT_UNSUPPORTED, "content_not_list")
    structured = raw.get("structuredContent")
    if structured is not None:
        if not isinstance(structured, dict):
            raise McpError(ErrorCode.RESULT_UNSUPPORTED, "structured_content_not_object")
        if output_schema is not None:
            compiled = _compiled_output(output_schema, limits)
            errors = validate_output(compiled, structured)
            if errors:
                raise McpError(ErrorCode.OUTPUT_SCHEMA_MISMATCH, ",".join(errors))
        elif entry.output_digest is None:
            # No pinned schema: structured content is still allowed, but its digest cannot be pinned.
            pass
        if len(canonical_json(structured).encode("utf-8")) > limits.max_text_bytes:
            raise McpError(ErrorCode.RESULT_TRUNCATED, "structured_content_too_large")

    blocks: list[ContentBlock] = []
    notes: list[str] = []
    text_parts: list[str] = []
    text_used = 0
    truncated_any = False
    unsupported = 0
    for item in blocks_raw:
        if not isinstance(item, dict):
            unsupported += 1
            continue
        kind = item.get("type")
        if kind == "text":
            text = item.get("text")
            if not isinstance(text, str):
                unsupported += 1
                continue
            cleaned, truncated = _clean_text(text, limits.max_text_bytes - text_used)
            if truncated:
                truncated_any = True
                notes.append("text_truncated")
            if cleaned:
                text_parts.append(cleaned)
                text_used += len(cleaned.encode("utf-8"))
            blocks.append(ContentBlock(kind="text", text=cleaned, size_bytes=len(cleaned.encode("utf-8"))))
        elif kind in ("image", "audio"):
            data = item.get("data")
            mime = item.get("mimeType")
            if not isinstance(data, str) or not isinstance(mime, str):
                unsupported += 1
                continue
            try:
                decoded = base64.b64decode(data, validate=True)
            except (binascii.Error, ValueError):
                unsupported += 1
                continue
            if len(decoded) > limits.max_response_bytes:
                unsupported += 1
                notes.append("binary_too_large")
                continue
            blocks.append(
                ContentBlock(
                    kind=kind,  # type: ignore[arg-type]
                    mime_type=mime[:128],
                    size_bytes=len(decoded),
                    sha256=hashlib.sha256(decoded).hexdigest(),
                )
            )
            notes.append(f"{kind}_not_inlined")
        elif kind == "resource_link":
            uri = item.get("uri")
            if not isinstance(uri, str) or len(uri) > 2048 or any(ord(c) < 0x20 for c in uri):
                unsupported += 1
                continue
            blocks.append(
                ContentBlock(
                    kind="resource_link",
                    uri=uri,
                    mime_type=(item.get("mimeType") or None) if isinstance(item.get("mimeType"), str) else None,
                )
            )
            notes.append("resource_link_not_fetched")
        elif kind == "resource":
            resource = item.get("resource")
            if not isinstance(resource, dict):
                unsupported += 1
                continue
            uri = resource.get("uri")
            if not isinstance(uri, str) or len(uri) > 2048:
                unsupported += 1
                continue
            if isinstance(resource.get("text"), str):
                cleaned, truncated = _clean_text(resource["text"], limits.max_text_bytes - text_used)
                if truncated:
                    truncated_any = True
                    notes.append("text_truncated")
                text_parts.append(cleaned)
                text_used += len(cleaned.encode("utf-8"))
                blocks.append(
                    ContentBlock(
                        kind="resource",
                        text=cleaned,
                        uri=uri,
                        mime_type=(resource.get("mimeType") if isinstance(resource.get("mimeType"), str) else None),
                        size_bytes=len(cleaned.encode("utf-8")),
                    )
                )
            elif isinstance(resource.get("blob"), str):
                try:
                    decoded = base64.b64decode(resource["blob"], validate=True)
                except (binascii.Error, ValueError):
                    unsupported += 1
                    continue
                if len(decoded) > limits.max_response_bytes:
                    unsupported += 1
                    notes.append("binary_too_large")
                    continue
                blocks.append(
                    ContentBlock(
                        kind="resource",
                        uri=uri,
                        mime_type=(resource.get("mimeType") if isinstance(resource.get("mimeType"), str) else None),
                        size_bytes=len(decoded),
                        sha256=hashlib.sha256(decoded).hexdigest(),
                    )
                )
                notes.append("resource_blob_not_inlined")
            else:
                unsupported += 1
        else:
            unsupported += 1

    if unsupported:
        notes.append(f"unsupported_blocks:{unsupported}")
    if not blocks:
        return NormalizedResult(
            status="UNSUPPORTED",
            structured=structured,
            notes=tuple(dict.fromkeys(notes)),
        )
    status = "TOOL_ERROR" if bool(raw.get("isError")) else "OK"
    joined = "\n".join(text_parts)
    return NormalizedResult(
        status=status,  # type: ignore[arg-type]
        blocks=tuple(blocks),
        text=joined,
        structured=structured,
        truncated=truncated_any,
        notes=tuple(dict.fromkeys(notes)),
    )


def _compiled_output(schema: dict[str, Any], limits: AdapterLimits) -> CompiledSchema:
    return compile_schema(schema, limits, where="output")


def result_fingerprint(result: NormalizedResult) -> str:
    return digest(result.model_dump(mode="json", exclude_none=True))
