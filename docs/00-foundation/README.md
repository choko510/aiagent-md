# Foundation — Architecture and Product Authority
> CHAT_00 Draft, 2026-10-30. This folder is the entrypoint for the Universal Personal Agent Harness specification; **not a release-ready implementation**.
## Read first, in order
1. [Project Brief](project-brief.md) — original user-approved intent/non-goals.
2. [Product Requirements](product-requirements.md) — specific MUST outcomes and SC mapping.
3. [System Architecture](system-architecture.md) — component boundaries and flow.
4. [Domain Glossary](domain-glossary.md) — semantic terms with canonical owners.
5. [Architecture Decision Register](architecture-decisions.md) — proposed vs adopted decisions and blockers.
6. [Acceptance Scenarios](acceptance-scenarios.md), [Review Gates](review-gates.md), [Spec Index](spec-index.md), [Architecture Rules](architecture-rules.md).
## Canonical owner rule
A foundation ADR **never silently supersedes** CHAT_01's TypeScript/Event definitions, CHAT_02 Runtime, CHAT_03 Tool/Gateway, CHAT_09 security or CHAT_10 migrations. Discrepancy: record `Cross-team proposal` with source PR HEAD, field, alternative, migration, TEST-ID and owner; revise **only the owning PR** after acceptance. Current 58PR/80MD are drafts, not source of truth on main. Foundation submission closes only missing writing coverage; it does not unlock automatic merges.
## Verified versus designed
2026-10-30 Round4 independent test branch executed draft PostgreSQL 16 initial DDL, synthetic concurrency/FK, generic Docker policy primitive, local Chromium and mock SSRF fixtures. These **do not validate the unimplemented Agent Harness**. Provider credentials, real Browser/Gateway confinement, migration upgrades, X entitlement, E2E notification/payment effects require explicit, separately evidenced testing.
## Current integration gates
X subscription ingress/receipts, canonical retailer/storefront identity, digest member delivery/UNKNOWN, multimodal raw-to-derived evidence/parser confinement, browser action side-effect trap, source/Watch observation lineage and feedback memory mutation remain cross-owner `PROVISIONAL`. See [decision register](architecture-decisions.md) for owners and exact evidence needed.
## Authoring boundaries
All specs: MUST/SHOULD/non-goals; typed ports/schema; determinism and budget; race/crash/replay/security; evidence/version/provenance; alternatives; Unit/Integration/E2E/Fault/Security/Benchmark; REQ→TEST→SC. For exact mapping see `docs/00-foundation/review-gates.md`. Only owner branches may modify corresponding MDs. No merge without maintainer approval.
## Release gate
`Draft`=proposed, `Reviewed`=independently examined, `Locked`=shared contracts settled and implementable, `Implemented`=code exists, `Verified`=actual tests passed. Green prose/CI smoke alone is never production approval.
