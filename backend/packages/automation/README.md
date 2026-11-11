# STEP 4 AI-04 — bounded Campaign / Automation (isolated slice)

**Status: IMPLEMENTED (isolated logic) / TESTED (local mock). NOT VERIFIED in production.**

This package intentionally contains **no provider clients, Task/Run tables, PostgreSQL migrations, or actual recurring remote access**. It preserves the STEP 3 `QueuePort` contract (the only real queue is `run`, with canonical `RunJobPointerV1`) and delegates mapping of campaign dispatch intents into authorized Task/Run creations to AI-07. SQLite here is a **test harness only**, not a competing production datastore. This package is a member of the locked backend workspace. Registration does not connect it to PostgreSQL or the production scheduler.

## Implemented

- CampaignPlan/StopPolicy with strictly positive finite cycles, deadline, total cost/token/tool ceilings and per-cycle reservations; no unbounded flag.
- `PLAN → EXECUTE → VERIFY → DONE` with immutable logical work ID, stable cycle ID, trusted VerificationPort (default deny) and audit DTO. No model-only success counted.
- SQLite test aggregate with single-transaction mutation and durable dispatch outbox; simulated restart, repeated queue relay, stale control version, stop/approval/revoke, and pessimistic UNKNOWN effect/usage hold.
- Stable effect intent identity is persisted as `MAY_HAVE_SENT` **before** the caller can send; a repeated intent is never re-admitted. External send is never performed by this module. Already-sent effects may remain UNKNOWN after STOP; the UI must not claim a physical exactly-once effect.
- Test-only bounded daily Watch slots with IANA timezone, DST gap skip / first fold, stable revision-aware occurrence key, missed-day skip and deterministic FakeClock.

## Test in Chat / WSL2 / GitHub Actions

```bash
cd backend/packages/automation
python3 -m pip install pytest  # only if pytest is not installed
bash scripts/ci.sh
```

The root `backend.yml` currently runs `backend/tests` and `uv sync --locked`, **not the tests in this isolated package**. AI-07 should add the following step to an active workflow **after** registering the package and updating `backend/uv.lock`:

```yaml
- name: AI-04 bounded Campaign/Watch tests
  working-directory: backend/packages/automation
  run: bash scripts/ci.sh
```

This proposed step is a **handoff**, not an active GitHub Actions workflow. An optional copy-ready workflow is provided in `ci-workflow.example.yml`; GitHub discovers workflows only in repository-root `.github/workflows/`. Existing backend Actions running on this PR check the overall repository but do not establish CAM test completion unless that step is activated. The production DB admission / KVM / real WSL / external service tests remain **NOT RUN**.

## AI-07 integration contract (blocking for deploy)

1. Create PostgreSQL campaign/cycle/control/audit/outbox schema with unique `(tenant,campaign,cycle)` and `(tenant,campaign,logicalWorkItemId)` count identity, revision, immutable effect-intent reference, held/uncertain reservations and monotonically increasing controlVersion. Use one row lock/CAS for STOP vs child admission; never store production state in SQLite.
2. Implement `QueueDispatch.request_run` returning `bool` and creating Task/Run (via `aiagent_core.commands.create_task_tx`) **in the same PostgreSQL transaction** as verifying owner authorization, policy epoch, campaign controlVersion, goal revision, budget and STOP fence. A preflight read before enqueue is insufficient. Existing `QueuePort` only supports `run` payload, so no campaign queue/broker is added.
3. Recheck authorization/controlVersion/Run lease/Tool Gateway ledger and limits before each model/tool/Browser operation, not just once when the cycle is claimed. A relay may race STOP: rejected Task creation must return `False` and create **zero** new Tasks.
4. Bridge VerificationPort to trusted independently obtained evidence and quality-gate receipts; model-supplied `claimed_success` and arbitrary strings alone are insufficient. For Feature Factory, require current integrated HEAD eligibility (CAM-T17/18/19) before counted completion. Store signed/validated usage receipts; unknown provider usage must retain reservations.
5. Bridge the existing APScheduler + schedule occurrences to Watch; DST logic here is for deterministic test of daily scheduling, not a replacement for production Scheduler or DB occurrence contract. An updated revision must not reuse the previous revision's slot key.
6. Route stop/pause/resume/approval through authenticated, idempotent command handlers; handle stale pending outbox intents, wake generation, quota evidence and verified reconciliation. Disallow unapproved remote writes and arbitrary worker egress.
7. Register module in root `uv` workspace/lock, mypy paths, and root CI. Keep concurrent AI-02/03/05 patches out of this PR and install integration changes only after conflict review.

## Codex CLI / Windows + WSL + real-host handoff (NOT RUN)

Run **after AI-07 DB integration**, from Windows Terminal / WSL2 (do not assume WSL2 runs while Windows sleeps):

```bash
wsl -d Ubuntu -- bash -lc 'cd ~/aiagent-md/backend && uv sync --locked && bash packages/automation/scripts/ci.sh'
wsl -d Ubuntu -- bash -lc 'cd ~/aiagent-md/backend && AIAGENT_TEST_PG_URL=postgresql://postgres:ci@127.0.0.1:5432/postgres uv run pytest -q tests'
# Then stop the worker in a test-only environment and restart it (never on production).
wsl --shutdown
wsl -d Ubuntu -- bash -lc 'cd ~/aiagent-md/backend && bash packages/automation/scripts/ci.sh'
```

**Expected:** no duplicate Task/Run for `(campaign,cycle)`, no external send after committed STOP, no second Effect attempt following kill/reclaim, no bypass of UNKNOWN barrier, no reset of budget/receipt after restart. Verify DST transitions in both Asia/Tokyo and America/New_York, and a migration rollback/recovery on a disposable test DB. **If failed:** inspect controlVersion/goalRevision, queue dedupe key, authoritative campaign transaction, Run leaseEpoch, Tool intent ledger `MAY_HAVE_SENT/UNKNOWN`, held budget receipts, schedule revision/UTC slot, PostgreSQL transaction and service logs. WSL shutdown does not prove 24/7 operation; continuous operation requires a separate powered host, two-VM isolation verification and staging gate.

No secrets should be copied into this repository. This test does not make purchases, posts, send mail, or access real provider endpoints.
