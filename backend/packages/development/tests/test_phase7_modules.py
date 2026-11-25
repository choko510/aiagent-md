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

from aiagent_development.adaptive_testing import AdaptiveTestingGate, plan_adaptive_tests
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
from aiagent_development.mock import MockSandbox
from aiagent_development.pr_guardian import evaluate_pr_checks
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
    repo, sha = _make_git_repo(tmp_path)
    scope = ProjectScope("t1", "u1", "p1", "w1", "org/demo", "main", sha, "1")
    run = RunFence("task-1", "run-1", 1, 1, 0)
    plan = TaskPlan("plan-1", "cr-1", ("test_security_baseline.py",), 1)
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
    # 1. Pure Python source change -> selects impacted tests/test_app.py only (no full fallback)
    ad_plan = plan_adaptive_tests(reader, inv, ("pkg_a/app.py",), plan)
    assert ad_plan.fallback_to_full is False
    assert ad_plan.impacted_tests == ("tests/test_app.py",)
    assert ad_plan.mandatory_tests == ("test_security_baseline.py",)

    # 2. Manifest / config change triggers conservative fallback to full discovered test suite
    fb_plan = plan_adaptive_tests(reader, inv, ("pyproject.toml",), plan)
    assert fb_plan.fallback_to_full is True
    assert set(fb_plan.impacted_tests) == {"tests/test_app.py", "tests/test_other.py"}

    # 3. Deleted/missing file also triggers conservative fallback
    del_plan = plan_adaptive_tests(reader, inv, ("deleted_file.py",), plan)
    assert del_plan.fallback_to_full is True
    assert set(del_plan.impacted_tests) == {"tests/test_app.py", "tests/test_other.py"}

    # 4. Impacted PASS with mandatory SKIPPED MUST NOT report overall_passed=True
    sb = MockSandbox()
    gate = AdaptiveTestingGate(sb)
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

    # 5. Both impacted and mandatory PASS -> overall_passed=True
    res_ok = gate.run_gate(
        scope=scope,
        run=run,
        plan=plan,
        adaptive_plan=ad_plan,
        toolchain_digest=tc_digest,
        intent_prefix="g2",
        target_commit_sha=sha,
        run_mandatory=True,
    )
    assert res_ok.impacted_status == "PASSED"
    assert res_ok.mandatory_status == "PASSED"
    assert res_ok.overall_passed is True

    # 6. Truncated/timed-out sandbox evidence fails closed even if exit_code==0
    class _TruncatedSandbox:
        def execute(self, request: ExecutionRequest) -> ExecutionEvidence:
            return ExecutionEvidence(
                intent_ref=request.intent_ref,
                run=request.run,
                kind=request.kind,
                exit_code=0,
                executed=True,
                artifact_sha256=tc_digest,
                toolchain_digest=request.toolchain_digest,
                stdout="clipped",
                stderr="",
                source_commit_sha=sha,
                test_id=request.test_id,
                receipt_ref="trunc:1",
                timed_out=False,
                truncated=True,
            )

    res_trunc = AdaptiveTestingGate(_TruncatedSandbox()).run_gate(
        scope=scope,
        run=run,
        plan=plan,
        adaptive_plan=ad_plan,
        toolchain_digest=tc_digest,
        intent_prefix="g3",
        target_commit_sha=sha,
    )
    assert res_trunc.impacted_status == "FAILED"
    assert res_trunc.overall_passed is False


def test_dev12_reproduction_lab_enforces_fail_pass_pair_and_detects_flaky() -> None:
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

    # 1. Valid pre-FAIL + post-PASS pair
    sb = MockSandbox()
    sb.results["repro-pre"] = (True, 1, "test_bug")
    sb.results["repro-post"] = (True, 0, "test_bug")
    proof = ReproductionLab(sb).verify_pair(test_id="test_bug", pre_request=pre_req, post_request=post_req)
    assert proof.verified is True
    assert proof.flaky is False
    assert proof.reason == "VERIFIED"
    assert proof.before_fail_digest is not None and proof.after_pass_digest is not None

    # 2. Pre-fix already passing -> rejected
    sb2 = MockSandbox()
    sb2.results["repro-pre"] = (True, 0, "test_bug")
    sb2.results["repro-post"] = (True, 0, "test_bug")
    p_nofail = ReproductionLab(sb2).verify_pair(test_id="test_bug", pre_request=pre_req, post_request=post_req)
    assert p_nofail.verified is False and p_nofail.reason == "PRE_FIX_DID_NOT_FAIL"

    # 3. Flaky post-fix test (alternates 0 and 1 across repeat_count=2)
    class _FlakySandbox:
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}

        def execute(self, request: ExecutionRequest) -> ExecutionEvidence:
            n = self.calls.get(request.intent_ref, 0)
            self.calls[request.intent_ref] = n + 1
            code = 1 if request.intent_ref == "repro-pre" else (0 if n == 0 else 1)
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
                receipt_ref=f"flaky:{n}",
                timed_out=False,
                truncated=False,
            )

    p_flaky = ReproductionLab(_FlakySandbox()).verify_pair(
        test_id="test_bug", pre_request=pre_req, post_request=post_req, repeat_count=2
    )
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
    ev_pass = evaluate_pr_checks(
        pr_payload={"mergeable": True, "mergeable_state": "clean"},
        check_runs_payload={"check_runs": [{"name": "static", "status": "completed", "conclusion": "success"}]},
        repair_attempts=0,
        max_repair_attempts=3,
    )
    assert ev_pass.ci_state == "PASS" and ev_pass.should_repair is False and ev_pass.escalated is False

    ev_fail = evaluate_pr_checks(
        pr_payload={"mergeable": True, "mergeable_state": "unstable"},
        check_runs_payload={"check_runs": [{"name": "pytest", "status": "completed", "conclusion": "failure"}]},
        repair_attempts=1,
        max_repair_attempts=3,
    )
    assert ev_fail.ci_state == "FAIL" and ev_fail.failing_checks == ("pytest",)
    assert ev_fail.should_repair is True and ev_fail.escalated is False

    ev_cap = evaluate_pr_checks(
        pr_payload={"mergeable": True, "mergeable_state": "unstable"},
        check_runs_payload={"check_runs": [{"name": "pytest", "status": "completed", "conclusion": "failure"}]},
        repair_attempts=3,
        max_repair_attempts=3,
    )
    assert ev_cap.ci_state == "ESCALATED_LIMIT_REACHED"
    assert ev_cap.should_repair is False and ev_cap.escalated is True

    ev_conf = evaluate_pr_checks(
        pr_payload={"mergeable": False, "mergeable_state": "dirty"},
        check_runs_payload={"check_runs": []},
        repair_attempts=0,
        max_repair_attempts=2,
    )
    assert ev_conf.ci_state == "CONFLICT" and ev_conf.should_repair is True
