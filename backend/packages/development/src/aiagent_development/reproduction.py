"""DEV-12 Reproduction Lab: enforces paired pre-fix FAIL / post-fix PASS evidence and flake isolation.

Hardens P1-08:
- Requires pre_request and post_request to bind the exact same test_id, kind='test', argv,
  toolchain_digest, plan_ref, and project identity, across two distinct target commits.
- Uses distinct intent_ref per repetition and refuses duplicate receipt_ref reuse across runs.
- Validates every returned ExecutionEvidence matches its request (intent_ref, test_id, toolchain_digest,
  source_commit_sha, receipt_ref) and rejects pytest collection/env errors (exit codes 2/3/4/5).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .contracts import ExecutionEvidence, ExecutionRequest, InvalidEvidence
from .ports import SandboxExecutionPort

_NON_FAILURE_ENV_EXIT_CODES = frozenset({2, 3, 4, 5})


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


def _matches_request(req: ExecutionRequest, ev: ExecutionEvidence, expected_test_id: str) -> bool:
    return (
        ev.intent_ref == req.intent_ref
        and ev.kind == "test"
        and ev.test_id == expected_test_id
        and ev.toolchain_digest == req.toolchain_digest
        and ev.source_commit_sha == req.target_commit_sha
        and bool(ev.receipt_ref and ev.receipt_ref.strip())
    )


def _validate_pair_requests(
    test_id: str, pre_request: ExecutionRequest, post_request: ExecutionRequest
) -> tuple[str, str]:
    if not test_id:
        raise InvalidEvidence("reproduction test_id is required")
    if pre_request.kind != "test" or post_request.kind != "test":
        raise InvalidEvidence("reproduction requires kind='test' on both requests")
    if pre_request.test_id != test_id or post_request.test_id != test_id:
        raise InvalidEvidence("pre and post requests must match reproduction test_id")
    if pre_request.argv != post_request.argv:
        raise InvalidEvidence("pre and post requests must execute identical test command")
    if pre_request.toolchain_digest != post_request.toolchain_digest:
        raise InvalidEvidence("pre and post requests must use identical toolchain_digest")
    if pre_request.plan_ref != post_request.plan_ref:
        raise InvalidEvidence("pre and post requests must share identical plan_ref")
    p1, p2 = pre_request.project, post_request.project
    if (p1.tenant_id, p1.owner_user_id, p1.project_id, p1.workspace_id, p1.repository, p1.branch) != (
        p2.tenant_id,
        p2.owner_user_id,
        p2.project_id,
        p2.workspace_id,
        p2.repository,
        p2.branch,
    ):
        raise InvalidEvidence("pre and post requests must belong to the same project scope")
    if pre_request.target_commit_sha is None or post_request.target_commit_sha is None:
        raise InvalidEvidence("pre and post requests must declare explicit target_commit_sha")
    if pre_request.target_commit_sha == post_request.target_commit_sha:
        raise InvalidEvidence("pre and post target_commit_sha must differ")
    return pre_request.target_commit_sha, post_request.target_commit_sha


class ReproductionLab:
    """Runs failure-then-fix verification pairs inside an isolated Execution Cell."""

    def __init__(self, sandbox: SandboxExecutionPort) -> None:
        self._sandbox = sandbox

    def verify_pair(  # noqa: PLR0911
        self,
        *,
        test_id: str,
        pre_request: ExecutionRequest,
        post_request: ExecutionRequest,
        repeat_count: int = 1,
    ) -> ReproductionProof:
        pre_commit, post_commit = _validate_pair_requests(test_id, pre_request, post_request)
        if isinstance(repeat_count, bool) or not 1 <= repeat_count <= 5:
            raise InvalidEvidence("repeat_count must be between 1 and 5")

        pre_results: list[ExecutionEvidence] = []
        post_results: list[ExecutionEvidence] = []
        seen_receipts: set[str] = set()

        for i in range(repeat_count):
            req_i = (
                pre_request
                if repeat_count == 1
                else replace(pre_request, intent_ref=f"{pre_request.intent_ref}:iter:{i}")
            )
            ev = self._sandbox.execute(req_i)
            if not _matches_request(req_i, ev, test_id):
                raise InvalidEvidence("pre-fix execution evidence mismatched request bindings")
            if ev.receipt_ref in seen_receipts:
                return ReproductionProof(
                    test_id,
                    pre_commit,
                    post_commit,
                    None,
                    None,
                    False,
                    False,
                    "DUPLICATE_EXECUTION_RECEIPT",
                    tuple(pre_results),
                    tuple(post_results),
                )
            seen_receipts.add(ev.receipt_ref)
            pre_results.append(ev)

        for i in range(repeat_count):
            req_i = (
                post_request
                if repeat_count == 1
                else replace(post_request, intent_ref=f"{post_request.intent_ref}:iter:{i}")
            )
            ev = self._sandbox.execute(req_i)
            if not _matches_request(req_i, ev, test_id):
                raise InvalidEvidence("post-fix execution evidence mismatched request bindings")
            if ev.receipt_ref in seen_receipts:
                return ReproductionProof(
                    test_id,
                    pre_commit,
                    post_commit,
                    None,
                    None,
                    False,
                    False,
                    "DUPLICATE_EXECUTION_RECEIPT",
                    tuple(pre_results),
                    tuple(post_results),
                )
            seen_receipts.add(ev.receipt_ref)
            post_results.append(ev)

        pre_outcomes = {(_is_clean_complete(ev), ev.exit_code == 0) for ev in pre_results}
        post_outcomes = {(_is_clean_complete(ev), ev.exit_code == 0) for ev in post_results}
        flaky = len(pre_outcomes) > 1 or len(post_outcomes) > 1

        if any(ev.exit_code in _NON_FAILURE_ENV_EXIT_CODES for ev in pre_results):
            return ReproductionProof(
                test_id,
                pre_commit,
                post_commit,
                None,
                None,
                flaky,
                False,
                "PRE_FIX_ENV_OR_COLLECTION_ERROR",
                tuple(pre_results),
                tuple(post_results),
            )

        pre_all_failed = all(_is_clean_complete(ev) and ev.exit_code not in (None, 0) for ev in pre_results)
        post_all_passed = all(_is_clean_complete(ev) and ev.exit_code == 0 for ev in post_results)

        before_digest = pre_results[0].artifact_sha256 if pre_all_failed else None
        after_digest = post_results[0].artifact_sha256 if post_all_passed else None

        if flaky:
            return ReproductionProof(
                test_id,
                pre_commit,
                post_commit,
                before_digest,
                after_digest,
                True,
                False,
                "FLAKY_REPRODUCTION",
                tuple(pre_results),
                tuple(post_results),
            )
        if not pre_all_failed:
            return ReproductionProof(
                test_id,
                pre_commit,
                post_commit,
                None,
                after_digest,
                False,
                False,
                "PRE_FIX_DID_NOT_FAIL",
                tuple(pre_results),
                tuple(post_results),
            )
        if not post_all_passed:
            return ReproductionProof(
                test_id,
                pre_commit,
                post_commit,
                before_digest,
                None,
                False,
                False,
                "POST_FIX_DID_NOT_PASS",
                tuple(pre_results),
                tuple(post_results),
            )
        return ReproductionProof(
            test_id,
            pre_commit,
            post_commit,
            before_digest,
            after_digest,
            False,
            True,
            "VERIFIED",
            tuple(pre_results),
            tuple(post_results),
        )
