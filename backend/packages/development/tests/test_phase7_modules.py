"""Isolated offline contract tests for Phase 7 Development modules (DEV-02/03/11/12/14).

Runs under both isolated `aiagent-development` contract CI (without Postgres/Core) and full backend pytest.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from aiagent_development.adaptive_testing import AdaptiveTestingGate, AdaptiveTestPlan, plan_adaptive_tests
from aiagent_development.code_intelligence.git_reader import open_snapshot
from aiagent_development.code_intelligence.inventory import inventory
from aiagent_development.code_intelligence.models import AuthorizedSnapshot, Identity
from aiagent_development.contracts import (
    ExecutionEvidence,
    ExecutionRequest,
    InvalidEvidence,
    ProjectScope,
    RunFence,
    ScopeDenied,
    TaskPlan,
)
from aiagent_development.design_demo import DevelopmentPreferences, normalize_design_candidates, redact_dev_text
from aiagent_development.pr_guardian import PrGuardian, RequiredCheckRule, evaluate_pr_checks
from aiagent_development.reproduction import ReproductionLab


def _git(repo: Path, *args: str) -> str:
    git_bin = shutil.which("git")
    assert git_bin is not None
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", os.environ.get("USERPROFILE", "")),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    proc = subprocess.run(
        [git_bin, "-C", str(repo), "-c", "core.fsmonitor=false", *args],
        capture_output=True,
        env=env,
        check=True,
    )
    return proc.stdout.decode("utf-8").strip()


def _make_git_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    (repo / "pkg_a").mkdir(parents=True)
    (repo / "pkg_b").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    (repo / "pkg_a" / "app.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")
    (repo / "tests" / "test_app.py").write_text(
        "from pkg_a import app\n\ndef test_add():\n    assert app.add(1, 2) == 3\n", encoding="utf-8"
    )
    (repo / "pkg_b" / "other.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests" / "test_other.py").write_text(
        "from pkg_b import other\n\ndef test_other():\n    assert other.VALUE == 1\n", encoding="utf-8"
    )
    (repo / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    sha = _git(repo, "rev-parse", "HEAD")
    return repo, sha


def test_dev14_adaptive_testing_separates_impacted_and_mandatory_and_falls_back(tmp_path: Path) -> None:
    """DEV-14 / P2-12: fail-closed TEXT_ONLY fallback, separate static command, authoritative TaskPlan, evidence validation."""
    repo, sha = _make_git_repo(tmp_path)
    scope = ProjectScope("t1", "u1", "p1", "w1", "org/demo", "main", sha, "1")
    run = RunFence("task-1", "run-1", 1, 1, 0)
    plan = TaskPlan("plan-1", "cr-1", ("test_security_baseline.py", "static:pkg_a"), 1)
    tc_digest = hashlib.sha256(b"toolchain-v1").hexdigest()

    git_bin = shutil.which("git")
    assert git_bin is not None
    opened = open_snapshot(
        AuthorizedSnapshot(Identity("t1", "u1", "b1", "1", sha), repo.resolve()),
        Path(git_bin).resolve(),
    )
    assert opened.reader is not None
    reader = opened.reader
    inv = inventory(reader)

    # 1. Default allow_text_only_narrowing=False falls back to full suite when impact coverage is TEXT_ONLY (P2-12)
    conservative_plan = plan_adaptive_tests(reader, inv, ("pkg_a/app.py",), plan)
    assert conservative_plan.fallback_to_full is True
    assert "coverage:TEXT_ONLY" in conservative_plan.fallback_reasons
    assert set(conservative_plan.impacted_tests) == {"tests/test_app.py", "tests/test_other.py"}
    assert conservative_plan.mandatory_tests == ("test_security_baseline.py", "static:pkg_a")

    # 2. Explicit allow_text_only_narrowing=True narrows pure Python source change to tests/test_app.py only
    ad_plan = plan_adaptive_tests(reader, inv, ("pkg_a/app.py",), plan, allow_text_only_narrowing=True)
    assert ad_plan.fallback_to_full is False
    assert ad_plan.impacted_tests == ("tests/test_app.py",)
    assert ad_plan.mandatory_tests == ("test_security_baseline.py", "static:pkg_a")

    # 3. Manifest / config change triggers conservative fallback even with allow_text_only_narrowing=True
    fb_plan = plan_adaptive_tests(reader, inv, ("pyproject.toml",), plan, allow_text_only_narrowing=True)
    assert fb_plan.fallback_to_full is True
    assert set(fb_plan.impacted_tests) == {"tests/test_app.py", "tests/test_other.py"}

    # 4. Deleted/missing file triggers conservative fallback
    del_plan = plan_adaptive_tests(reader, inv, ("deleted_file.py",), plan, allow_text_only_narrowing=True)
    assert del_plan.fallback_to_full is True
    assert set(del_plan.impacted_tests) == {"tests/test_app.py", "tests/test_other.py"}

    # 5. Impacted PASS with mandatory SKIPPED MUST NOT report overall_passed=True
    recorded_reqs: list[ExecutionRequest] = []

    class _RecordingSandbox:
        def execute(self, request: ExecutionRequest) -> ExecutionEvidence:
            recorded_reqs.append(request)
            return ExecutionEvidence(
                intent_ref=request.intent_ref,
                run=request.run,
                kind=request.kind,
                exit_code=0,
                executed=True,
                artifact_sha256=tc_digest,
                toolchain_digest=request.toolchain_digest,
                stdout="ok",
                stderr="",
                source_commit_sha=request.target_commit_sha,
                test_id=request.test_id,
                receipt_ref=f"rcpt:{request.intent_ref}",
                timed_out=False,
                truncated=False,
            )

    # R3-09: Default AdaptiveTestingGate WITHOUT receipt_verifier MUST fail closed!
    res_unverified = AdaptiveTestingGate(_RecordingSandbox()).run_gate(
        scope=scope,
        run=run,
        plan=plan,
        adaptive_plan=ad_plan,
        toolchain_digest=tc_digest,
        intent_prefix="g0-no-verifier",
        target_commit_sha=sha,
    )
    assert res_unverified.impacted_status == "FAILED" and res_unverified.overall_passed is False

    def _valid_rcpt(req: ExecutionRequest, ev: ExecutionEvidence) -> bool:
        return ev.receipt_ref == f"rcpt:{req.intent_ref}"

    gate = AdaptiveTestingGate(_RecordingSandbox(), receipt_verifier=_valid_rcpt)
    res_skip = gate.run_gate(
        scope=scope,
        run=run,
        plan=plan,
        adaptive_plan=ad_plan,
        toolchain_digest=tc_digest,
        intent_prefix="g1",
        target_commit_sha=sha,
        run_mandatory=False,
    )
    assert res_skip.impacted_status == "PASSED"
    assert res_skip.mandatory_status == "SKIPPED"
    assert res_skip.overall_passed is False

    # 6. Separate test_command vs static_command AND authoritative TaskPlan.required_tests (tampered adaptive_plan ignored)
    recorded_reqs.clear()
    tampered_plan = AdaptiveTestPlan(
        changed_paths=ad_plan.changed_paths,
        impacted_tests=ad_plan.impacted_tests,
        mandatory_tests=(),  # Tampered empty list MUST be ignored in favor of TaskPlan.required_tests
        fallback_to_full=False,
        fallback_reasons=(),
    )
    res_ok = AdaptiveTestingGate(_RecordingSandbox(), receipt_verifier=_valid_rcpt).run_gate(
        scope=scope,
        run=run,
        plan=plan,
        adaptive_plan=tampered_plan,
        toolchain_digest=tc_digest,
        intent_prefix="g2",
        target_commit_sha=sha,
        run_mandatory=True,
    )
    assert res_ok.impacted_status == "PASSED"
    assert res_ok.mandatory_status == "PASSED"
    assert res_ok.overall_passed is True
    assert res_ok.plan.mandatory_tests == ("test_security_baseline.py", "static:pkg_a")
    static_reqs = [r for r in recorded_reqs if r.kind == "static-check"]
    assert len(static_reqs) == 1
    assert static_reqs[0].argv == ("ruff", "check", "pkg_a")
    assert static_reqs[0].test_id is None

    # 7. Truncated, wrong-commit, wrong-test, wrong-RunFence, or unverified-receipt evidence fails closed (P2-12/R2-12)
    other_run = RunFence("task-1", "run-1", 1, 2, 0)

    class _MismatchedSandbox:
        def __init__(
            self,
            *,
            truncate: bool = False,
            wrong_commit: bool = False,
            wrong_test: bool = False,
            wrong_run: bool = False,
        ) -> None:
            self.truncate = truncate
            self.wrong_commit = wrong_commit
            self.wrong_test = wrong_test
            self.wrong_run = wrong_run

        def execute(self, request: ExecutionRequest) -> ExecutionEvidence:
            return ExecutionEvidence(
                intent_ref=request.intent_ref,
                run=other_run if self.wrong_run else request.run,
                kind=request.kind,
                exit_code=0,
                executed=True,
                artifact_sha256=tc_digest,
                toolchain_digest=request.toolchain_digest,
                stdout="clipped",
                stderr="",
                source_commit_sha=("f" * 40) if self.wrong_commit else sha,
                test_id="other_test" if self.wrong_test else request.test_id,
                receipt_ref=f"rcpt:{request.intent_ref}",
                timed_out=False,
                truncated=self.truncate,
            )

    for bad_sb in (
        _MismatchedSandbox(truncate=True),
        _MismatchedSandbox(wrong_commit=True),
        _MismatchedSandbox(wrong_test=True),
        _MismatchedSandbox(wrong_run=True),
    ):
        res_bad = AdaptiveTestingGate(bad_sb, receipt_verifier=_valid_rcpt).run_gate(
            scope=scope,
            run=run,
            plan=plan,
            adaptive_plan=ad_plan,
            toolchain_digest=tc_digest,
            intent_prefix="g3",
            target_commit_sha=sha,
        )
        assert res_bad.impacted_status == "FAILED"
        assert res_bad.overall_passed is False

    res_bad_receipt = AdaptiveTestingGate(_RecordingSandbox(), receipt_verifier=lambda _req, _ev: False).run_gate(
        scope=scope,
        run=run,
        plan=plan,
        adaptive_plan=ad_plan,
        toolchain_digest=tc_digest,
        intent_prefix="g4",
        target_commit_sha=sha,
    )
    assert res_bad_receipt.impacted_status == "FAILED" and res_bad_receipt.overall_passed is False


def test_dev12_reproduction_lab_enforces_fail_pass_pair_and_detects_flaky() -> None:
    """DEV-12 / P1-08: strict pair binding, duplicate receipt rejection, pytest collection error rejection, flake check."""
    sha_pre = "a" * 40
    sha_post = "b" * 40
    scope = ProjectScope("t1", "u1", "p1", "w1", "org/demo", "main", sha_pre, "1")
    run = RunFence("task-1", "run-1", 1, 1, 0)
    tc_digest = hashlib.sha256(b"tc").hexdigest()

    pre_req = ExecutionRequest(
        scope, run, "plan-1", "test", ("pytest", "test_bug.py"), tc_digest, "repro-pre", "test_bug", sha_pre
    )
    post_req = ExecutionRequest(
        scope, run, "plan-1", "test", ("pytest", "test_bug.py"), tc_digest, "repro-post", "test_bug", sha_post
    )

    class _CommitAwareSandbox:
        def __init__(self, codes: dict[str, int]) -> None:
            self.codes = codes

        def execute(self, request: ExecutionRequest) -> ExecutionEvidence:
            return ExecutionEvidence(
                intent_ref=request.intent_ref,
                run=request.run,
                kind=request.kind,
                exit_code=self.codes.get(request.intent_ref, 0),
                executed=True,
                artifact_sha256=hashlib.sha256(
                    f"{request.intent_ref}:{request.target_commit_sha}".encode()
                ).hexdigest(),
                toolchain_digest=request.toolchain_digest,
                stdout="",
                stderr="",
                source_commit_sha=request.target_commit_sha,
                test_id=request.test_id,
                receipt_ref=f"rcpt:{request.intent_ref}",
                timed_out=False,
                truncated=False,
            )

    # 0. R3-09: ReproductionLab WITHOUT receipt_verifier MUST fail closed!
    sb = _CommitAwareSandbox({"repro-pre": 1, "repro-post": 0})
    with pytest.raises(InvalidEvidence, match="EXECUTION_RECEIPT_VERIFIER_REQUIRED"):
        ReproductionLab(sb).verify_pair(test_id="test_bug", pre_request=pre_req, post_request=post_req)

    def _ok_verifier(req: ExecutionRequest, ev: ExecutionEvidence) -> bool:
        return bool(ev.receipt_ref) and ev.intent_ref == req.intent_ref

    # 1. Valid pre-FAIL (exit_code=1) + post-PASS (exit_code=0) pair with verified receipts
    proof = ReproductionLab(sb, receipt_verifier=_ok_verifier).verify_pair(
        test_id="test_bug", pre_request=pre_req, post_request=post_req
    )
    assert proof.verified is True
    assert proof.flaky is False
    assert proof.reason == "VERIFIED"
    assert proof.before_fail_digest is not None and proof.after_pass_digest is not None

    # 2. P1-08: Reject mismatched test_id, mismatched argv, or identical pre==post commit SHA
    wrong_test_post = ExecutionRequest(
        scope, run, "plan-1", "test", ("pytest", "test_bug.py"), tc_digest, "repro-post", "other_test", sha_post
    )
    with pytest.raises(InvalidEvidence, match="test_id"):
        ReproductionLab(sb, receipt_verifier=_ok_verifier).verify_pair(
            test_id="test_bug", pre_request=pre_req, post_request=wrong_test_post
        )

    wrong_argv_post = ExecutionRequest(
        scope, run, "plan-1", "test", ("pytest", "test_other.py"), tc_digest, "repro-post", "test_bug", sha_post
    )
    with pytest.raises(InvalidEvidence, match="identical test command"):
        ReproductionLab(sb, receipt_verifier=_ok_verifier).verify_pair(
            test_id="test_bug", pre_request=pre_req, post_request=wrong_argv_post
        )

    same_sha_post = ExecutionRequest(
        scope, run, "plan-1", "test", ("pytest", "test_bug.py"), tc_digest, "repro-post", "test_bug", sha_pre
    )
    with pytest.raises(InvalidEvidence, match="must differ"):
        ReproductionLab(sb, receipt_verifier=_ok_verifier).verify_pair(
            test_id="test_bug", pre_request=pre_req, post_request=same_sha_post
        )

    # R2-12: Reject mismatched RunFence between pre/post or returned evidence, and unverified receipts
    other_run = RunFence("task-1", "run-1", 1, 2, 0)
    wrong_fence_post = ExecutionRequest(
        scope, other_run, "plan-1", "test", ("pytest", "test_bug.py"), tc_digest, "repro-post", "test_bug", sha_post
    )
    with pytest.raises(InvalidEvidence, match="RunFence"):
        ReproductionLab(sb, receipt_verifier=_ok_verifier).verify_pair(
            test_id="test_bug", pre_request=pre_req, post_request=wrong_fence_post
        )

    with pytest.raises(InvalidEvidence, match="mismatched request bindings"):
        ReproductionLab(sb, receipt_verifier=lambda _req, _ev: False).verify_pair(
            test_id="test_bug", pre_request=pre_req, post_request=post_req
        )

    # 3. P1-08: Pre-fix pytest collection/environment error (exit_code=5) MUST NOT count as bug reproduction
    sb_env = _CommitAwareSandbox({"repro-pre": 5, "repro-post": 0})
    p_env = ReproductionLab(sb_env, receipt_verifier=_ok_verifier).verify_pair(
        test_id="test_bug", pre_request=pre_req, post_request=post_req
    )
    assert p_env.verified is False and p_env.reason == "PRE_FIX_ENV_OR_COLLECTION_ERROR"

    # 4. Pre-fix already passing -> rejected
    sb2 = _CommitAwareSandbox({"repro-pre": 0, "repro-post": 0})
    p_nofail = ReproductionLab(sb2, receipt_verifier=_ok_verifier).verify_pair(
        test_id="test_bug", pre_request=pre_req, post_request=post_req
    )
    assert p_nofail.verified is False and p_nofail.reason == "PRE_FIX_DID_NOT_FAIL"

    # 5. P1-08: Duplicate receipt_ref reused across repeat_count=2 is rejected
    class _DupReceiptSandbox:
        def execute(self, request: ExecutionRequest) -> ExecutionEvidence:
            is_pre = request.intent_ref.startswith("repro-pre")
            return ExecutionEvidence(
                intent_ref=request.intent_ref,
                run=request.run,
                kind=request.kind,
                exit_code=1 if is_pre else 0,
                executed=True,
                artifact_sha256=tc_digest,
                toolchain_digest=request.toolchain_digest,
                stdout="",
                stderr="",
                source_commit_sha=request.target_commit_sha,
                test_id=request.test_id,
                receipt_ref="dup-static-receipt",
                timed_out=False,
                truncated=False,
            )

    p_dup = ReproductionLab(_DupReceiptSandbox(), receipt_verifier=_ok_verifier).verify_pair(
        test_id="test_bug", pre_request=pre_req, post_request=post_req, repeat_count=2
    )
    assert p_dup.verified is False and p_dup.reason == "DUPLICATE_EXECUTION_RECEIPT"

    # 6. Flaky post-fix test (alternates 0 and 1 across repeat_count=2 with distinct intent_ref & receipt_ref)
    class _FlakySandbox:
        def __init__(self) -> None:
            self.seen_intents: list[str] = []
            self.post_calls = 0

        def execute(self, request: ExecutionRequest) -> ExecutionEvidence:
            self.seen_intents.append(request.intent_ref)
            if request.intent_ref.startswith("repro-pre"):
                code = 1
            else:
                code = 0 if self.post_calls == 0 else 1
                self.post_calls += 1
            return ExecutionEvidence(
                intent_ref=request.intent_ref,
                run=request.run,
                kind=request.kind,
                exit_code=code,
                executed=True,
                artifact_sha256=tc_digest,
                toolchain_digest=request.toolchain_digest,
                stdout="",
                stderr="",
                source_commit_sha=request.target_commit_sha,
                test_id=request.test_id,
                receipt_ref=f"flaky:{request.intent_ref}",
                timed_out=False,
                truncated=False,
            )

    flaky_sb = _FlakySandbox()
    p_flaky = ReproductionLab(flaky_sb, receipt_verifier=_ok_verifier).verify_pair(
        test_id="test_bug", pre_request=pre_req, post_request=post_req, repeat_count=2
    )
    assert len(set(flaky_sb.seen_intents)) == 4
    assert p_flaky.flaky is True and p_flaky.verified is False and p_flaky.reason == "FLAKY_REPRODUCTION"


def test_dev03_design_preferences_reject_legacy_demo_choice_and_sanitize() -> None:
    with pytest.raises(InvalidEvidence, match="DEPRECATED_DEMO_CHOICE_AUTO"):
        DevelopmentPreferences.from_raw({"demoChoice": "AUTO"})

    prefs = DevelopmentPreferences.from_raw(
        {"designPolicy": "AUTO_RECOMMEND", "demoAcceptancePolicy": "REQUIRE_USER_APPROVAL", "styleCandidates": 3}
    )
    assert prefs.design_policy == "AUTO_RECOMMEND"
    assert prefs.demo_acceptance_policy == "REQUIRE_USER_APPROVAL"

    cands, digest = normalize_design_candidates(
        [
            {
                "candidateId": "c1",
                "label": "Minimal",
                "fixtureRef": "fixture://dashboard-v1",
                "summary": "Uses Bearer tok_secret123 and ws://127.0.0.1:9222/devtools/page/1",
            },
            {
                "candidateId": "c2",
                "label": "Dense",
                "fixtureRef": "fixture://dashboard-v1",
                "summary": "Compact table layout",
            },
        ]
    )
    assert len(digest) == 64
    assert "tok_secret123" not in cands[0]["summary"]
    assert "devtools" not in cands[0]["summary"]
    assert "[REDACTED]" in redact_dev_text("ghp_1234567890abcdef")

    with pytest.raises(ScopeDenied, match="identical fixtureRef"):
        normalize_design_candidates(
            [
                {"candidateId": "c1", "label": "A", "fixtureRef": "fixture://a"},
                {"candidateId": "c2", "label": "B", "fixtureRef": "fixture://b"},
            ]
        )

    with pytest.raises(ScopeDenied, match="raw HTML"):
        normalize_design_candidates(
            [{"candidateId": "c1", "label": "A", "fixtureRef": "fixture://a", "rawHtml": "<script>x</script>"}]
        )


def test_dev11_pr_guardian_evaluation_and_repair_ceiling() -> None:
    """DEV-11 / P1-09: stale HEAD, required checks, neutral/unknown conclusions, pagination, mergeable=None, commit status."""
    sha = "a" * 40
    other_sha = "b" * 40

    ev_pass = evaluate_pr_checks(
        pr_payload={"mergeable": True, "mergeable_state": "clean", "head": {"sha": sha}},
        check_runs_payload={
            "total_count": 1,
            "check_runs": [{"name": "static", "status": "completed", "conclusion": "success"}],
        },
        expected_head_sha=sha,
        required_checks=("static",),
        repair_attempts=0,
        max_repair_attempts=3,
    )
    assert ev_pass.ci_state == "PASS" and ev_pass.should_repair is False and ev_pass.escalated is False

    # Stale HEAD refuses PASS even when checks report success
    ev_stale = evaluate_pr_checks(
        pr_payload={"mergeable": True, "mergeable_state": "clean", "head": {"sha": other_sha}},
        check_runs_payload={
            "total_count": 1,
            "check_runs": [{"name": "static", "status": "completed", "conclusion": "success"}],
        },
        expected_head_sha=sha,
        required_checks=("static",),
        repair_attempts=0,
        max_repair_attempts=3,
    )
    assert ev_stale.ci_state == "STALE_HEAD" and ev_stale.observed_head_sha == other_sha

    # Missing required check -> UNKNOWN (never PASS)
    ev_missing_req = evaluate_pr_checks(
        pr_payload={"mergeable": True, "mergeable_state": "clean", "head": {"sha": sha}},
        check_runs_payload={
            "total_count": 1,
            "check_runs": [{"name": "static", "status": "completed", "conclusion": "success"}],
        },
        expected_head_sha=sha,
        required_checks=("static", "security-gate"),
        repair_attempts=0,
        max_repair_attempts=3,
    )
    assert ev_missing_req.ci_state == "UNKNOWN"
    assert ev_missing_req.missing_required_checks == ("security-gate",)

    # Neutral/skipped-only checks -> UNKNOWN (never PASS)
    ev_neutral = evaluate_pr_checks(
        pr_payload={"mergeable": True, "mergeable_state": "clean", "head": {"sha": sha}},
        check_runs_payload={
            "total_count": 1,
            "check_runs": [{"name": "lint", "status": "completed", "conclusion": "skipped"}],
        },
        expected_head_sha=sha,
        repair_attempts=0,
        max_repair_attempts=3,
    )
    assert ev_neutral.ci_state == "UNKNOWN"

    # Unknown conclusion -> UNKNOWN
    ev_unk_conc = evaluate_pr_checks(
        pr_payload={"mergeable": True, "mergeable_state": "clean", "head": {"sha": sha}},
        check_runs_payload={
            "total_count": 2,
            "check_runs": [
                {"name": "static", "status": "completed", "conclusion": "success"},
                {"name": "custom", "status": "completed", "conclusion": "weird_future_state"},
            ],
        },
        expected_head_sha=sha,
        repair_attempts=0,
        max_repair_attempts=3,
    )
    assert ev_unk_conc.ci_state == "UNKNOWN"

    # Incomplete pagination -> UNKNOWN
    ev_incomplete = evaluate_pr_checks(
        pr_payload={"mergeable": True, "mergeable_state": "clean", "head": {"sha": sha}},
        check_runs_payload={
            "total_count": 150,
            "check_runs": [{"name": "static", "status": "completed", "conclusion": "success"}],
        },
        expected_head_sha=sha,
        repair_attempts=0,
        max_repair_attempts=3,
    )
    assert ev_incomplete.ci_state == "UNKNOWN"

    # mergeable=None -> PENDING (never PASS)
    ev_merge_none = evaluate_pr_checks(
        pr_payload={"mergeable": None, "mergeable_state": "unknown", "head": {"sha": sha}},
        check_runs_payload={
            "total_count": 1,
            "check_runs": [{"name": "static", "status": "completed", "conclusion": "success"}],
        },
        expected_head_sha=sha,
        repair_attempts=0,
        max_repair_attempts=3,
    )
    assert ev_merge_none.ci_state == "PENDING"

    # Commit status failure triggers FAIL even if check_runs succeeded
    ev_status_fail = evaluate_pr_checks(
        pr_payload={"mergeable": True, "mergeable_state": "clean", "head": {"sha": sha}},
        check_runs_payload={
            "total_count": 1,
            "check_runs": [{"name": "static", "status": "completed", "conclusion": "success"}],
        },
        commit_status_payload={"statuses": [{"context": "external/deploy-check", "state": "failure"}]},
        expected_head_sha=sha,
        repair_attempts=0,
        max_repair_attempts=3,
    )
    assert ev_status_fail.ci_state == "FAIL"
    assert ev_status_fail.failing_checks == ("external/deploy-check",)

    # Finite repair ceiling & conflict
    ev_cap = evaluate_pr_checks(
        pr_payload={"mergeable": True, "mergeable_state": "unstable", "head": {"sha": sha}},
        check_runs_payload={"check_runs": [{"name": "pytest", "status": "completed", "conclusion": "failure"}]},
        expected_head_sha=sha,
        repair_attempts=3,
        max_repair_attempts=3,
    )
    assert ev_cap.ci_state == "ESCALATED_LIMIT_REACHED"
    assert ev_cap.should_repair is False and ev_cap.escalated is True

    ev_conf = evaluate_pr_checks(
        pr_payload={"mergeable": False, "mergeable_state": "dirty", "head": {"sha": sha}},
        check_runs_payload={"check_runs": []},
        expected_head_sha=sha,
        repair_attempts=0,
        max_repair_attempts=2,
    )
    assert ev_conf.ci_state == "CONFLICT" and ev_conf.should_repair is True

    # R2-08: Empty required_checks OR commit_status_ok=False MUST fail closed as UNKNOWN (never PASS)
    ev_no_req = evaluate_pr_checks(
        pr_payload={"mergeable": True, "mergeable_state": "clean", "head": {"sha": sha}},
        check_runs_payload={
            "total_count": 1,
            "check_runs": [{"name": "static", "status": "completed", "conclusion": "success"}],
        },
        expected_head_sha=sha,
        required_checks=(),
        repair_attempts=0,
        max_repair_attempts=3,
    )
    assert ev_no_req.ci_state == "UNKNOWN"

    ev_status_err = evaluate_pr_checks(
        pr_payload={"mergeable": True, "mergeable_state": "clean", "head": {"sha": sha}},
        check_runs_payload={
            "total_count": 1,
            "check_runs": [{"name": "static", "status": "completed", "conclusion": "success"}],
        },
        commit_status_payload=None,
        commit_status_ok=False,
        expected_head_sha=sha,
        required_checks=("static",),
        repair_attempts=0,
        max_repair_attempts=3,
    )
    assert ev_status_err.ci_state == "UNKNOWN"

    # R3-10: App ID mismatch MUST NOT satisfy an app-pinned required check rule!
    ev_wrong_app = evaluate_pr_checks(
        pr_payload={"mergeable": True, "mergeable_state": "clean", "head": {"sha": sha}},
        check_runs_payload={
            "total_count": 1,
            "check_runs": [{"name": "ci/build", "status": "completed", "conclusion": "success", "app": {"id": 99999}}],
        },
        expected_head_sha=sha,
        required_check_rules=(RequiredCheckRule("ci/build", app_id=15368),),
        repair_attempts=0,
        max_repair_attempts=3,
    )
    assert ev_wrong_app.ci_state == "UNKNOWN"
    assert ev_wrong_app.missing_required_checks == ("ci/build(app:15368)",)

    # PrGuardian paginates check-runs, reads branch metadata + branch protection + rulesets (with App IDs) + commit statuses
    seen_paths: list[str] = []

    def _fake_read(scope: ProjectScope, intent_ref: str, path: str) -> tuple[int, object]:
        del scope, intent_ref
        seen_paths.append(path)
        if path.endswith("/pulls/9"):
            return 200, {"mergeable": True, "mergeable_state": "clean", "head": {"sha": sha}, "base": {"ref": "main"}}
        if path.endswith("/rules/branches/main"):
            return 200, [
                {
                    "type": "required_status_checks",
                    "parameters": {"required_status_checks": [{"context": "ruleset-gate", "integration_id": 15368}]},
                }
            ]
        if path.endswith("/branches/main"):
            return 200, {"name": "main", "protected": True}
        if path.endswith("/branches/main/protection/required_status_checks"):
            return 200, {"contexts": ["legacy/ci"], "checks": [{"context": "required-last", "app_id": 15368}]}
        if "check-runs?per_page=100&page=1" in path:
            return 200, {
                "total_count": 102,
                "check_runs": [
                    {"name": f"c-{i}", "status": "completed", "conclusion": "success", "app": {"id": 15368}}
                    for i in range(100)
                ],
            }
        if "check-runs?per_page=100&page=2" in path:
            return 200, {
                "total_count": 102,
                "check_runs": [
                    {"name": "required-last", "status": "completed", "conclusion": "success", "app": {"id": 15368}},
                    {"name": "ruleset-gate", "status": "completed", "conclusion": "success", "app": {"id": 15368}},
                ],
            }
        if path.endswith("/status"):
            return 200, {"statuses": [{"context": "legacy/ci", "state": "success"}]}
        return 404, {}

    scope = ProjectScope("t1", "u1", "p1", "w1", "org/demo", "feat/x", sha, "1")
    ev_paged = PrGuardian(_fake_read).inspect_pr(
        scope=scope,
        intent_ref="int-1",
        pr_number=9,
        head_sha=sha,
        repair_attempts=0,
        max_repair_attempts=2,
        required_checks=(),
    )
    assert ev_paged.ci_state == "PASS"
    assert any("page=2" in p for p in seen_paths)
    assert any("protection/required_status_checks" in p for p in seen_paths)
    assert any("/rules/branches/main" in p for p in seen_paths)
    assert any(p.endswith("/status") for p in seen_paths)

    # R3-10: Protected branch where protection returns 404 and rulesets return [] (permission gap) MUST fail closed as UNKNOWN!
    def _fake_protected_404(scope: ProjectScope, intent_ref: str, path: str) -> tuple[int, object]:
        del scope, intent_ref
        if path.endswith("/pulls/9"):
            return 200, {"mergeable": True, "mergeable_state": "clean", "head": {"sha": sha}, "base": {"ref": "main"}}
        if path.endswith("/rules/branches/main"):
            return 200, []
        if path.endswith("/branches/main"):
            return 200, {"name": "main", "protected": True}
        if path.endswith("/branches/main/protection/required_status_checks"):
            return 404, {}
        if "check-runs" in path:
            return 200, {
                "total_count": 1,
                "check_runs": [{"name": "static", "status": "completed", "conclusion": "success"}],
            }
        if path.endswith("/status"):
            return 200, {"statuses": []}
        return 404, {}

    ev_prot_404 = PrGuardian(_fake_protected_404).inspect_pr(
        scope=scope,
        intent_ref="int-1",
        pr_number=9,
        head_sha=sha,
        repair_attempts=0,
        max_repair_attempts=2,
        required_checks=("static",),
    )
    assert ev_prot_404.ci_state == "UNKNOWN"

    # R4-07: Strict Commit Status completeness + explicit PASS mergeable_state allowlist + separate merge_readiness!
    # 1. Incomplete commit statuses (total_count > len(statuses)) -> UNKNOWN (never PASS)
    ev_st_incomplete = evaluate_pr_checks(
        pr_payload={"mergeable": True, "mergeable_state": "clean", "head": {"sha": sha}},
        check_runs_payload={
            "total_count": 1,
            "check_runs": [{"name": "static", "status": "completed", "conclusion": "success"}],
        },
        commit_status_payload={
            "state": "success",
            "total_count": 5,
            "statuses": [{"context": "legacy/ci", "state": "success"}],
        },
        expected_head_sha=sha,
        required_checks=("static",),
        repair_attempts=0,
        max_repair_attempts=3,
    )
    assert ev_st_incomplete.ci_state == "UNKNOWN" and ev_st_incomplete.merge_readiness == "MERGEABLE"

    # 2. Aggregate commit status state="failure" triggers FAIL even if statuses array is empty
    ev_st_agg_fail = evaluate_pr_checks(
        pr_payload={"mergeable": True, "mergeable_state": "clean", "head": {"sha": sha}},
        check_runs_payload={
            "total_count": 1,
            "check_runs": [{"name": "static", "status": "completed", "conclusion": "success"}],
        },
        commit_status_payload={"state": "failure", "total_count": 0, "statuses": []},
        expected_head_sha=sha,
        required_checks=("static",),
        repair_attempts=0,
        max_repair_attempts=3,
    )
    assert ev_st_agg_fail.ci_state == "FAIL" and "commit_status:aggregate" in ev_st_agg_fail.failing_checks

    # 3. GitHub empty commit status default (state="pending", total_count=0, statuses=[]) still allows PASS
    ev_st_empty_ok = evaluate_pr_checks(
        pr_payload={"mergeable": True, "mergeable_state": "clean", "head": {"sha": sha}},
        check_runs_payload={
            "total_count": 1,
            "check_runs": [{"name": "static", "status": "completed", "conclusion": "success"}],
        },
        commit_status_payload={"state": "pending", "total_count": 0, "statuses": []},
        expected_head_sha=sha,
        required_checks=("static",),
        repair_attempts=0,
        max_repair_attempts=3,
    )
    assert ev_st_empty_ok.ci_state == "PASS" and ev_st_empty_ok.merge_readiness == "MERGEABLE"

    # 4. Non-clean mergeable_state ("behind", "draft", "unstable", "future_state") MUST NOT PASS
    for bad_state, expected_mr in (("behind", "BLOCKED"), ("draft", "BLOCKED"), ("future_state", "UNKNOWN")):
        ev_bad_ms = evaluate_pr_checks(
            pr_payload={"mergeable": True, "mergeable_state": bad_state, "head": {"sha": sha}},
            check_runs_payload={
                "total_count": 1,
                "check_runs": [{"name": "static", "status": "completed", "conclusion": "success"}],
            },
            expected_head_sha=sha,
            required_checks=("static",),
            repair_attempts=0,
            max_repair_attempts=3,
        )
        assert ev_bad_ms.ci_state == "UNKNOWN" and ev_bad_ms.merge_readiness == expected_mr
