"""Shared OOXML relationship and text helpers.

Only parts named by an internal relationship or an explicit allowlist are read.
An external relationship is a hard failure: the parser never fetches, renders
or follows it.
"""

from __future__ import annotations

import posixpath
import re

from ..archive_gate import read_entry
from ..errors import MmiError
from ..limits import MmiLimits
from ..xml_gate import XmlNode, parse_xml

_SAFE = re.compile(r"^[A-Za-z0-9_./\-\[\]]+$")


def relationships(data: bytes, part_path: str, names: set[str], limits: MmiLimits) -> dict[str, tuple[str, bool]]:
    base = posixpath.dirname(part_path)
    rels_path = posixpath.join(base, "_rels", posixpath.basename(part_path) + ".rels")
    if rels_path not in names:
        return {}
    root = parse_xml(read_entry(data, rels_path, limits), limits, root_expected="Relationships")
    out: dict[str, tuple[str, bool]] = {}
    for rel in root.children_named("Relationship"):
        rel_id = attr(rel, "Id") or ""
        target = attr(rel, "Target") or ""
        external = (attr(rel, "TargetMode") or "").lower() == "external"
        if not rel_id or not target:
            continue
        if external:
            raise MmiError("EXTERNAL_REFERENCE", "external OOXML relationship is not allowed")
        resolved = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(base, target))
        if resolved.startswith("../") or not _SAFE.match(resolved):
            raise MmiError("BAD_ARCHIVE", "unsafe OOXML relationship target")
        out[rel_id] = (resolved, external)
    return out


def assert_no_external_relationships(data: bytes, names: set[str], limits: MmiLimits) -> None:
    for name in sorted(names):
        if not name.endswith(".rels"):
            continue
        owner = posixpath.join(posixpath.dirname(posixpath.dirname(name)), posixpath.basename(name)[:-5])
        relationships(data, owner, names, limits)


def attr(node: XmlNode | None, name: str) -> str | None:
    if node is None:
        return None
    if name in node.attrs:
        return node.attrs[name]
    for key, value in node.attrs.items():
        if key.rsplit(":", 1)[-1] == name:
            return value
    return None


def int_attr(node: XmlNode, name: str, *, default: int) -> int:
    value = attr(node, name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise MmiError("BAD_XML", f"invalid integer attribute {name}") from exc


def bool_attr(node: XmlNode, name: str) -> bool:
    return (attr(node, name) or "").strip().lower() in {"1", "true", "on"}


def local_name(node: XmlNode) -> str:
    return node.name.rsplit(":", 1)[-1]


def word_paragraph_text(paragraph: XmlNode) -> str:
    """Flatten WordprocessingML runs into text; tabs/breaks stay explicit."""
    parts: list[str] = []

    def walk(node: XmlNode) -> None:
        local = local_name(node)
        if local == "t":
            parts.append(node.text)
            return
        if local == "tab":
            parts.append("\t")
            return
        if local in {"br", "cr"}:
            parts.append("\n")
            return
        if local == "noBreakHyphen":
            parts.append("-")
            return
        if local in {"delText", "instrText"}:
            return
        for child in node.children:
            walk(child)

    walk(paragraph)
    return "".join(parts).replace("\u00a0", " ")


def drawing_alt_texts(paragraph: XmlNode) -> list[str]:
    texts: list[str] = []
    for node in paragraph.iter():
        if local_name(node) in {"docPr", "cNvPr"}:
            descr = attr(node, "descr") or attr(node, "title")
            if descr:
                texts.append(descr)
    return texts


def pptx_paragraph_text(paragraph: XmlNode) -> str:
    parts: list[str] = []

    def walk(node: XmlNode) -> None:
        local = local_name(node)
        if local == "t":
            parts.append(node.text)
            return
        if local == "br":
            parts.append("\n")
            return
        if local == "tab":
            parts.append("\t")
            return
        for child in node.children:
            walk(child)

    walk(paragraph)
    return "".join(parts).replace("\u00a0", " ")
