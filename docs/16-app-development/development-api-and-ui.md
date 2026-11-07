# DEV-09 — Development Workspace UI, REST & Event Proposals
> Draft / PROVISIONAL | 2026-11-07 | Owners CHAT_01/03/04/07/08/09/10。既存 `/api/v1`、authenticated CAS/Idempotency/SSEを拡張。UI未実装。

## 1. 主要画面
- **Projects**: GitHub import/create、target framework、branch、Repo/Project/Worktree、commit history、差分・テスト・コスト・権限を一覧。
- **Development Workspace**: 指示チャット/Task timeline、ロール別8AI進捗、編集中ファイル・diff・terminalのredacted output、commit SHA、test result、Browser preview、live debug、P0/P1/issues/Expert Consult。通常のタスク画面を消さず独立のタブ。
- **Design Picker**: 同一仕様・データで生成した3候補のthumbnailを並べ、desktop/mobile、light/dark、拡大、keyboard/a11y、各候補の動くDemoへ移動、部分採用・再生成・ユーザーフィードバック。
- **Demo Review**: 同一DesignRevisionの実際に動くブラウザアプリ（mock箇所を明示）、Approve/RequestRevision/Reject、Design/Demo approvalと外部Effect承認は別の表示。
- **Endless Run**: mode (optimization/uiux/feature_factory/bug_hunter/custom)、件数10等、continuous、使うモデル/Expert budget、UI判断、quota recovery、current/target verified progress、未決Demo別queue、Stop/Pause/Resume、対象限定。
- **Browser Debug Studio**: (存在すれば)リアルタイムObserve-only、静止画/操作時系列、Console/Network/DOM/Trace、問題検出領域・再現手順・関連ソース/修正PR。Human takeoverは別権限。機密情報のFrameは非表示。

## 2. HTTP route proposals（既存REST正本のownerレビューが必要）
| method | route | Scope | semantics |
|---|---|---|---|
| POST | /api/v1/development/projects | project:create | immutable import/create Intent→202 |
| GET | /api/v1/development/projects/{id} | project:read | authorized snapshot+ETag |
| POST | /api/v1/development/projects/{id}/cycles | dev:run | 8AI DAG dispatch receipt |
| GET | /api/v1/development/cycles/{id} | dev:read | progress+child refs+latest SHA |
| GET | /api/v1/development/cycles/{id}/design-candidates | dev:read | metadata/sanitized screenshot refs |
| POST | /api/v1/development/cycles/{id}/design-decision | dev:decide | immutable candidate/revision, CAS |
| POST | /api/v1/development/cycles/{id}/demo-decision | dev:decide | approve/reject/revise, snapshot digest |
| GET | /api/v1/development/cycles/{id}/reviews | dev:read | SHA-pinned 3-review / finding list |
| GET | /api/v1/development/cycles/{id}/debug-evidence | debug:read | privacy-scoped artifacts |
| POST | /api/v1/campaigns | campaign:create | typed goal, scope/budget, 202 |
| GET | /api/v1/campaigns/{id} | campaign:read | progress, budget, blocked reasons |
| POST | /api/v1/campaigns/{id}/commands | campaign:control | pause/resume/stop/steer, CAS |
| GET | /api/v1/campaigns/{id}/events | campaign:read | signed SSE cursor + GET reconciliation |

Write endpointsには `Idempotency-Key`、`If-Match`/expected version、CSRF/Origin、signed principal scope。POST応答202は実行完了でない。新たな Tool execute/CDP/raw socket/secret CRUDは公開しない。ユーザー入力のowner/tenant/policy/model identityに従って権限増やさず、server DBから再取得。WebUIのBack/refresh/network断ではGET snapshot とSSE cursorから確定状態を同期し、tentative progressを完了として記録しない。引数構造、error taxonomy、multi-tenant identity/ACL、SSE unified offsetはOwnerがshared contractを承認してから固定する。

## 3. User preference contracts
```ts
type DevelopmentPreferencesProposal = {
 designChoice:"AUTO"|"USER_CHOICE";
 designPolicy?:"AUTO_RECOMMEND"|"REQUIRE_USER_SELECTION";
 demoAcceptancePolicy?:"REQUIRE_USER_APPROVAL"|"PREAUTHORIZED_BOUNDED_AUTO";
 styleCandidates:number; // bounded; same content / fixtures for all
 expertAdvisorEnabled:boolean; expertProfileRef?:string;
 reviewRoles:["functional","security","ui_e2e"];
 releasePolicy:"LOCAL_ONLY"|"PR_AFTER_APPROVED_SCOPE"|"REQUIRE_PER_ACTION_APPROVAL";
 campaignMode?:"full_optimization"|"uiux_evolution"|"feature_factory"|"bug_hunter"|"custom_goal";
 targetCount?:number; continuousUntilStopped?:boolean;
 quotaPolicy?:"STOP"|"WAIT_FOR_KNOWN_RESET";
};
```
安全承認はこれとは別の Tool Gateway ApprovalRecord。UI方針のAutoは本番公開や不可逆操作のAuto approveを意味しない。SDKがある場合も実権限はOwner policy intersection。Demo選択後にapproved manifestを固定し、変更があれば再レビューを求める。

