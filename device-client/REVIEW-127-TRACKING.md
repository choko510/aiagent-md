# PR 127 review tracking

Review baseline: `2fdcc32ad42b7439e2300b986bebfeb55b468d55`. Status is based on implementation evidence, not CI alone. Real device operations remain disabled.

| Finding | Status | Evidence or remaining work |
|---|---|---|
| P1-01 | PARTIAL | Test-schema challenges are channel-scoped; a 64-row limit and expiry cleanup are implemented. A valid existing proof survives a flood; fair admission under saturation and production migration remain open. |
| P1-02 | OPEN | Server-verifiable pairing transcript and generated contract required. |
| P1-03 | PARTIAL | `begin` rejects a missing control row under the writer transaction. `load_control` and `save_control` can still treat absence as initialization; independent rollback fencing and safe enrollment are missing. |
| P1-04 | OPEN | Core persistence and delivery fencing required. |
| P1-05 | OPEN | Authenticated effect history and cross-device semantic reservation required. |
| P1-06 | OPEN | Verified authority and fresh delivery fencing not integrated. |
| P1-07 | PARTIAL | Unauthenticated challenges no longer update identity restore state. Independent restore authority and barrier remain required; an old process can still act until a new proof succeeds. |
| P1-08 | OPEN | Ancestor-safe root registration required. |
| P1-09 | OPEN | Single writer and safe recovery required. |
| P2-01 | OPEN | Durable pairing lifecycle and safe key publication required. |
| P2-02 | OPEN | Bounded connections, database resource controls and cleanup required. |
| P2-03 | OPEN | Handle-based journal storage and per-identity key required. |
| P2-04 | OPEN | Handle-based identity store and atomic publication required. |
| P2-05 | OPEN | Session actor and operator trust provisioning required. |
| P2-06 | OPEN | Handle lifecycle, Unicode and I/O deadline required. |
| P2-07 | OPEN | Authenticated UI projection and legacy path separation required. |
| P2-08 | OPEN | Windows cross-language test is not a CI gate. |
