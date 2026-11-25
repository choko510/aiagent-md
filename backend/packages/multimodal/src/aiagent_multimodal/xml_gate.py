"""Strict OOXML XML reader with DTD/entity/external-reference refusal (15-01 §7.2).

`xml.etree` is not used for untrusted input because its default parser still
performs internal entity expansion. This reader drives expat directly: a
DOCTYPE declaration, entity declaration, XInclude or external entity is a hard
error, and node/depth/text budgets are enforced during the event stream.

The returned tree is a small, immutable-by-convention projection of the XML
that keeps prefixes so OOXML parts (`w:p`, `p:sp`, `r:id`) remain readable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final
from xml.parsers import expat

from .errors import MmiError
from .limits import MmiLimits

_ENTITY_LIKE: Final[tuple[str, ...]] = ("<!ENTITY", "<!DOCTYPE", "XInclude")


@dataclass(slots=True)
class XmlNode:
    name: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[XmlNode] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)

    @property
    def local(self) -> str:
        return self.name.rsplit(":", 1)[-1]

    @property
    def text(self) -> str:
        return "".join(self.text_parts)

    def child(self, name: str) -> XmlNode | None:
        for node in self.children:
            if node.name == name or node.local == name.rsplit(":", 1)[-1]:
                return node
        return None

    def children_named(self, name: str) -> list[XmlNode]:
        local = name.rsplit(":", 1)[-1]
        return [n for n in self.children if n.name == name or n.local == local]

    def iter(self) -> list[XmlNode]:
        out: list[XmlNode] = [self]
        for child in self.children:
            out.extend(child.iter())
        return out

    def find(self, path: str) -> XmlNode | None:
        node: XmlNode | None = self
        for part in path.split("/"):
            if node is None:
                return None
            node = node.child(part)
        return node

    def all_text(self) -> str:
        return "".join(n.text for n in self.iter())


def parse_xml(data: bytes, limits: MmiLimits, *, root_expected: str | None = None) -> XmlNode:
    if not data:
        raise MmiError("BAD_XML", "empty XML part")
    budget = _Budget(limits)
    parser = expat.ParserCreate()
    parser.buffer_text = True
    parser.buffer_size = 64 * 1024
    stack: list[XmlNode] = []
    root: list[XmlNode] = []

    def forbid(*_args: object) -> None:
        raise MmiError("BAD_XML", "DTD/entity declarations are not allowed")

    def external(*_args: object) -> int:
        raise MmiError("EXTERNAL_REFERENCE", "external XML entity is not allowed")

    def start(name: str, attrs: dict[str, str]) -> None:
        budget.node()
        node = XmlNode(name, dict(attrs))
        if stack:
            budget.depth(len(stack) + 1)
            stack[-1].children.append(node)
        elif root:
            raise MmiError("BAD_XML", "multiple XML roots")
        else:
            root.append(node)
        stack.append(node)

    def end(_name: str) -> None:
        if not stack:
            raise MmiError("BAD_XML", "unbalanced XML")
        stack.pop()

    def chars(text: str) -> None:
        if stack:
            budget.text(len(text.encode("utf-8", "ignore")))
            stack[-1].text_parts.append(text)

    parser.StartDoctypeDeclHandler = forbid
    parser.EntityDeclHandler = forbid
    parser.UnparsedEntityDeclHandler = forbid
    parser.ExternalEntityRefHandler = external
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    try:
        parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    except Exception as exc:  # pragma: no cover - expat always supports this
        raise MmiError("BAD_XML", "cannot disable parameter entities") from exc
    try:
        parser.Parse(data, True)
    except MmiError:
        raise
    except expat.ExpatError as exc:
        raise MmiError("BAD_XML", f"malformed XML ({exc.code})") from exc
    if not root:
        raise MmiError("BAD_XML", "XML has no root element")
    node = root[0]
    if root_expected is not None and node.local != root_expected:
        raise MmiError("BAD_XML", "unexpected XML root element")
    return node


def xml_from_text(text: str, limits: MmiLimits) -> XmlNode:
    return parse_xml(text.encode("utf-8"), limits)


class _Budget:
    __slots__ = ("limits", "nodes", "text_bytes")

    def __init__(self, limits: MmiLimits) -> None:
        self.limits = limits
        self.nodes = 0
        self.text_bytes = 0

    def node(self) -> None:
        self.nodes += 1
        if self.nodes > self.limits.max_xml_nodes:
            raise MmiError("EXPANSION_LIMIT", "XML node count exceeds limit")

    def depth(self, depth: int) -> None:
        if depth > self.limits.max_xml_depth:
            raise MmiError("EXPANSION_LIMIT", "XML depth exceeds limit")

    def text(self, size: int) -> None:
        self.text_bytes += size
        if self.text_bytes > self.limits.max_xml_text_bytes:
            raise MmiError("EXPANSION_LIMIT", "XML text exceeds limit")
