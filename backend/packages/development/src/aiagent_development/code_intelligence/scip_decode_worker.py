"""Isolated SCIP binary decoder; invoked only by the trusted adapter."""

from __future__ import annotations

import json
import os
import resource
import sys

# Set before importing Protobuf; protect the parent from parser amplification.
resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
sys.path.insert(0, os.path.dirname(__file__))
import scip_pb2  # type: ignore[import-not-found]  # noqa: E402


class ResourceLimit(ValueError):
    pass


def main() -> None:  # noqa: PLR0912, PLR0915
    data = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024:
        raise ResourceLimit("index byte limit")
    idx = scip_pb2.Index()
    idx.ParseFromString(data)
    if not idx.HasField("metadata") or not idx.metadata.HasField("tool_info"):
        raise ValueError("missing metadata")
    if len(idx.documents) > 10000:
        raise ResourceLimit("document limit")
    output = {
        # A canonical protobuf round trip is a conservative proof of original
        # top-level metadata order/uniqueness; other legal encodings are refused.
        "wire_canonical": idx.SerializeToString() == data,
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
    output["external_relationships"] = count_relations > 0
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
            "definition_relationships": False,
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
                    d["definition_relationships"] = True
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
