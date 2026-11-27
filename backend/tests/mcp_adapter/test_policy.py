from __future__ import annotations

from aiagent_mcp_adapter.policy import PolicySet

from .helpers import descriptor


def test_policy_record_is_scoped_to_its_provider() -> None:
    desc = descriptor()
    approved = PolicySet(provider_id="provider-a", revision=1, allowlist=frozenset({"echo"})).approve(
        desc, risk_class="read", approval_required=False, actor="admin"
    )
    transplanted = PolicySet(
        provider_id="provider-b",
        revision=approved.revision,
        allowlist=approved.allowlist,
        policies=approved._policies,
    )
    decision = transplanted.decide(desc)
    assert decision.policy is None
    assert decision.reason == "provider_mismatch"


def test_policy_requires_a_named_approver() -> None:
    desc = descriptor()
    approved = PolicySet(provider_id="provider-a", revision=1, allowlist=frozenset({"echo"})).approve(
        desc, risk_class="read", approval_required=False, actor="admin"
    )
    record = approved._policies["echo"]
    no_actor = PolicySet(
        provider_id="provider-a",
        revision=approved.revision,
        allowlist=approved.allowlist,
        policies={"echo": type(record)(**{**record.__dict__, "approved_by": " "})},
    )
    assert no_actor.decide(desc).reason == "unclassified"


def test_allowlist_and_policy_must_both_match() -> None:
    desc = descriptor()
    approved = PolicySet(provider_id="provider-a", revision=1, allowlist=frozenset({"echo"})).approve(
        desc, risk_class="read", approval_required=False, actor="admin"
    )
    narrowed = approved.with_allowlist(("other",))
    decision = narrowed.decide(desc)
    assert decision.policy is None
    assert decision.reason == "not_allowlisted"
