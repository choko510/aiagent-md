"""Deterministic, explainable security scanning for untrusted Skill content.

The scanner runs over decoded text of every bundle file. A match is a REVIEW SIGNAL, not proof of intent, and
a PASS is not proof of safety. Structural violations (archive, manifest, digest) are rejected before this stage
and are blocking; content findings stay heuristic because Skill content is non-executable untrusted data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .contracts import ScanResult, SecurityFinding, Severity, SkillFile
from .digest import findings_digest

ENGINE_VERSION = "skill-scan/1"


@dataclass(frozen=True)
class Rule:
    rule_id: str
    severity: Severity
    pattern: re.Pattern[str]
    rationale: str
    recommendation: str


def _rule(rule_id: str, severity: Severity, pattern: str, rationale: str, recommendation: str) -> Rule:
    return Rule(rule_id, severity, re.compile(pattern, re.IGNORECASE | re.MULTILINE), rationale, recommendation)


RULES: tuple[Rule, ...] = (
    _rule(
        "SKL-INJ-001",
        Severity.HIGH,
        r"(ignore|disregard|forget)\s+(all\s+)?(previous|prior|above|system)\s+(instructions?|prompts?)|you are now\b",
        "Content attempts to override higher-priority instructions.",
        "Treat as untrusted data; never place it in a system prompt.",
    ),
    _rule(
        "SKL-INJ-002",
        Severity.HIGH,
        r"(auto[- ]?approve|approve (this|the) (tool|action|request)|bypass\s+approval|without\s+asking\s+the\s+user)",
        "Content asks to bypass the human approval boundary.",
        "Approval is decided only by the existing Approval owner; refuse the request.",
    ),
    _rule(
        "SKL-SEC-001",
        Severity.HIGH,
        r"(exfiltrat|send|post|upload|leak)\w*\s+(the\s+)?(api[_-]?key|secret|credential|cookie|token|password)",
        "Content asks to move a credential or secret outside the trust boundary.",
        "Secrets never enter model context or Skill content; deny any such action.",
    ),
    _rule(
        "SKL-HOST-001",
        Severity.HIGH,
        r"(os\.system|subprocess\.(run|popen|call)|Invoke-Expression|cmd\.exe|powershell(\.exe)?|bash\s+-c)",
        "Content asks for a host shell or process execution.",
        "Only Gateway-approved sandbox.exec inside a Runtime Cell is allowed; host execution is forbidden.",
    ),
    _rule(
        "SKL-HOST-002",
        Severity.HIGH,
        r"(curl|wget)[^\n|]{0,200}\|\s*(sh|bash|zsh|python[0-9.]*)",
        "Content pipes a remote download directly into an interpreter.",
        "Do not fetch and execute remote code; require review and a pinned digest.",
    ),
    _rule(
        "SKL-PKG-001",
        Severity.MEDIUM,
        r"\b(pip[0-9.]*|npm|pnpm|yarn|uvx|npx)\s+(install|add|exec|run)\b",
        "Content installs or runs an external package.",
        "Package installation is outside Skill lifecycle; no host package manager may run.",
    ),
    _rule(
        "SKL-NET-001",
        Severity.HIGH,
        r"(/var/run/docker\.sock|docker\s+(run|exec|compose)|/proc/self/environ)",
        "Content addresses a host control plane or process environment.",
        "Docker/control-plane access is never delegated to a Skill.",
    ),
    _rule(
        "SKL-NET-002",
        Severity.HIGH,
        r"\b(ssh|scp|sftp)\s+[^\n]{0,120}",
        "Content asks for SSH/remote shell access.",
        "Host SSH and arbitrary remote execution are forbidden.",
    ),
    _rule(
        "SKL-BUILTIN-001",
        Severity.HIGH,
        r"(overwrite|replace|modify|patch).{0,60}(nihongo|ALWAYS\.md|built[- ]?in skill)",
        "Content attempts to modify a protected built-in Skill.",
        "Built-in Skill files are read-only package data and are never writable by user content.",
    ),
    _rule(
        "SKL-DATA-001",
        Severity.HIGH,
        r"(read|dump|collect|send|upload).{0,40}(all|every).{0,30}(memory|skill bod|reference|context)",
        "Content asks for bulk memory or Skill body extraction.",
        "Disclosure is on demand and run-scoped; bulk extraction is denied.",
    ),
    _rule(
        "SKL-MCP-001",
        Severity.HIGH,
        r"mcp.{0,40}(install|register|connect|start|enable)",
        "Content asks to install or activate an MCP server.",
        "MCP connection/installation is a separate owner and is never performed by a Skill.",
    ),
    _rule(
        "SKL-POL-001",
        Severity.HIGH,
        r"(write|modify|update|rewrite).{0,40}(tool policy|approval policy|policy file|capability grant)",
        "Content attempts to change policy or grants.",
        "Policy and grants are owned by trusted control code, never by Skill content.",
    ),
    _rule(
        "SKL-CRED-001",
        Severity.MEDIUM,
        r"(printenv|Get-ChildItem\s+Env:|cat\s+/proc/self/environ|env\s*\|)",
        "Content inspects process environment variables.",
        "Environment and credentials are not available to Skill content.",
    ),
    _rule(
        "SKL-CROSS-001",
        Severity.HIGH,
        r"(other|another)\s+(tenant|owner|user).{0,30}(artifact|memory|skill|file)",
        "Content references another tenant or owner.",
        "All reads are tenant/owner scoped; cross-scope access is denied.",
    ),
    _rule(
        "SKL-EXEC-001",
        Severity.MEDIUM,
        r"\b(eval|exec|compile|Function)\s*\(",
        "Content contains dynamic code evaluation.",
        "Scripts are not executed; dynamic code is a review signal only.",
    ),
    _rule(
        "SKL-OBF-001",
        Severity.MEDIUM,
        r"[A-Za-z0-9+/]{200,}={0,2}",
        "Content contains a long opaque blob that may hide instructions or code.",
        "Decode only in an isolated review environment; never auto-execute.",
    ),
    _rule(
        "SKL-HIDE-001",
        Severity.MEDIUM,
        r"(do not|don.t|never)\s+(tell|inform|show)\s+(the\s+)?user",
        "Content asks to hide an action from the user.",
        "Actions and approvals are visible; hidden side effects are not permitted.",
    ),
)

_TEXTUAL = frozenset({".md", ".txt", ".json", ".yaml", ".yml", ".py", ".js", ".ts", ".sh", ".html", ".css", ".toml"})


def _excerpt(line: str) -> str:
    clean = "".join(ch if ord(ch) >= 32 or ch == "\t" else " " for ch in line).strip()
    return clean[:200]


def scan_files(files: dict[str, bytes], entries: tuple[SkillFile, ...]) -> ScanResult:
    findings: list[SecurityFinding] = []
    media = {entry.relative_path: entry.media_type for entry in entries}
    for path, payload in sorted(files.items()):
        if path.casefold().startswith(("scripts/", "bin/")) or media.get(path) == "text/x-shellscript":
            findings.append(
                SecurityFinding(
                    rule_id="SKL-SCRIPT-001",
                    severity=Severity.INFO,
                    path=path,
                    line=None,
                    column=None,
                    excerpt="",
                    rationale="Bundle contains a script. Script execution is disabled; the file is review-only data.",
                    recommendation="Review the script manually. Do not enable execution.",
                    engine_version=ENGINE_VERSION,
                    heuristic=True,
                )
            )
        suffix = "." + path.rsplit(".", 1)[-1].casefold() if "." in path else ""
        if suffix not in _TEXTUAL:
            continue
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            text = payload.decode("utf-8", errors="replace")
        for rule in RULES:
            for match in rule.pattern.finditer(text):
                line_start = text.rfind("\n", 0, match.start()) + 1
                line_end = text.find("\n", match.end())
                if line_end < 0:
                    line_end = len(text)
                line_no = text.count("\n", 0, match.start()) + 1
                col = match.start() - line_start + 1
                findings.append(
                    SecurityFinding(
                        rule_id=rule.rule_id,
                        severity=rule.severity,
                        path=path,
                        line=line_no,
                        column=col,
                        excerpt=_excerpt(text[line_start:line_end]),
                        rationale=rule.rationale,
                        recommendation=rule.recommendation,
                        engine_version=ENGINE_VERSION,
                        heuristic=True,
                    )
                )
                if len(findings) >= 500:
                    break
    unique = {(f.rule_id, f.path, f.line, f.column): f for f in findings}
    ordered = tuple(unique[key] for key in sorted(unique, key=lambda k: (k[0], k[1], k[2] or 0, k[3] or 0)))
    blocking = any(f.severity is Severity.BLOCKING for f in ordered)
    return ScanResult(
        findings=ordered,
        engine_version=ENGINE_VERSION,
        digest=findings_digest(ordered),
        blocking=blocking,
    )