## 4. Events
`design.proposal.created`, `design.decision.requested`, `design.decision.recorded`, `demo.ready`, `demo.approval.requested`, `demo.decision.recorded`, `cycle.started`, `review.round.completed`, `debug.finding.created`, `cycle.verified`, `campaign.progress.committed`, `campaign.quota.wait`, `campaign.stopped` は提案名（正式enumは01）。Eventはtenanted, goalRevision, commit SHA, artifact refs, server eventId, durable offsetを含む; raw code/PII/token/HTMLは含めない。SSEはnotification hintでread modelが正本。

## 5. Tests
DEV-UI-T01: 3 candidate thumbnail同一fixtureで選択、キーボード/モバイル対応。
DEV-UI-T02: 旧Design/Demo版をApproveすると409/412で拒否。
DEV-UI-T03: Demo承認が購入/デプロイ承認にならない。
DEV-UI-T04: Viewerとdebug操作者の権限分離、CDP URL配布0。
DEV-UI-T05: browser/terminal raw sensitive contentがWeb client/Traceへ漏洩0。
DEV-UI-T06: Stop UI受付と実停止完了を区別、SSE断線復元で重複count0。

## 2026-11-08 P1-01/P1-02/P1-07 — Action-specific permissions and API preference compatibility

The former `demoChoice:"AUTO"` is **deprecated as ambiguous**. New clients send `designPolicy:"AUTO_RECOMMEND"|"REQUIRE_USER_SELECTION"` separately from `demoAcceptancePolicy:"REQUIRE_USER_APPROVAL"|"PREAUTHORIZED_BOUNDED_AUTO"`. Feature Factory defaults to REQUIRE_USER_APPROVAL. Old requests with `demoChoice=AUTO` **must not** silently accept a Demo: reject with an actionable migration error or conservatively map only to automatic recommendation while preserving human approval; never auto-implement.

A usable Demo uses `demo:interact` only in a synthetic/mock BrowserContext and short-lived exclusive input lease; `preview:observe` remains read-only; `browser:handoff` is a separate sensitive takeover. Token/sensitive credential never reaches front-end. Route handlers look up server principal and Project/Cycle/Preview binding again on every command. Any stale manifest, controlVersion, identityVersion, policyEpoch, project ownership or browser generation => 409/412 (or authorization 403/404). UI must show which permission is being requested and clearly mark mock actions.

| Resource | Action | Authorized actor | Scope |
|---|---|---|---|
| project | `project:create/read/edit` | authenticated owner or explicit project-scoped ACL | tenant + owner + project |
| dev cycle | `dev:run/read/decide` | project owner, separately scoped delegate on read-only operations | project + cycle + goal revision |
| preview | `preview:observe` | authorized viewer | project + preview + TTL; no input |
| demo | `demo:interact` | authenticated authorized demo viewer | synthetic fixture only + exclusive lease |
| browser | `browser:handoff` | separate sensitive approval subject | existing Browser Worker handoff rules |
| debug | `debug:read` | project owner or narrow debug-reader ACL | redacted artifacts only |
| campaign | `campaign:create/read/control` | owner for start/stop/steer, scoped view for sharing | campaign + controlVersion + budget |
| integration | `dev:integrate` | project owner, branch policy plus Integration Gate | expected HEAD + receipt |
| release | `dev:release` | owner + separate Gateway deployment approval | release target, digest, approved limits |

Additional proposed routes: `POST /api/v1/development/previews/{id}/commands` accepts only enumerated synthetic action kinds and a scoped exclusive lease; `POST /api/v1/development/projects/{id}/integrations` is expectedHeadSha-bound; `GET /api/v1/development/projects/{id}/integration-receipts` exposes current HEAD quality state; `GET /api/v1/campaigns/{id}/quota-wait` exposes redacted receipts and confirmed vs estimated reset. The generic REST framework's Idempotency-Key/If-Match/CSRF/fresh authorization apply to all mutations. No `GET` has side effects, and an ACK is not a completed operation.

### Compatibility / Event proposals
Map the new actions in [Auth owner](../09-security/authentication-and-user-isolation.md), domain events in [Event envelope](../01-contracts/event-envelope-and-stream.md) and SQL in [DB owner](../10-infrastructure/database-schema-and-migrations.md); they are not automatically locked by this draft. Event additions: `preview.session.revoked`, `demo.interaction.admitted`, `feature.integration.verified`, `campaign.control.changed`, `campaign.quota_wait.recorded`, `campaign.count.reconciled`. Event payload must contain safe ID/receipt references, no raw preview capability token, no raw HTML/JS, no secret.

**DEV-UI-T07** Old `demoChoice=AUTO` cannot cause unapproved implementation.
**DEV-UI-T08** Same-tenant different-user caller with project-only read role cannot decide demo/stop campaign.
**DEV-UI-T09** Preview observe allows viewing but not typing; `demo:interact` synthetic action works without sensitive browser takeover.
**DEV-UI-T10** stale actor/preview/session/control lease or unauthorized shared-repo owner → deny, no effect/event.
