# Skill lifecycle (MKT-2) — implementation notes

Status: **IMPLEMENTED_PARTIAL / CODE_READY (unit-tested), runtime wiring NOT_IMPLEMENTED, PostgreSQL path CI-verified.**

This document records what the branch actually implements. It does not turn the Draft specs into Locked contracts.
`docs/06-memory-skills/skill-lifecycle.md` remains the authority for the lifecycle intent; this file describes the
current code and its limits.

## 1. What is implemented

| Concern | Code | Evidence |
|---|---|---|
| Bounded SKILL.md frontmatter | `aiagent_skills.frontmatter` | unit tests: object tags, duplicate keys, aliases rejected |
| Immutable manifest and digests | `aiagent_skills.manifest`, `digest` | unit tests via ingest/content digest |
| Archive safety | `aiagent_skills.bundle` | traversal, executable, nested archive, symlink, compression-ratio tests |
| Content quarantine | `aiagent_skills.quarantine` | temp-dir tests with digest re-verification |
| Deterministic scanner | `aiagent_skills.scanner` | high-severity findings report; PASS is not a safety proof |
| Capability narrowing | `aiagent_skills.policy` | intersection and forbidden-class tests |
| Review approval binding | `aiagent_skills.approval` | digest, revision, TTL, and tamper tests |
| Release/installation state machine | `aiagent_skills.lifecycle` | install/enable/update/rollback/revoke tests |
| Dependency resolution | `aiagent_skills.dependency`, `SkillRegistry.resolve_dependencies` | required dependency must be enabled; exact resolved versions enter the run pin |
| Registry orchestration and idempotency | `aiagent_skills.registry` | idempotent replay and conflicting-payload test |
| Run pins and progressive disclosure | `aiagent_skills.pinning` | pinned catalog, immutable backend, offset/truncation test |
| Source adapters | `aiagent_skills.sources` | GitHub/ClawHub require an injected `EgressPort`; upload is local |
| PostgreSQL persistence | `aiagent_skills.pg_store`, migration `0029` | CI integration test passes on Python 3.12 and 3.13 (migration, RLS, CAS, least privilege, pins); skipped locally without a server |
| Owner-scoped HTTP API | `aiagent_api.skills` | TestClient tests with an in-memory registry |
| Feature flag / app hook | `aiagent_api.app` | `AIAGENT_SKILLS_ENABLED=1` registers the router |

Skill content is always data. The package has no shell, subprocess, network client, tool executor, or evaluator.
Source fetches go through an injected egress port, and this branch does not include a production egress adapter.

## 2. Lifecycle and invariants

1. **Discover/resolve** — source adapters pin a publisher identity and a commit/artifact digest. A mutable branch or
   tag is never accepted as a release identity.
2. **Quarantine** — the archive is inspected and stored under a SHA-256 content address outside `SKILLS_DIR`.
3. **Validate** — bounded frontmatter, manifest/file-list verification, content digest, and the deterministic scanner.
4. **Review** — an approval binds tenant, owner, actor, action, release id, content digest, publisher, requested
   capability digest, policy revision, scan digest, expected installation revision, and a TTL of at most 24 hours.
5. **Install** — install never enables. Installation creates an `INSTALLED_DISABLED` record and an audit receipt.
6. **Enable** — a separate approval binds the exact release and activation revision, then increments
   `activation_generation`.
7. **Runtime primitives** — a run pin captures release/content/generation/policy/digests. `load_pinned_body`
   re-verifies the pinned body from quarantine; the catalog exposes only enabled releases and bodies are disclosed in
   byte-bounded chunks. The live Agent Runtime is not wired to these primitives (section 6).
8. **Update/rollback/remove** — each is a separate operation with an expected revision. A failed update restores the
   prior enabled/disabled activation instead of silently disabling a live Skill.
9. **Revocation** — moves the release and installation to revoked, removes it from the catalog, and prevents new pins
   and loads. It does not resurrect an older grant.

Requested capabilities only narrow. Effective capabilities are
`requested ∩ actor ∩ tenant ∩ run/task ∩ Gateway ∩ approved allowlist`. Classes such as `approval:`, `policy:`,
`secret:`, `shell:`, `sandbox:`, `gateway:`, `docker:`, and `script:` cannot be requested by a Skill.

## 3. HTTP surface

All routes are owner-scoped and require the existing bearer/session authentication. Mutating routes require
`Idempotency-Key`; enable/update/rollback/remove require `expectedRevision`. Installation and enablement are always
separate calls. Errors use stable codes such as `STALE_REVISION`, `STALE_CONTENT_DIGEST`,
`IDEMPOTENCY_KEY_REQUIRED`, and `SKILL_NOT_FOUND`.

