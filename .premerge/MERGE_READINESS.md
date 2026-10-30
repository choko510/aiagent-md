# Round 7 — Merge readiness and maintainer handoff

> **Status: TECHNICAL DOCUMENT INTEGRATION PASS; FINAL MAINTAINER ACCEPTANCE PENDING.**
> Branch: `integration/round7-all-specs-premerge`. PR [#64](https://github.com/choko510/aiagent-md/pull/64). Source PRs #1–#61 remain Draft; no changes were made to `main`.

## What was concretely verified
- All **85** indexed module specs are physically present, under the intended owner directories. Ten preexisting Foundation bootstrap documents remain.
- All 85 specification Git blobs match the exact current content of source PRs #1–#61, pinned by SHA in [pinned-prs.json](pinned-prs.json). No one silently re-authored a source module in the integration branch.
- Round 7 CI on the **assembled** tree checks expected file counts, owner mapping, local relative Markdown links, canonical int64 JSON encodings and 11 key cross-module contract-reference fingerprints. Every Green check is scoped to the version of the branch it ran on.
- PostgreSQL 16 runs 0001–0003 proposed DDL and synthetic concurrency/FK/migration fixtures, while real Chromium reproduces hidden POST side effects and a restricted Docker ZIP metadata parser rejects test threats. **Those are fixture tests, not product runtime E2E**.

## Pre-merge issues and their precise disposition
| Prior blocker | Current disposition | Remaining human gate |
|---|---|---|
| 85 specs scattered across 61 branches | **Resolved technically:** integrated tree assembled without duplicate/lost source blobs | Reviewer chooses integration strategy |
| Different source PRs individually clean but untested together | **Resolved for tested properties:** full merged-tree CI, exact SHA, source link/type guard | Semantic review of actual contract decisions |
| Legacy UUIDv4/revision0/nullable Conversation text | Current PR #10 DDL updated; Round6 JSON wire tests and Owner docs aligned | Owner accepts canonical field semantics; not Runtime-verified |
| X receipt generation / Task dedup, digest ownership, feedback and parser lineage | Proposed physical v2/v3 migrations and synthetic PostgreSQL tests succeeded | App code contracts and final boundary tests remain separate implementation milestone |
| Hidden click/fill POST and untrusted parser exploit avenues | Documented safe default (no auto-replay interactive actions); threat-reproduction and sample sandbox fixtures green | Real Gateway/EgressGuard/Parser still require runtime security test before production |
| Contract signoff requirement | **NOT RESOLVED BY CI:** PR #62/Issue #63 explicitly request CL-01..12 Owner/maintainer ratification | Human maintainer approval of documents as Draft or formal Locked decisions |
| Deployment readiness | Not a blocker to **merging clearly labelled Draft specs**, but a hard blocker to claiming product implementation/production readiness | Separate code, test and security review |

## Two exclusive merge methods
**Option A — one consolidated specification PR (recommended for simple integration):** Review [PR #64](https://github.com/choko510/aiagent-md/pull/64), confirm CI on exact HEAD, require responsible human maintainer approval, then merge #64. After merged, **close #1–#61 as superseded by #64 (not as individually merged)**. Retain their HEADs and provenance manifest; do not merge both paths.

**Option B — 61 individual PR merges (preserves each PR's original merge history):** Require the same maintainer decision, then merge in dependency order: Foundation 59–61; core 2/23/33/43 and security 9/18/41/47; DB 10/22/26/36/45; runtime/tool/browser/providers/memory/watch/interfaces; intelligence/event/learning/multimodal; QA. After *each* PR, confirm GitHub mergeability and re-run 85-MD integrated-tree diff/CI or create an integration staging branch from updated main. Do **not** also merge #64. Original PR #62 is a **separate unapproved governance proposal** and should not be represented as ratified.

## Final click gate
1. Authorized maintainer explicitly accepts the documentation-only Draft merge, with provisional/unapproved CL statuses preserved, **or** completes CL-01..12 with review evidence and locks the specs.
2. CI green on **latest** PR #64 HEAD; GitHub mergeable clean; main has not drifted.
3. Review the PR diff for accidental secrets/private files; verify the `.premerge` manifest is a dated snapshot and future `main` CI does not depend on the source Drafts still being open.
4. Do not claim Agent Harness runtime is Implemented or Verified; follow-up implementation begins only with approved contracts.

## Important
The main software does not exist in this repo. A spec Draft may be stored on main without claiming it is released, but the **project's existing review-gates.md requires explicit maintainer acceptance** for that change. No AI tool can fabricate a human GitHub review, and no agreement has been fabricated here.
