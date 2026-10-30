# Round 6 — PR-level lock readiness / all 61 Drafts
**No PR is LOCKED or authorized for automatic merge.** This assessment pins one HEAD per PR in `contract-lock-candidate.json` and distinguishes blockers:
- `CROSS_OWNER_CONTRACT_REVIEW`: changed or consumed shared semantics, requires sign-off on CL-01..12.
- `OWNER_REVIEW`: module itself has a documented Owner and may be internally coherent, but no actual Owner/independent reviewer approval at exact HEAD was available.
- `SPEC_LOCK_CANDIDATE` is a **future** state only after evidence of cross-owner approval; current none are marked so.
- Runtime verification is **separate** from design LOCK. The separate Harness code does not exist in this specs repo.

| Owner | PR(s) | Lock state | Needed next evidence |
|---|---|---|---|
| CHAT_10 |  | REVIEW REQUIRED | DB/migrations/Docker/operations; reviewed HEAD and cross-owner contract acceptance |
| CHAT_11 |  | REVIEW REQUIRED | QA/fault/benchmarks; reviewed HEAD and cross-owner contract acceptance |
| CHAT_12 |  | REVIEW REQUIRED | Radar/Entity/Trend/Risk/Source Reliability; reviewed HEAD and cross-owner contract acceptance |
| CHAT_13 |  | REVIEW REQUIRED | Event ingress/change/digest; reviewed HEAD and cross-owner contract acceptance |
| CHAT_14 |  | REVIEW REQUIRED | Learned Browser Actions/Feedback; reviewed HEAD and cross-owner contract acceptance |
| CHAT_15 |  | REVIEW REQUIRED | Multimodal parser; reviewed HEAD and cross-owner contract acceptance |
| CHAT_00 |  | REVIEW REQUIRED | Foundation: product requirements, architecture, glossary, ADR; reviewed HEAD and cross-owner contract acceptance |
| CHAT_01 |  | REVIEW REQUIRED | Canonical Domain / Events / State / Tool and Evidence types; reviewed HEAD and cross-owner contract acceptance |
| CHAT_02 |  | REVIEW REQUIRED | Agent runtime, checkpoints, lease and model orchestration; reviewed HEAD and cross-owner contract acceptance |
| CHAT_03 |  | REVIEW REQUIRED | Tool Registry / Gateway / Approval / Ledger / MCP; reviewed HEAD and cross-owner contract acceptance |
| CHAT_04 |  | REVIEW REQUIRED | HTTP, Browser Worker, Jev/Lightpanda/Scrapling; reviewed HEAD and cross-owner contract acceptance |
| CHAT_05 |  | REVIEW REQUIRED | Connectors: Amazon/Keepa, resale price, X, hotel; reviewed HEAD and cross-owner contract acceptance |
| CHAT_06 |  | REVIEW REQUIRED | Memory and Skill write authority; reviewed HEAD and cross-owner contract acceptance |
| CHAT_07 |  | REVIEW REQUIRED | Scheduler/Watch/Observation/Outbox; reviewed HEAD and cross-owner contract acceptance |
| CHAT_08 |  | REVIEW REQUIRED | REST/SSE/Web UI/Telegram; reviewed HEAD and cross-owner contract acceptance |
| CHAT_09 |  | REVIEW REQUIRED | Auth/secrets/isolation/SSRF/prompt injection; reviewed HEAD and cross-owner contract acceptance |

**Gates before removing Draft:** SHA-bound Owner acceptance; reject/accept alternatives, no inconsistent schemas; independent reviewer, meaningful real tests where spec depends on actual security semantics; main/rebase conflict check. All 61 are **HOLD** until recorded approvals and valid base compatibility. No merges performed.