```text
GET  /v1/skills
GET  /v1/skills/search?q=
GET  /v1/extensions/skills/installations
GET  /v1/extensions/skills/installations/{id}
GET  /v1/extensions/skills/releases/{id}
GET  /v1/extensions/skills/releases/{id}/files
GET  /v1/extensions/skills/releases/{id}/files/{path}
POST /v1/extensions/skills/ingest                  (raw ZIP body)
POST /v1/extensions/skills/releases/{id}/install
POST /v1/extensions/skills/installations/{id}/enable
POST /v1/extensions/skills/installations/{id}/disable
POST /v1/extensions/skills/installations/{id}/update
POST /v1/extensions/skills/installations/{id}/rollback
POST /v1/extensions/skills/installations/{id}/remove
POST /v1/extensions/skills/installations/{id}/revoke
POST /v1/extensions/skills/installations/{id}/pin
```

The feature is off by default:

```text
AIAGENT_SKILLS_ENABLED=1
AIAGENT_SKILL_QUARANTINE_DIR=<private path outside packaged skills>
AIAGENT_SKILL_POLICY_REVISION=<current policy revision>
```

File preview is `text/plain`, `X-Content-Type-Options: nosniff`, CSP `sandbox`, and `no-store`. Binary or oversized
files are refused; HTML is never rendered.

## 4. Database

Migration `0029_skill_lifecycle` adds:

- `skill_releases` (immutable release rows; state/revocation are the only mutations);
- `skill_installations` (owner-scoped, optimistic version, activation generation);
- `skill_installation_history` (rollback candidates);
- `skill_receipts` (idempotency replay);
- `skill_run_pins` (immutable run-scoped release pins);
- `skill_audit_events` (append-only; every successful registry operation writes one row).

Every table has RLS enabled and forced, using `current_setting('aiagent.tenant_id')`. Composite
`(tenant_id, id)` foreign keys prevent cross-tenant references. The API role gets the minimum DML grants; the runtime
role gets read-only access to releases, installations, and pins.

**Numbering risk:** PR #122 also introduces `0029`. This branch was cut from `origin/main` `53ac14c` before that
migration merged. If PR #122 merges first, this migration must be renumbered to `0030` and
`APP_SCHEMA_VERSION` bumped again. Contiguous migration validation must not be weakened.

## 5. Security properties and explicit limits

- Archive inspection rejects traversal, absolute/UNC paths, ADS/colons, NUL/control/format characters, non-NFC names,
  Windows reserved/trailing-dot names, nested archives, executables and binary magic, symlinks/hardlinks/devices,
  encrypted entries, duplicate/case-colliding names, `.git`, excessive entries/depth/size, and compression-ratio
  bombs.
- YAML is composed without object construction; duplicate keys, unsupported tags, recursive aliases, node/depth limits,
  and non-finite floats are rejected.
- Quarantine is content-addressed, written via a staging directory and atomic rename, and re-hashed on read. The
  runtime/model has no read path to it; only the API owner-scoped preview reads it.
- Approval and pin checks are constant-time string comparisons and bind exact digests/revisions, preventing TOCTOU
  between review and enable. A run pin must carry the digest of the release's validated capability request; a caller
  cannot substitute an arbitrary capability digest.
- The scanner is heuristic. A passing scan is **not** proof of safety; high-severity findings are review signals.
  Only structural violations are fail-closed.
- The API creates the review approval server-side from the authenticated actor and the exact release digest. It does
  not accept a client-supplied capability set, policy revision, or scan digest. The configured policy revision is a
  deployment input; a real policy-service revision feed is not implemented here.
- No Skill is executed. Scripts, package managers, host shells, MCP installation, and remote Git execution are outside
  this implementation.

## 6. Runtime integration status (important)

The existing runtime still mounts packaged read-only `SKILLS_DIR` at `/skills/` and caches its DeepAgents graph per
profile. It does **not** yet consume `EnabledSkillCatalog`, `ImmutableSkillBackend`, or `RunSkillPin`. Therefore:

- installing/enabling a Skill through this API does **not** currently change the model's skill catalog or tools;
- run pins and on-demand disclosure are implemented and tested as library contracts (`SkillRegistry.load_pinned_body`
  reads the digest-verified quarantine object), but are not injected into a live run;
- wiring requires a run-scoped graph/backend keyed by tenant, owner, run, and pin, plus a runtime policy revision feed.
  That is deliberately left as the next integration step rather than faking per-run behavior in a cached graph.

This is the main reason the branch is `IMPLEMENTED_PARTIAL`, not `IMPLEMENTED`.

## 7. Tests and verification

Executed in this branch on Windows:

```text
uv run pytest packages/skills/tests -q          # 34 passed
uv run pytest tests/test_skill_api.py -q        # 5 passed
uv run pytest tests/test_skill_pg.py -q         # skipped locally: no PostgreSQL
# CI integration: 1 passed (Python 3.12 and 3.13)
uv run ruff check packages/skills packages/api/src/aiagent_api/skills.py
uv run mypy packages/skills/src packages/api/src/aiagent_api/skills.py
```

The PostgreSQL migration/RLS/CAS/least-privilege test executed in GitHub Actions run `45665909124` on both Python 3.12
and 3.13. Not executed locally: full API lifespan, Docker/VM isolation, GitHub/ClawHub network resolution, and any live
Agent Runtime run. Those remain unverified and must be run in the deployment environment before claiming production
readiness.

