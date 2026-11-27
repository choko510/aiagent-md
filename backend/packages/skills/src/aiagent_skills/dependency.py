"""Bounded SemVer dependency resolution and external dependency checks.

Resolution is deterministic and never falls back to latest: an unsatisfied required constraint is an error,
cycles and depth/count overflows are errors, and the chosen exact versions become an immutable pin.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import total_ordering

from .contracts import DependencyPin, SkillDependency
from .digest import dependency_digest
from .errors import DependencyError

MAX_DEPTH = 4
MAX_COUNT = 16

_VERSION_RE = re.compile(
    r"\A(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?\Z"
)


@total_ordering
@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: str) -> Version:
        match = _VERSION_RE.match(value)
        if not match:
            raise DependencyError(f"invalid SemVer: {value[:40]}")
        pre = tuple(match.group(4).split(".")) if match.group(4) else ()
        build = tuple(match.group(5).split(".")) if match.group(5) else ()
        return cls(int(match.group(1)), int(match.group(2)), int(match.group(3)), pre, build)

    def _key(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def _precedence(self) -> tuple[object, ...]:
        # SemVer build metadata is part of the immutable version string but does not affect precedence.
        return (self._key(), self.prerelease)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._precedence() == other._precedence()

    def __hash__(self) -> int:
        return hash(self._precedence())

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        if self._key() != other._key():
            return self._key() < other._key()
        if self.prerelease == other.prerelease:
            return False
        if not self.prerelease:
            return False
        if not other.prerelease:
            return True
        for left, right in zip(self.prerelease, other.prerelease, strict=False):
            if left == right:
                continue
            left_num = left.isdigit()
            right_num = right.isdigit()
            if left_num and right_num:
                return int(left) < int(right)
            if left_num != right_num:
                return left_num
            return left < right
        return len(self.prerelease) < len(other.prerelease)

    def __str__(self) -> str:
        suffix = "-" + ".".join(self.prerelease) if self.prerelease else ""
        build = "+" + ".".join(self.build) if self.build else ""
        return f"{self.major}.{self.minor}.{self.patch}{suffix}{build}"


def _partial(value: str) -> tuple[int, int, int]:
    parts = value.split(".")
    if not 1 <= len(parts) <= 3:
        raise DependencyError(f"invalid version constraint: {value[:40]}")
    numbers = []
    for part in parts:
        if part in ("*", "x", "X"):
            numbers.append(0)
        elif part.isdigit():
            numbers.append(int(part))
        else:
            raise DependencyError(f"invalid version constraint: {value[:40]}")
    while len(numbers) < 3:
        numbers.append(0)
    return numbers[0], numbers[1], numbers[2]


@dataclass(frozen=True)
class VersionRange:
    """Comma/space-separated AND comparators with `||` alternatives."""

    alternatives: tuple[tuple[tuple[str, Version], ...], ...]

    @classmethod
    def parse(cls, text: str) -> VersionRange:
        raw = text.strip()
        if not raw or raw == "*":
            return cls(((),))
        groups: list[tuple[tuple[str, Version], ...]] = []
        for alternative in raw.split("||"):
            if not alternative.strip():
                raise DependencyError("empty alternative in version constraint")
            tokens = [t for t in re.split(r"[\s,]+", alternative.strip()) if t]
            comparators: list[tuple[str, Version]] = []
            index = 0
            while index < len(tokens):
                token = tokens[index]
                index += 1
                if token in (">=", "<=", ">", "<", "=", "=="):
                    if index >= len(tokens):
                        raise DependencyError("comparator has no version")
                    comparators.append(("=" if token == "==" else token, Version.parse(tokens[index])))  # noqa: S105
                    index += 1
                    continue
                if token.startswith((">=", "<=", "==", ">", "<", "=")):
                    for operator in (">=", "<=", "==", ">", "<", "="):
                        if token.startswith(operator):
                            comparators.append(
                                (
                                    "=" if operator == "==" else operator,
                                    Version.parse(token[len(operator) :]),
                                )
                            )
                            break
                    continue
                if token.startswith("^"):
                    base = Version.parse(token[1:])
                    comparators.append((">=", base))
                    if base.major > 0:
                        upper = Version(base.major + 1, 0, 0)
                    elif base.minor > 0:
                        upper = Version(0, base.minor + 1, 0)
                    else:
                        upper = Version(0, 0, base.patch + 1)
                    comparators.append(("<", upper))
                    continue
                if token.startswith("~"):
                    base = Version.parse(token[1:])
                    comparators.append((">=", base))
                    comparators.append(("<", Version(base.major, base.minor + 1, 0)))
                    continue
                if token.endswith(".x") or token.endswith(".*"):
                    prefix = token[:-2].split(".")
                    try:
                        numbers = [int(part) for part in prefix]
                    except ValueError:
                        raise DependencyError(f"invalid wildcard: {token[:40]}") from None
                    if len(numbers) == 1:
                        comparators.append((">=", Version(numbers[0], 0, 0)))
                        comparators.append(("<", Version(numbers[0] + 1, 0, 0)))
                    elif len(numbers) == 2:
                        major, minor = numbers
                        comparators.append((">=", Version(major, minor, 0)))
                        comparators.append(("<", Version(major, minor + 1, 0)))
                    else:
                        raise DependencyError(f"invalid wildcard: {token[:40]}")
                    continue
                # An exact version may contain a prerelease suffix (for example 1.2.3-alpha).
                try:
                    exact = Version.parse(token)
                except DependencyError:
                    if "-" in token:
                        raise DependencyError(f"hyphen ranges are not supported: {token[:40]}") from None
                    raise
                comparators.append(("=", exact))
            groups.append(tuple(comparators))
        return cls(tuple(groups))

    def contains(self, version: Version) -> bool:
        for comparators in self.alternatives:
            if all(self._compare(version, op, target) for op, target in comparators):
                return True
        return False

    @staticmethod
    def _compare(version: Version, operator: str, target: Version) -> bool:
        if operator == "=" and target.build:
            # Build metadata is ignored for precedence, but an explicitly written build is an exact identity.
            return version == target and version.build == target.build
        if operator == ">=":
            return version >= target
        if operator == "<=":
            return version <= target
        if operator == ">":
            return version > target
        if operator == "<":
            return version < target
        return version == target


@dataclass(frozen=True)
class DependencyResolution:
    pins: dict[str, str]
    order: tuple[str, ...]
    optional_missing: tuple[str, ...]
    digest: str
    exact: tuple[DependencyPin, ...] = ()


class DependencyResolver:
    def __init__(
        self,
        catalog: Mapping[str, Sequence[str]],
        edges: Callable[[str, str], Sequence[SkillDependency]],
        *,
        max_depth: int = MAX_DEPTH,
        max_count: int = MAX_COUNT,
    ) -> None:
        self.catalog = catalog
        self.edges = edges
        self.max_depth = max_depth
        self.max_count = max_count

    def resolve(self, root: Sequence[SkillDependency]) -> DependencyResolution:
        pins: dict[str, str] = {}
        order: list[str] = []
        optional_missing: list[str] = []
        stack: list[str] = []

        def visit(dependency: SkillDependency, depth: int) -> None:
            name = dependency.skill_name
            if name in stack:
                raise DependencyError(f"circular Skill dependency: {name}")
            if depth > self.max_depth:
                raise DependencyError("Skill dependency depth limit exceeded")
            rng = VersionRange.parse(dependency.range)
            if name in pins:
                if not rng.contains(Version.parse(pins[name])):
                    raise DependencyError(f"conflicting version constraints for {name}")
                return
            versions = [v for v in self.catalog.get(name, ()) if rng.contains(Version.parse(v))]
            if not versions:
                if dependency.optional:
                    if name not in optional_missing:
                        optional_missing.append(name)
                    return
                raise DependencyError(f"missing required Skill dependency: {name}")
            # Preserve the exact catalog string, including build metadata. Lexical tie-break keeps equal-precedence
            # build variants deterministic instead of letting input order decide.
            chosen = max(versions, key=lambda value: (Version.parse(value), value))
            if len(pins) >= self.max_count:
                raise DependencyError("Skill dependency count limit exceeded")
            pins[name] = chosen
            stack.append(name)
            try:
                for child in self.edges(name, chosen):
                    visit(child, depth + 1)
            finally:
                stack.pop()
            order.append(name)

        for item in root:
            visit(item, 1)
        return DependencyResolution(pins, tuple(order), tuple(sorted(optional_missing)), dependency_digest(pins))


@dataclass(frozen=True)
class ExternalAvailability:
    tools: frozenset[str]
    mcp_tools: frozenset[str]
    harness_api: str = "1"
    platform: str = "linux"


def check_external_requirements(
    requested_capabilities: Sequence[str],
    availability: ExternalAvailability,
) -> tuple[str, ...]:
    """Return stable issue codes; MCP absence is DEPENDENCY_UNAVAILABLE, never auto-connect."""
    issues: list[str] = []
    for capability in requested_capabilities:
        if capability.startswith("tool:"):
            tool = capability.split(":", 1)[1]
            if tool not in availability.tools:
                issues.append(f"TOOL_UNAVAILABLE:{tool}")
        elif capability.startswith("mcp:"):
            target = capability.split(":", 1)[1]
            if target not in availability.mcp_tools:
                issues.append(f"DEPENDENCY_UNAVAILABLE:mcp:{target}")
    return tuple(issues)
