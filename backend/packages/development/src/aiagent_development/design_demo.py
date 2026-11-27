"""DEV-02 / DEV-03 Design Picker & Demo Approval validation and sanitization."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from aiagent_domain import canonical_json

from .contracts import InvalidEvidence, ScopeDenied

VALID_DESIGN_POLICIES = frozenset({"AUTO_RECOMMEND", "REQUIRE_USER_SELECTION"})
VALID_DEMO_ACCEPTANCE_POLICIES = frozenset({"REQUIRE_USER_APPROVAL", "PREAUTHORIZED_BOUNDED_AUTO"})
VALID_DEMO_DECISIONS = frozenset({"APPROVED", "REVISION_REQUESTED", "REJECTED"})
_SECRET_LIKE_RE = re.compile(
    r"(?:Bearer\s+[A-Za-z0-9._-]+|sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{8,}|ws://[^\s\"']+/devtools/[^\s\"']+)",
    re.IGNORECASE,
)


def redact_dev_text(text: str, *, limit: int = 500) -> str:
    cleaned = _SECRET_LIKE_RE.sub("[REDACTED]", text)
    return cleaned[:limit]


@dataclass(frozen=True)
class DevelopmentPreferences:
    design_policy: str = "REQUIRE_USER_SELECTION"
    demo_acceptance_policy: str = "REQUIRE_USER_APPROVAL"
    style_candidates: int = 3
    expert_advisor_enabled: bool = False
    review_roles: tuple[str, str, str] = ("functional", "security", "ui-e2e")
    release_policy: str = "PR_AFTER_APPROVED_SCOPE"
    required_checks: tuple[str, ...] = ()

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> DevelopmentPreferences:
        data = raw or {}
        if "demoChoice" in data:
            raise InvalidEvidence("DEPRECATED_DEMO_CHOICE_AUTO: use explicit designPolicy and demoAcceptancePolicy")
        dp = str(data.get("designPolicy") or "REQUIRE_USER_SELECTION")
        dap = str(data.get("demoAcceptancePolicy") or "REQUIRE_USER_APPROVAL")
        if dp not in VALID_DESIGN_POLICIES:
            raise InvalidEvidence(f"invalid designPolicy: {dp}")
        if dap not in VALID_DEMO_ACCEPTANCE_POLICIES:
            raise InvalidEvidence(f"invalid demoAcceptancePolicy: {dap}")

        raw_count = data.get("styleCandidates", 3)
        if isinstance(raw_count, bool) or not isinstance(raw_count, int) or not 1 <= raw_count <= 3:
            raise InvalidEvidence("styleCandidates must be an integer between 1 and 3")

        release = str(data.get("releasePolicy") or "PR_AFTER_APPROVED_SCOPE")
        if release not in ("LOCAL_ONLY", "PR_AFTER_APPROVED_SCOPE", "REQUIRE_PER_ACTION_APPROVAL"):
            raise InvalidEvidence(f"invalid releasePolicy: {release}")

        expert = data.get("expertAdvisorEnabled", False)
        if not isinstance(expert, bool):
            raise InvalidEvidence("expertAdvisorEnabled must be boolean")

        raw_checks = data.get("requiredChecks") or []
        if (
            not isinstance(raw_checks, list)
            or len(raw_checks) > 50
            or any(not isinstance(item, str) or not item.strip() or len(item.strip()) > 128 for item in raw_checks)
        ):
            raise InvalidEvidence("requiredChecks must be a list of non-empty check names")
        req_checks = tuple(dict.fromkeys(item.strip() for item in raw_checks))

        return cls(
            design_policy=dp,
            demo_acceptance_policy=dap,
            style_candidates=raw_count,
            expert_advisor_enabled=expert,
            release_policy=release,
            required_checks=req_checks,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "designPolicy": self.design_policy,
            "demoAcceptancePolicy": self.demo_acceptance_policy,
            "styleCandidates": self.style_candidates,
            "expertAdvisorEnabled": self.expert_advisor_enabled,
            "reviewRoles": list(self.review_roles),
            "releasePolicy": self.release_policy,
            "requiredChecks": list(self.required_checks),
        }


def normalize_design_candidates(
    candidates: Sequence[dict[str, Any]],
    *,
    expected_fixture_ref: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Validate 1..3 candidates built from identical fixture/viewport parameters and sanitize metadata."""
    if not 1 <= len(candidates) <= 3:
        raise InvalidEvidence("must supply between 1 and 3 design candidates")
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    fixture_ref: str | None = expected_fixture_ref

    for item in candidates:
        cid = str(item.get("candidateId") or "").strip()
        label = str(item.get("label") or "").strip()
        fref = str(item.get("fixtureRef") or "").strip()
        if not cid or not label or not fref:
            raise InvalidEvidence("each candidate requires candidateId, label, and fixtureRef")
        if cid in seen_ids:
            raise InvalidEvidence(f"duplicate candidateId: {cid}")
        seen_ids.add(cid)
        if fixture_ref is None:
            fixture_ref = fref
        elif fref != fixture_ref:
            raise ScopeDenied("all design candidates must share identical fixtureRef")

        if item.get("rawHtml") is not None or item.get("script") is not None:
            raise ScopeDenied("raw HTML/script payloads are forbidden in design candidates")

        summary = redact_dev_text(str(item.get("summary") or ""), limit=300)
        desktop_ref = redact_dev_text(str(item.get("desktopThumbRef") or ""), limit=256)
        mobile_ref = redact_dev_text(str(item.get("mobileThumbRef") or ""), limit=256)
        tokens_summary = {
            "theme": str(item.get("theme") or "light"),
            "accent": str(item.get("accent") or "neutral"),
        }
        normalized.append(
            {
                "candidateId": cid,
                "label": redact_dev_text(label, limit=80),
                "fixtureRef": fref,
                "summary": summary,
                "desktopThumbRef": desktop_ref,
                "mobileThumbRef": mobile_ref,
                "viewports": ["desktop:1280x800", "mobile:390x844"],
                "themes": ["light", "dark"],
                "tokens": tokens_summary,
            }
        )

    digest = hashlib.sha256(canonical_json(normalized).encode("utf-8")).hexdigest()
    return normalized, digest