A deterministic disclosure-budget test (`test_disclosure_benchmark.py`) builds 40 Skills with 16,000-line bodies and
asserts that the enabled-catalog surface is under 1% of the full body bytes, and that one disclosure slice stays within
16 KiB. This is a byte-budget regression check, not a measurement of a particular model's tokenizer.

## 8. Review hardening (2026-11-14)

Commits `ca224d5`..`c331174` address the PR #125 review findings that can be closed without inventing a production
review UI or Gateway integration. GitHub Actions run `45956073812` is green on `c331174`; the PostgreSQL integration
test seeds a real owned Run and exercises migration `0029`, RLS, CAS, receipts/audit and pin persistence:

- Atomic lifecycle transactions: `_receipt` now runs the state mutation, receipt, and audit append in one PostgreSQL
  transaction, serialized per tenant with an advisory lock. The in-memory store snapshots and restores on failure.
- Run pins require a run authorizer. The API supplies `run_is_owned`, which checks tenant, task owner, and terminal run
  status; the PostgreSQL integration test seeds a real owned Run and verifies the pin FK. The registry fails closed
  when no authorizer is configured.
- Run pins capture the exact enabled dependency closure (release, content digest, installation, generation, version),
  and enable re-resolves required dependencies before activation.
- Pinned body loading uses the pin's release, so an update cannot swap the body under a running Run; revocation is
  fenced on every pinned load.
- Update/rollback require stable source lineage (publisher + catalog key, including GitHub subdirectory), not just the
  same Skill name.
- Suspended installations can be re-enabled and updated without silently becoming enabled; repeated revoke is a no-op
  and rollback restores policy revision.
- Approval binding includes publisher, request, installation, target release and enable/reenable effects; TTL is capped
  at 24 hours and creation time is validated.
- Release uniqueness is owner-scoped, and installation/release/pin foreign keys carry the owner where required.
- UTF-8 disclosure slices never split a code point; SemVer build metadata and prerelease exact constraints are
  preserved; GitHub zipball wrapper and subdirectory are normalized; the enabled catalog no longer truncates at 50.

Still open / partial: independent persisted reviewer identity and one-time approval consumption (P1-01/P1-02), Gateway
effective-capability evidence (P1-06), owner-aware RLS beyond application scoping (P1-11), signature verification,
quarantine GC, API pagination, and live runtime wiring. These are not represented as fixed.

## 9. Second-review hardening (A2/B2, still Draft)

This pass keeps `AIAGENT_SKILLS_ENABLED` default-off and does not claim `CODE_READY` or `MERGE_READY`. The
original review IDs are authoritative; the earlier PR-body P2-05..P2-10 numbering was shifted and is not used to close
findings.

Implemented in the working tree after `690292d`:

- **A2-02 revocation epoch:** `SkillInstallation` and `RunSkillPin` carry a monotonic `revocation_epoch`.
  Disable, remove and revoke increment it; enable/update/rollback preserve it. Pinned load verifies the epoch, so
  disable -> re-enable cannot revive an old Run pin. Normal update still allows the old Run to read its immutable
  snapshot, while a new pin for that Run is refused.
- **A2-02 dependency closure:** the enabled catalog now omits a Skill whose required dependency is disabled, revoked,
  moved or otherwise unresolvable. Pinned loads recompute the closure digest and verify each dependency's exact
  release, installation, generation, content digest, name and version.
- **A2-03/A2-04 fences:** pin replay is checked against the live installation generation/epoch/release; the public
  mutations look up an existing receipt before release/state prechecks, so a retry after a later mutation returns the
  original receipt instead of a stale-revision error. Idempotency payloads mirror the HTTP request body.
- **A2-06 ingest:** upload parsing, scanning and quarantine writes run in Starlette's bounded thread pool instead of
  blocking the event loop. This is bounded threadpool offload, not a durable job queue.
- **A2-09/B2-03/B2-06 bundle hardening:** raw GitHub zip metadata is validated before wrapper rewrite (entry count,
  encryption, compression method/ratio, traversal, symlinks, total expansion, bounded member reads); bundles reject
  file/directory prefix collisions, reserved `.complete`/`.manifest` markers and nested archive magic; manifest
  signature wire parsing maps `keyId` explicitly and rejects unknown fields.
- **B2-02/B2-05 identity:** GitHub subdirectory case is preserved in the catalog key; ClawHub requires an exact SemVer
  and a slug that is bound into the catalog key. The scanner's 500-finding cap is global across files.

Additional tests: 21 regressions in `backend/packages/skills/tests/test_review_regressions.py`, one API receipt-replay
test, and a PostgreSQL disable -> re-enable pin test. Local package/API runs are green; PostgreSQL and the live
runtime path still require CI/deployment evidence.

Still open after this pass: independent persisted reviewer identity and one-time approval consumption (A2-01/A2-05),
run-scoped runtime graph/backend and Gateway-mediated Skill loading (A2-03/A2-07), owner-aware RLS (A2-07), publisher
evidence/raw artifact digest (A2-08), quarantine GC and a durable ingest queue (A2-06), signature verification/trust
policy, and the independent review UI.
