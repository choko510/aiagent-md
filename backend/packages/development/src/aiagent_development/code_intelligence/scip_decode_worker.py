"""Isolated SCIP binary decoder; invoked only by the trusted adapter."""

from __future__ import annotations

import json
import os
import resource
import sys
from typing import Any

# Set before importing Protobuf; protect the parent from parser amplification.
resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
sys.path.insert(0, os.path.dirname(__file__))
import scip_pb2  # type: ignore[import-not-found]  # noqa: E402
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory, unknown_fields  # type: ignore[import-untyped]  # noqa: E402
from google.protobuf.internal import decoder  # type: ignore[import-untyped]  # noqa: E402


class ResourceLimit(ValueError):
    pass


def validate_envelope(data: bytes) -> None:
    """Validate wire types without losing overwritten message/oneof values.

    A descriptor derived from the pinned official schema treats message
    fields as repeated and removes oneofs only in this validation view. The
    Protobuf runtime decodes both this view and the original schema; the latter
    remains authoritative for normal merge, oneof and range semantics.
    """
    if not data:
        raise ValueError("missing metadata")
    tag, _ = decoder._DecodeVarint(data, 0)
    if tag != 10:
        raise ValueError("SCIP metadata must be first")
    schema = descriptor_pb2.FileDescriptorProto()
    schema.ParseFromString(scip_pb2.DESCRIPTOR.serialized_pb)

    def preserve_messages(message: Any) -> None:
        for field in message.field:
            if field.type == descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE:
                field.label = descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED
            if field.HasField("oneof_index"):
                field.ClearField("oneof_index")
        message.ClearField("oneof_decl")
        for nested in message.nested_type:
            preserve_messages(nested)

    for message in schema.message_type:
        preserve_messages(message)
    pool = descriptor_pool.DescriptorPool()
    pool.Add(schema)
    probe_type = message_factory.GetMessageClass(pool.FindMessageTypeByName("scip.Index"))
    probe = probe_type()
    probe.ParseFromString(data)
    if len(probe.metadata) != 1:
        raise ValueError("duplicate or missing SCIP metadata")
    validate_known_field_wire_types(probe)


def validate_known_field_wire_types(root: Any) -> None:
    """Reject known fields decoded as unknown because their wire type is wrong."""
    pending = [root]
    while pending:
        message = pending.pop()
        known = message.DESCRIPTOR.fields_by_number
        if any(field.field_number in known for field in unknown_fields.UnknownFieldSet(message)):
            raise ValueError("known field has invalid wire type")
        for field, value in message.ListFields():
            if field.message_type is not None:
                if field.is_repeated:
                    pending.extend(value)
                else:
                    pending.append(value)


def main() -> None:  # noqa: PLR0912, PLR0915
    data = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024:
        raise ResourceLimit("index byte limit")
    validate_envelope(data)
    idx = scip_pb2.Index()
    idx.ParseFromString(data)
    if not idx.HasField("metadata") or not idx.metadata.HasField("tool_info"):
        raise ValueError("missing metadata")
    if len(idx.documents) > 10000:
        raise ResourceLimit("document limit")
    output = {
        "version": idx.metadata.version,
        "tool": idx.metadata.tool_info.name,
        "tool_version": idx.metadata.tool_info.version,
        "encoding": idx.metadata.text_document_encoding,
        "docs": [],
    }
    count_occurrences = 0
    count_symbols = len(idx.external_symbols)
    if count_symbols > 50000 or any(len(sym.symbol.encode("utf-8")) > 4096 for sym in idx.external_symbols):
        raise ResourceLimit("external symbol limit")
    count_relations = sum(len(sym.relationships) for sym in idx.external_symbols)
    if count_relations > 50000:
        raise ResourceLimit("relationship limit")
    output["external_relationships"] = []
    for sym in idx.external_symbols:
        for rel in sym.relationships:
            if (rel.is_implementation or rel.is_reference or rel.is_definition) and (
                not sym.symbol or not rel.symbol or len(rel.symbol.encode("utf-8")) > 4096
            ):
                raise ValueError("invalid external relationship")
            if rel.is_implementation or rel.is_reference or rel.is_definition:
                output["external_relationships"].append(
                    [sym.symbol, rel.symbol, rel.is_definition, rel.is_reference, rel.is_implementation]
                )
    for doc in idx.documents:
        count_occurrences += len(doc.occurrences)
        count_symbols += len(doc.symbols)
        if count_occurrences > 50000 or count_symbols > 50000:
            raise ResourceLimit("symbol or occurrence limit")
        d = {
            "path": doc.relative_path,
            "encoding": doc.position_encoding,
            "text_digest": __import__("hashlib").sha256(doc.text.encode("utf-8")).hexdigest() if doc.text else None,
            "occurrences": [],
            "relationships": [],
            "reference_relationships": [],
            "definition_relationships": [],
        }
        for occ in doc.occurrences:
            kind = occ.WhichOneof("typed_range")
            if kind == "single_line_range":
                r = occ.single_line_range
                pos = [r.line, r.start_character, r.line, r.end_character]
            elif kind == "multi_line_range":
                r = occ.multi_line_range
                pos = [r.start_line, r.start_character, r.end_line, r.end_character]
            elif len(occ.range) == 3:
                pos = [occ.range[0], occ.range[1], occ.range[0], occ.range[2]]
            elif len(occ.range) == 4:
                pos = list(occ.range)
            else:
                raise ValueError("invalid occurrence range")
            if kind and occ.range:
                legacy = list(occ.range)
                if len(legacy) == 3:
                    legacy = [legacy[0], legacy[1], legacy[0], legacy[2]]
                if legacy != pos:
                    raise ValueError("conflicting occurrence ranges")
            if len(occ.symbol.encode("utf-8")) > 4096:
                raise ResourceLimit("symbol size limit")
            d["occurrences"].append([occ.symbol, occ.symbol_roles, pos])
        for sym in doc.symbols:
            if not sym.symbol or len(sym.symbol.encode("utf-8")) > 4096:
                raise ValueError("invalid symbol")
            count_relations += len(sym.relationships)
            if count_relations > 50000:
                raise ResourceLimit("relationship limit")
            for rel in sym.relationships:
                if (rel.is_implementation or rel.is_reference or rel.is_definition) and (
                    not rel.symbol or len(rel.symbol.encode("utf-8")) > 4096
                ):
                    raise ValueError("invalid relationship")
                if rel.is_implementation:
                    d["relationships"].append([sym.symbol, rel.symbol])
                if rel.is_reference:
                    d["reference_relationships"].append([sym.symbol, rel.symbol])
                if rel.is_definition:
                    d["definition_relationships"].append([sym.symbol, rel.symbol])
        output["docs"].append(d)
    encoded = json.dumps(output, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(encoded) > 16 * 1024 * 1024:
        raise ResourceLimit("decoded output limit")
    sys.stdout.buffer.write(encoded)


if __name__ == "__main__":
    try:
        main()
    except (ResourceLimit, MemoryError):
        sys.exit(2)
    except Exception:  # noqa: BLE001
        sys.exit(1)
