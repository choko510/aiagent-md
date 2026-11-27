"""Bounded SKILL.md frontmatter parsing.

We never call `yaml.load`: the YAML document is composed into a node graph, then converted by this module with
explicit type/tag allowlists, duplicate-key rejection, depth/node/alias budgets and no object construction.
That blocks Python object tags, YAML alias bombs and recursive anchors.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import yaml  # type: ignore[import-untyped]
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode  # type: ignore[import-untyped]

from .digest import sha256_bytes
from .errors import FrontmatterInvalid, ManifestInvalid

MAX_SKILL_MD_BYTES = 64 * 1024
MAX_FRONTMATTER_BYTES = 16 * 1024
MAX_YAML_NODES = 4000
MAX_YAML_EVENTS = 8000
MAX_YAML_DEPTH = 12

_ALLOWED_SCALAR_TAGS = frozenset(
    {
        "tag:yaml.org,2002:str",
        "tag:yaml.org,2002:int",
        "tag:yaml.org,2002:float",
        "tag:yaml.org,2002:bool",
        "tag:yaml.org,2002:null",
    }
)


@dataclass(frozen=True)
class Frontmatter:
    data: dict[str, Any]
    body: str
    digest: str
    raw: str


def _scalar(node: ScalarNode) -> Any:
    tag = node.tag
    if tag not in _ALLOWED_SCALAR_TAGS:
        raise FrontmatterInvalid(f"unsupported YAML tag {tag}", line=node.start_mark.line + 1)
    value = node.value
    if tag.endswith(":str"):
        return value
    if tag.endswith(":null"):
        return None
    if tag.endswith(":bool"):
        return yaml.safe_load(value)
    if tag.endswith(":int"):
        try:
            return int(yaml.safe_load(value))
        except (TypeError, ValueError):
            raise FrontmatterInvalid("invalid integer", line=node.start_mark.line + 1) from None
    try:
        parsed = yaml.safe_load(value)
    except yaml.YAMLError:
        raise FrontmatterInvalid("invalid float", line=node.start_mark.line + 1) from None
    if not isinstance(parsed, float) or not math.isfinite(parsed):
        raise FrontmatterInvalid("non-finite float is not allowed", line=node.start_mark.line + 1)
    return parsed


class _Converter:
    def __init__(self) -> None:
        self.nodes = 0
        self.active: set[int] = set()

    def convert(self, node: Node, depth: int = 0) -> Any:
        self.nodes += 1
        if self.nodes > MAX_YAML_NODES:
            raise FrontmatterInvalid("YAML document is too large")
        if depth > MAX_YAML_DEPTH:
            raise FrontmatterInvalid("YAML nesting is too deep")
        ident = id(node)
        if ident in self.active:
            raise FrontmatterInvalid("recursive YAML alias is not allowed", line=node.start_mark.line + 1)
        self.active.add(ident)
        try:
            if isinstance(node, ScalarNode):
                return _scalar(node)
            if isinstance(node, SequenceNode):
                if node.tag != "tag:yaml.org,2002:seq":
                    raise FrontmatterInvalid("unsupported YAML tag", line=node.start_mark.line + 1)
                return [self.convert(child, depth + 1) for child in node.value]
            if isinstance(node, MappingNode):
                if node.tag != "tag:yaml.org,2002:map":
                    raise FrontmatterInvalid("unsupported YAML tag", line=node.start_mark.line + 1)
                out: dict[str, Any] = {}
                for key_node, value_node in node.value:
                    if not isinstance(key_node, ScalarNode) or not key_node.tag.endswith(":str"):
                        raise FrontmatterInvalid(
                            "mapping keys must be plain strings", line=key_node.start_mark.line + 1
                        )
                    key = key_node.value
                    if key in out:
                        raise FrontmatterInvalid(f"duplicate YAML key: {key}", line=key_node.start_mark.line + 1)
                    out[key] = self.convert(value_node, depth + 1)
                return out
            raise FrontmatterInvalid("unsupported YAML node", line=node.start_mark.line + 1)
        finally:
            self.active.discard(ident)


def _preflight_yaml(raw: str) -> None:
    """Bound the event stream and reject aliases/anchors before compose can build shared nodes."""
    events = 0
    depth = 0
    try:
        for event in yaml.parse(raw, Loader=yaml.SafeLoader):
            events += 1
            if events > MAX_YAML_EVENTS:
                raise FrontmatterInvalid("YAML document has too many events")
            if isinstance(event, yaml.events.AliasEvent):
                raise FrontmatterInvalid("YAML aliases are not allowed", line=event.start_mark.line + 1)
            if getattr(event, "anchor", None):
                raise FrontmatterInvalid("YAML anchors are not allowed", line=event.start_mark.line + 1)
            if isinstance(event, (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent)):
                depth += 1
                if depth > MAX_YAML_DEPTH:
                    raise FrontmatterInvalid("YAML nesting is too deep", line=event.start_mark.line + 1)
            elif isinstance(event, (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent)):
                depth -= 1
    except yaml.YAMLError as exc:
        raise FrontmatterInvalid("invalid YAML", problem=str(exc)[:200]) from None


def parse_skill_markdown(data: bytes, *, max_bytes: int = MAX_SKILL_MD_BYTES) -> Frontmatter:
    if len(data) > max_bytes:
        raise ManifestInvalid(f"SKILL.md exceeds {max_bytes} bytes", size=len(data))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise FrontmatterInvalid("SKILL.md must be valid UTF-8") from None
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise FrontmatterInvalid("SKILL.md must start with a YAML frontmatter block")
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        raise FrontmatterInvalid("frontmatter block is not closed")
    raw = "".join(lines[1:end])
    if len(raw.encode("utf-8")) > MAX_FRONTMATTER_BYTES:
        raise FrontmatterInvalid(f"frontmatter exceeds {MAX_FRONTMATTER_BYTES} bytes")
    _preflight_yaml(raw)
    try:
        documents = list(yaml.compose_all(raw, Loader=yaml.SafeLoader))
    except yaml.YAMLError as exc:
        raise FrontmatterInvalid("invalid YAML", problem=str(exc)[:200]) from None
    documents = [d for d in documents if d is not None]
    if len(documents) != 1:
        raise FrontmatterInvalid("frontmatter must contain exactly one YAML document")
    node = documents[0]
    if not isinstance(node, MappingNode):
        raise FrontmatterInvalid("frontmatter must be a YAML mapping")
    data = _Converter().convert(node)
    if not isinstance(data, dict):
        raise FrontmatterInvalid("frontmatter must be a YAML mapping")
    body = "".join(lines[end + 1 :])
    return Frontmatter(data=data, body=body, digest=sha256_bytes(raw.encode("utf-8")), raw=raw)
