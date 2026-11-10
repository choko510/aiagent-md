"""Bounded campaign state machine. No provider, DB schema, or Tool Gateway authority lives here.

Store.mutate is a SERIALIZABLE transaction containing the aggregate and its dispatch outbox.
The production PostgreSQL adapter and new Task/Run admission hook belong to AI-07.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Callable, Protocol, TypeVar


class CampaignState(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    WAITING_INPUT = "WAITING_INPUT"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_RECONCILIATION = "WAITING_RECONCILIATION"
    STOP_REQUESTED = "STOP_REQUESTED"
    STOPPED = "STOPPED"
    REVOKED = "REVOKED"
    LIMIT_REACHED = "LIMIT_REACHED"
    TARGET_REACHED = "TARGET_REACHED"


class Phase(StrEnum):
    PLAN = "PLAN"
    EXECUTE = "EXECUTE"
    VERIFY = "VERIFY"
    DONE = "DONE"


class EffectState(StrEnum):
    MAY_HAVE_SENT = "MAY_HAVE_SENT"
    UNKNOWN = "UNKNOWN"
    CONFIRMED = "CONFIRMED"
    NOT_SENT = "NOT_SENT"


class ResultState(StrEnum):
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    FAILED = "FAILED"


class Rejected(ValueError):
    """A rejected admission makes no persistent change."""


@dataclass(frozen=True)
class StopPolicy:
    max_cycles: int
    deadline_utc: str
    max_cost_micro_usd: int
    max_tokens: int
    max_tool_calls: int
    target_count: int | None = None

    def __post_init__(self) -> None:
        if any(not isinstance(n, int) or isinstance(n, bool) or n < 1 for n in (
            self.max_cycles, self.max_cost_micro_usd, self.max_tokens, self.max_tool_calls
        )):
            raise Rejected("FINITE_LIMITS_REQUIRED")
        if self.target_count is not None and (not isinstance(self.target_count, int) or self.target_count < 1):
            raise Rejected("BAD_TARGET")
        parse_utc(self.deadline_utc)


@dataclass(frozen=True)
class CampaignPlan:
    campaign_id: str
    tenant_id: str
    goal_revision: str
    policy: StopPolicy
    cost_per_cycle: int
    tokens_per_cycle: int
    tools_per_cycle: int

    def __post_init__(self) -> None:
        if not self.campaign_id or not self.tenant_id or not self.goal_revision:
            raise Rejected("IDENTITY_REQUIRED")
        for number in (self.cost_per_cycle, self.tokens_per_cycle, self.tools_per_cycle):
            if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                raise Rejected("POSITIVE_RESERVATION_REQUIRED")


@dataclass
class Cycle:
    cycle_id: str
    logical_work_item_id: str
    phase: Phase = Phase.PLAN
    started: bool = False
    result: ResultState | None = None
    evidence_refs: list[str] = field(default_factory=list)
    gate_ref: str | None = None
    usage_finalized: bool = False


@dataclass
class Effect:
    intent_id: str
    cycle_id: str
    target_ref: str
    state: EffectState = EffectState.MAY_HAVE_SENT
    evidence_ref: str | None = None


@dataclass
class Dispatch:
    dedupe_key: str
    cycle_id: str
    control_version: int
    status: str = "PENDING"


@dataclass
class Audit:
    seq: int
    at_utc: str
    reason: str
    state: str
    cycle_id: str | None = None


@dataclass
class Campaign:
    plan: CampaignPlan
    state: CampaignState = CampaignState.ACTIVE
    control_version: int = 1
    cycles: list[Cycle] = field(default_factory=list)
    effects: list[Effect] = field(default_factory=list)
    outbox: list[Dispatch] = field(default_factory=list)
    events: list[Audit] = field(default_factory=list)
    completed_items: list[str] = field(default_factory=list)
    spent_cost: int = 0
    spent_tokens: int = 0
    spent_tools: int = 0
    held_cost: int = 0
    held_tokens: int = 0
    held_tools: int = 0
    authorized: bool = True
    pending_approval: bool = False
    control_receipts: dict[str, tuple[str, int]] = field(default_factory=dict)

    def wire(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_wire(cls, value: dict[str, object]) -> Campaign:
        """Deserialize a trusted store record (not external requests)."""
        raw_plan = dict(value["plan"])  # type: ignore[arg-type]
        policy = StopPolicy(**dict(raw_plan.pop("policy")))  # type: ignore[arg-type]
        plan = CampaignPlan(policy=policy, **raw_plan)  # type: ignore[arg-type]
        rest = {k: v for k, v in value.items() if k != "plan"}
        rest["cycles"] = [Cycle(**x) for x in rest["cycles"]]  # type: ignore[union-attr]
        rest["effects"] = [Effect(**x) for x in rest["effects"]]  # type: ignore[union-attr]
        rest["outbox"] = [Dispatch(**x) for x in rest["outbox"]]  # type: ignore[union-attr]
        rest["events"] = [Audit(**x) for x in rest["events"]]  # type: ignore[union-attr]
        return cls(plan=plan, **rest)  # type: ignore[arg-type]


T = TypeVar("T")


class Store(Protocol):
    def create(self, campaign: Campaign) -> None: ...
    def read(self, campaign_id: str) -> Campaign: ...
    def mutate(self, campaign_id: str, fn: Callable[[Campaign], T]) -> T: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class QueueDispatch(Protocol):
    def request_run(self, *, tenant_id: str, campaign_id: str, cycle_id: str,
                    dedupe_key: str, control_version: int) -> bool:
        """Return true only after atomic admission against campaign control_version.

        AI-07 MUST re-check auth/policy/control_version under the same DB lock as
        create_task_tx + run.start outbox. A preflight check here is insufficient.
        """
        ...


class VerificationPort(Protocol):
    def passes(self, *, campaign_id: str, cycle_id: str, evidence_refs: list[str],
               quality_gate_ref: str) -> bool: ...


class DenyUntrustedVerification:
    def passes(self, *, campaign_id: str, cycle_id: str, evidence_refs: list[str],
               quality_gate_ref: str) -> bool:
        return False


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass
class FakeClock:
    current: datetime

    def now(self) -> datetime:
        return self.current

    def advance(self, **kwargs: int) -> None:
        from datetime import timedelta
        self.current += timedelta(**kwargs)


def parse_utc(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise Rejected("INVALID_DEADLINE") from error
    if dt.tzinfo is None or dt.utcoffset() != UTC.utcoffset(dt):
        raise Rejected("UTC_DEADLINE_REQUIRED")
    return dt.astimezone(UTC)


class CampaignController:
    def __init__(self, store: Store, clock: Clock, queue: QueueDispatch,
                 verifier: VerificationPort | None = None) -> None:
        self.store, self.clock, self.queue = store, clock, queue
        self.verifier = verifier or DenyUntrustedVerification()

    def _event(self, campaign: Campaign, reason: str, cycle: Cycle | None = None) -> None:
        campaign.events.append(Audit(len(campaign.events) + 1, self.clock.now().isoformat(),
                                     reason, str(campaign.state), cycle.cycle_id if cycle else None))

    def _gate(self, c: Campaign, expected_version: int | None = None) -> None:
        if expected_version is not None and expected_version != c.control_version:
            raise Rejected("STALE_CONTROL_VERSION")
        if not c.authorized:
            raise Rejected("AUTH_REVOKED")
        if c.state != CampaignState.ACTIVE:
            raise Rejected(f"CAMPAIGN_{c.state}")
        if self.clock.now() >= parse_utc(c.plan.policy.deadline_utc):
            c.state = CampaignState.LIMIT_REACHED
            self._event(c, "DEADLINE_REACHED")
            raise _CommitRejection("DEADLINE_REACHED")
        if c.pending_approval:
            c.state = CampaignState.WAITING_APPROVAL
            self._event(c, "APPROVAL_PENDING")
            raise _CommitRejection("APPROVAL_PENDING")
        if any(e.state in (EffectState.MAY_HAVE_SENT, EffectState.UNKNOWN) for e in c.effects):
            c.state = CampaignState.WAITING_RECONCILIATION
            self._event(c, "EFFECT_BARRIER")
            raise _CommitRejection("EFFECT_BARRIER")

    def _reserve(self, c: Campaign, cycle_id: str, work_id: str) -> Cycle | None:
        p = c.plan
        if len(c.cycles) >= p.policy.max_cycles:
            c.state = CampaignState.LIMIT_REACHED
            self._event(c, "MAX_CYCLES_REACHED")
            return None
        if (c.spent_cost + c.held_cost + p.cost_per_cycle > p.policy.max_cost_micro_usd or
            c.spent_tokens + c.held_tokens + p.tokens_per_cycle > p.policy.max_tokens or
            c.spent_tools + c.held_tools + p.tools_per_cycle > p.policy.max_tool_calls):
            c.state = CampaignState.LIMIT_REACHED
            self._event(c, "INSUFFICIENT_BUDGET")
            return None
        if any(x.logical_work_item_id == work_id for x in c.cycles):
            c.state = CampaignState.WAITING_INPUT
            self._event(c, "DUPLICATE_WORK_ITEM")
            return None
        cycle = Cycle(cycle_id, work_id)
        c.cycles.append(cycle)
        c.held_cost += p.cost_per_cycle
        c.held_tokens += p.tokens_per_cycle
        c.held_tools += p.tools_per_cycle
        c.outbox.append(Dispatch(f"campaign:{p.campaign_id}:cycle:{cycle_id}:start", cycle_id, c.control_version))
        self._event(c, "CYCLE_RESERVED", cycle)
        return cycle

    def create(self, plan: CampaignPlan, first_work_item_id: str) -> None:
        if not first_work_item_id:
            raise Rejected("WORK_ITEM_REQUIRED")
        if self.clock.now() >= parse_utc(plan.policy.deadline_utc):
            raise Rejected("DEADLINE_REACHED")
        c = Campaign(plan=plan)
        self._reserve(c, f"{plan.campaign_id}:1", first_work_item_id)
        self.store.create(c)

    def dispatch_pending(self, campaign_id: str) -> int:
        """Outbox relay; duplicates across crash are expected and MUST be deduped by Queue/Task admission."""
        sent = 0
        for item in self.store.read(campaign_id).outbox:
            if item.status != "PENDING":
                continue
            def allowed(c: Campaign, item: Dispatch = item) -> bool:
                try:
                    self._gate(c, item.control_version)
                except (Rejected, _CommitRejection):
                    return False
                return any(x.dedupe_key == item.dedupe_key and x.status == "PENDING" for x in c.outbox)
            if not self.store.mutate(campaign_id, allowed):
                continue
            # This call must be a trusted Task/Run intent; it MUST NOT itself send external effects.
            accepted = self.queue.request_run(tenant_id=self.store.read(campaign_id).plan.tenant_id,
                                              campaign_id=campaign_id, cycle_id=item.cycle_id,
                                              dedupe_key=item.dedupe_key, control_version=item.control_version)
            if not accepted:
                continue
            def ack(c: Campaign, item: Dispatch = item) -> None:
                for pending in c.outbox:
                    if pending.dedupe_key == item.dedupe_key:
                        pending.status = "SENT"
            self.store.mutate(campaign_id, ack)
            sent += 1
        return sent

    def admit_cycle(self, campaign_id: str, cycle_id: str, expected_control_version: int) -> bool:
        """Admit once; a redelivery resumes the same logical cycle, not a new child/effect."""
        def op(c: Campaign) -> bool:
            self._gate(c, expected_control_version)
            cycle = self._cycle(c, cycle_id)
            if cycle.started:
                return False
            if any(x.started and x.phase != Phase.DONE for x in c.cycles if x.cycle_id != cycle_id):
                raise Rejected("ACTIVE_CYCLE_EXISTS")
            cycle.started = True
            self._event(c, "CYCLE_ADMITTED", cycle)
            return True
        return self._mutate(campaign_id, op)

    @staticmethod
    def _cycle(c: Campaign, cycle_id: str) -> Cycle:
        return next((v for v in c.cycles if v.cycle_id == cycle_id), None) or _missing_cycle()

    def advance(self, campaign_id: str, cycle_id: str, from_phase: Phase, to_phase: Phase,
                expected_control_version: int) -> bool:
        if (from_phase, to_phase) not in ((Phase.PLAN, Phase.EXECUTE), (Phase.EXECUTE, Phase.VERIFY)):
            raise Rejected("INVALID_PHASE_TRANSITION")
        def op(c: Campaign) -> bool:
            self._gate(c, expected_control_version)
            cycle = self._cycle(c, cycle_id)
            if not cycle.started:
                raise Rejected("CYCLE_NOT_ADMITTED")
            if cycle.phase == to_phase:
                return False
            if cycle.phase != from_phase:
                raise Rejected("INVALID_PHASE_TRANSITION")
            cycle.phase = to_phase
            self._event(c, f"PHASE_{to_phase}", cycle)
            return True
        return self._mutate(campaign_id, op)

    def admit_effect(self, campaign_id: str, cycle_id: str, intent_id: str, target_ref: str,
                     expected_control_version: int) -> bool:
        if not intent_id or not target_ref:
            raise Rejected("EFFECT_ID_REQUIRED")
        def op(c: Campaign) -> bool:
            # Duplicate identity must never become a second external send, even if state is CONFIRMED.
            if any(e.intent_id == intent_id for e in c.effects):
                existing = next(e for e in c.effects if e.intent_id == intent_id)
                if existing.cycle_id != cycle_id or existing.target_ref != target_ref:
                    raise Rejected("INTENT_ID_CONFLICT")
                return False
            self._gate(c, expected_control_version)
            cycle = self._cycle(c, cycle_id)
            if cycle.phase != Phase.EXECUTE or not cycle.started:
                raise Rejected("NOT_EXECUTING")
            c.effects.append(Effect(intent_id, cycle_id, target_ref))
            self._event(c, "EFFECT_MAY_HAVE_SENT", cycle)
            return True
        return self._mutate(campaign_id, op)

    def record_effect(self, campaign_id: str, intent_id: str, state: EffectState,
                      evidence_ref: str | None = None) -> None:
        if state not in (EffectState.CONFIRMED, EffectState.UNKNOWN, EffectState.NOT_SENT):
            raise Rejected("INVALID_EFFECT_RESULT")
        if state != EffectState.UNKNOWN and not evidence_ref:
            raise Rejected("EVIDENCE_REQUIRED")
        def op(c: Campaign) -> None:
            effect = next((e for e in c.effects if e.intent_id == intent_id), None)
            if effect is None:
                raise Rejected("EFFECT_NOT_FOUND")
            if effect.state not in (EffectState.MAY_HAVE_SENT, EffectState.UNKNOWN):
                if effect.state != state or effect.evidence_ref != evidence_ref:
                    raise Rejected("EFFECT_ALREADY_FINAL")
                return
            effect.state, effect.evidence_ref = state, evidence_ref
            barrier = any(e.state in (EffectState.UNKNOWN, EffectState.MAY_HAVE_SENT) for e in c.effects)
            if c.state == CampaignState.STOP_REQUESTED:
                if not barrier:
                    c.state = CampaignState.STOPPED
            elif state == EffectState.UNKNOWN:
                c.state = CampaignState.WAITING_RECONCILIATION
            elif c.state == CampaignState.WAITING_RECONCILIATION and not barrier and c.authorized:
                c.state = CampaignState.PAUSED  # explicit authenticated resume required
            self._event(c, f"EFFECT_{state}")
        self.store.mutate(campaign_id, op)

    def complete_cycle(self, campaign_id: str, cycle_id: str, *, claimed_success: bool,
                       evidence_refs: list[str], quality_gate_ref: str | None,
                       cost_micro_usd: int | None, tokens: int | None, tools: int | None,
                       next_work_item_id: str | None = None) -> ResultState:
        def op(c: Campaign) -> ResultState:
            self._gate(c)
            cycle = self._cycle(c, cycle_id)
            if cycle.phase == Phase.DONE:
                return cycle.result or ResultState.UNVERIFIED
            if cycle.phase != Phase.VERIFY:
                raise Rejected("NOT_VERIFYING")
            if any(e.state in (EffectState.MAY_HAVE_SENT, EffectState.UNKNOWN) and e.cycle_id == cycle_id
                   for e in c.effects):
                raise Rejected("EFFECT_BARRIER")
            receipt_valid = all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in (
                cost_micro_usd, tokens, tools))
            if receipt_valid:
                assert cost_micro_usd is not None and tokens is not None and tools is not None
                if (cost_micro_usd > c.plan.cost_per_cycle or tokens > c.plan.tokens_per_cycle or
                    tools > c.plan.tools_per_cycle):
                    raise Rejected("USAGE_OVER_RESERVATION")
                c.spent_cost += cost_micro_usd
                c.spent_tokens += tokens
                c.spent_tools += tools
                c.held_cost -= c.plan.cost_per_cycle
                c.held_tokens -= c.plan.tokens_per_cycle
                c.held_tools -= c.plan.tools_per_cycle
            # Unknown usage remains held, and new cycles cannot begin.
            cycle.usage_finalized = receipt_valid
            passed = bool(claimed_success and quality_gate_ref and evidence_refs and receipt_valid and
                          self.verifier.passes(campaign_id=campaign_id, cycle_id=cycle_id,
                                               evidence_refs=evidence_refs, quality_gate_ref=quality_gate_ref))
            cycle.result = (ResultState.VERIFIED if passed
                            else ResultState.UNVERIFIED if claimed_success else ResultState.FAILED)
            cycle.evidence_refs = list(evidence_refs)
            cycle.gate_ref = quality_gate_ref
            cycle.phase = Phase.DONE
            if cycle.result == ResultState.VERIFIED and cycle.logical_work_item_id not in c.completed_items:
                c.completed_items.append(cycle.logical_work_item_id)
            self._event(c, f"CYCLE_{cycle.result}", cycle)
            if not receipt_valid:
                c.state = CampaignState.WAITING_RECONCILIATION
                self._event(c, "UNKNOWN_USAGE_HELD", cycle)
            elif c.plan.policy.target_count is not None and len(c.completed_items) >= c.plan.policy.target_count:
                c.state = CampaignState.TARGET_REACHED
                self._event(c, "TARGET_REACHED", cycle)
            elif c.state == CampaignState.ACTIVE:
                if not next_work_item_id:
                    c.state = CampaignState.WAITING_INPUT
                    self._event(c, "NO_NEXT_WORK", cycle)
                else:
                    self._reserve(c, f"{c.plan.campaign_id}:{len(c.cycles) + 1}", next_work_item_id)
            return cycle.result
        return self._mutate(campaign_id, op)

    def control(self, campaign_id: str, action: str, expected_control_version: int,
                command_id: str | None = None) -> int:
        if action not in {"PAUSE", "RESUME", "STOP", "REVOKE", "APPROVAL_PENDING", "APPROVAL_GRANTED"}:
            raise Rejected("BAD_CONTROL")
        def op(c: Campaign) -> int:
            if command_id and command_id in c.control_receipts:
                prior_action, prior_version = c.control_receipts[command_id]
                if prior_action != action:
                    raise Rejected("CONTROL_COMMAND_CONFLICT")
                return prior_version
            if expected_control_version != c.control_version:
                raise Rejected("STALE_CONTROL_VERSION")
            if action == "REVOKE":
                c.authorized, c.state = False, CampaignState.REVOKED
            elif action == "STOP":
                c.state = (CampaignState.STOP_REQUESTED if any(e.state in (
                    EffectState.MAY_HAVE_SENT, EffectState.UNKNOWN) for e in c.effects) else CampaignState.STOPPED)
            elif action == "PAUSE":
                if c.state != CampaignState.ACTIVE:
                    raise Rejected("NOT_PAUSABLE")
                c.state = CampaignState.PAUSED
            elif action == "RESUME":
                if not c.authorized or c.state not in (CampaignState.PAUSED, CampaignState.WAITING_INPUT,
                                                        CampaignState.WAITING_APPROVAL):
                    raise Rejected("NOT_RESUMABLE")
                if c.pending_approval or any(e.state in (EffectState.MAY_HAVE_SENT, EffectState.UNKNOWN)
                                             for e in c.effects):
                    raise Rejected("BLOCKED_RESUME")
                if self.clock.now() >= parse_utc(c.plan.policy.deadline_utc):
                    c.state = CampaignState.LIMIT_REACHED
                else:
                    c.state = CampaignState.ACTIVE
            elif action == "APPROVAL_PENDING":
                c.pending_approval, c.state = True, CampaignState.WAITING_APPROVAL
            elif action == "APPROVAL_GRANTED":
                c.pending_approval = False
                if c.state == CampaignState.WAITING_APPROVAL:
                    c.state = CampaignState.PAUSED
            c.control_version += 1
            if action in ("STOP", "REVOKE", "PAUSE", "APPROVAL_PENDING"):
                for item in c.outbox:
                    if item.status == "PENDING":
                        item.status = "CANCELLED"
            if action == "RESUME":
                for cycle in c.cycles:
                    if cycle.phase != Phase.DONE:
                        key = f"campaign:{c.plan.campaign_id}:cycle:{cycle.cycle_id}:resume:v{c.control_version}"
                        c.outbox.append(Dispatch(key, cycle.cycle_id, c.control_version))
            if command_id:
                c.control_receipts[command_id] = (action, c.control_version)
            self._event(c, action)
            return c.control_version
        return self.store.mutate(campaign_id, op)

    def view(self, campaign_id: str) -> dict[str, object]:
        c = self.store.read(campaign_id)
        return {"campaignId": c.plan.campaign_id, "tenantId": c.plan.tenant_id,
                "state": str(c.state), "controlVersion": c.control_version,
                "completedCount": len(c.completed_items), "cycleCount": len(c.cycles),
                "spentMicroUsd": str(c.spent_cost), "heldMicroUsd": str(c.held_cost),
                "remainingMicroUsd": str(max(0, c.plan.policy.max_cost_micro_usd - c.spent_cost - c.held_cost)),
                "cycles": [{"cycleId": x.cycle_id, "phase": str(x.phase),
                            "result": str(x.result) if x.result else None, "evidenceRefs": x.evidence_refs}
                           for x in c.cycles],
                "audit": [asdict(x) for x in c.events]}

    def _mutate(self, campaign_id: str, fn: Callable[[Campaign], T]) -> T:
        try:
            return self.store.mutate(campaign_id, fn)
        except _CommitRejection as exc:
            # This sentinel must be transformed by Store AFTER committing the blocked state.
            raise Rejected(str(exc)) from exc


class _CommitRejection(Exception):
    pass


def _missing_cycle() -> Cycle:
    raise Rejected("CYCLE_NOT_FOUND")
