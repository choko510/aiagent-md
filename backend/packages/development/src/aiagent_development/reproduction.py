"""DEV-12 Reproduction Lab: enforces paired pre-fix FAIL / post-fix PASS evidence and flake isolation."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import ExecutionEvidence, ExecutionRequest, InvalidEvidence
from .ports import SandboxExecutionPort


@dataclass(frozen=True)
class ReproductionProof:
    test_id: str
    pre_fix_commit: str
    post_fix_commit: str
    before_fail_digest: str | None
    after_pass_digest: str | None
    flaky: bool
    verified: bool
    reason: str
    pre_evidences: tuple[ExecutionEvidence, ...]
    post_evidences: tuple[ExecutionEvidence, ...]


def _is_clean_complete(ev: ExecutionEvidence) -> bool:
    return ev.executed and ev.timed_out is False and ev.truncated is False


class ReproductionLab:
    """Runs failure-then-fix verification pairs inside an isolated Execution Cell."""

    def __init__(self, sandbox: SandboxExecutionPort) -> None:
        self._sandbox = sandbox

    def verify_pair(
        self,
        *,
        test_id: str,
        pre_request: ExecutionRequest,
        post_request: ExecutionRequest,
        repeat_count: int = 1,
    ) -> ReproductionProof:
        if not test_id:
            raise InvalidEvidence("reproduction test_id is required")
        if repeat_count < 1 or repeat_count > 5:
            raise InvalidEvidence("repeat_count must be between 1 and 5")

        pre_results: list[ExecutionEvidence] = []
        post_results: list[ExecutionEvidence] = []

        for _ in range(repeat_count):
            pre_results.append(self._sandbox.execute(pre_request))
        for _ in range(repeat_count):
            post_results.append(self._sandbox.execute(post_request))

        pre_outcomes = {(_is_clean_complete(ev), ev.exit_code == 0) for ev in pre_results}
        post_outcomes = {(_is_clean_complete(ev), ev.exit_code == 0) for ev in post_results}
        flaky = len(pre_outcomes) > 1 or len(post_outcomes) > 1

        pre_all_failed = all(_is_clean_complete(ev) and ev.exit_code not in (None, 0) for ev in pre_results)
        post_all_passed = all(_is_clean_complete(ev) and ev.exit_code == 0 for ev in post_results)

        pre_commit = pre_results[0].source_commit_sha or pre_request.target_commit_sha or pre_request.project.base_sha
        post_commit = (
            post_results[0].source_commit_sha or post_request.target_commit_sha or post_request.project.base_sha
        )

        before_digest = pre_results[0].artifact_sha256 if pre_all_failed else None
        after_digest = post_results[0].artifact_sha256 if post_all_passed else None

        if flaky:
            return ReproductionProof(
                test_id=test_id,
                pre_fix_commit=pre_commit,
                post_fix_commit=post_commit,
                before_fail_digest=before_digest,
                after_pass_digest=after_digest,
                flaky=True,
                verified=False,
                reason="FLAKY_REPRODUCTION",
                pre_evidences=tuple(pre_results),
                post_evidences=tuple(post_results),
            )
        if not pre_all_failed:
            return ReproductionProof(
                test_id=test_id,
                pre_fix_commit=pre_commit,
                post_fix_commit=post_commit,
                before_fail_digest=None,
                after_pass_digest=after_digest,
                flaky=False,
                verified=False,
                reason="PRE_FIX_DID_NOT_FAIL",
                pre_evidences=tuple(pre_results),
                post_evidences=tuple(post_results),
            )
        if not post_all_passed:
            return ReproductionProof(
                test_id=test_id,
                pre_fix_commit=pre_commit,
                post_fix_commit=post_commit,
                before_fail_digest=before_digest,
                after_pass_digest=None,
                flaky=False,
                verified=False,
                reason="POST_FIX_DID_NOT_PASS",
                pre_evidences=tuple(pre_results),
                post_evidences=tuple(post_results),
            )
        return ReproductionProof(
            test_id=test_id,
            pre_fix_commit=pre_commit,
            post_fix_commit=post_commit,
            before_fail_digest=before_digest,
            after_pass_digest=after_digest,
            flaky=False,
            verified=True,
            reason="VERIFIED",
            pre_evidences=tuple(pre_results),
            post_evidences=tuple(post_results),
        )
