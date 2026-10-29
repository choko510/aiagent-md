# Login Challenges and Human Handoff — Detailed Implementation Specification

> CHAT_04 | `docs/04-web-browser/login-challenges-human-handoff.md` | Status: **Draft / PROVISIONAL** | Rev 0.1 | 2026-10-30 JST | Main `b45d4e989998663a00ef09673e69ae1f4191f00d` | **Harness tests executed: 0**

## 0. 採用設計・非目標・理由
ログイン画面、2FA、CAPTCHA、Turnstileなどは自動突破しない。BrowserSessionが認証チャレンジを検知したとき、Agent操作を一時停止し、認証済みユーザーに隔離ブラウザの**短期/単回Takeover ticket**を渡す。人間が操作を終えたあと、Session/Owner/Fence/タブ/権限を再確認してTaskへ復帰する。Cookieや認証画面の入力値はモデルへ共有しない。

**採用:** Browser Worker所有のContextを保持し、Human-only modeへDB CASでlease切替、one-time ticket with audience/user/tenant/task/session binding、expire/revoke、state再観測。
**却下:** Agentが2FA/CAPTCHAを勝手に処理することを成功保証、CDP remote endpointをユーザーに公開、HumanとAgentが同時に同じTab操作、CookieをLLM/RESTへ返す。
**非目標:** ログイン資格を収集してモデルに保存すること、OTP/TOTP/Turnstile自動回答、ユーザーの承認なしの予約/購入、サーバー管理権限の貸与。

## 1. Scope / Goal / Ownership / SC
Challenge detectionはDOM/HTTP/header/視覚表示を根拠として確度付きで扱う。Skyvernのtyped evidence carrierとJevのbot-protection観測を参考にするが「チャレンジが表示された」ことと「正規ログインできた」ことは別。Human takeoverの画面配信技術/REST API詳細はCHAT_08が所有し、ChatGPTへのスクリーンショットなどは明示したredaction後以外禁止。
関係するシナリオ: SC-06, SC-08, SC-07, SC-04, SC-09。SC-10/12/14のEntityと変化検出はCHAT_12/13の所有物で、このモジュールが勝手に「同じ商品・重大な変化」を確定しない。

## 2. Source-code audit / テスト・license
[S1] **Playwright** at `62d25ac221370f6fc5e26c600823792b83c67f1e` (2026-10-30, Code verified: source/test read, not executed; Apache-2.0): [`packages/playwright-core/src/server/browserContext.ts — storageState / setStorageState`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/packages/playwright-core/src/server/browserContext.ts) · [`tests/library/browsercontext-storage-state.spec.ts — restore/state isolation`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/tests/library/browsercontext-storage-state.spec.ts).
[S2] **Skyvern** at `fa2c00ff7ec9599b495cc52d24a1363613937895` (2026-10-30, Code verified: source/test read, not executed; 導入時にLICENSE確認（再利用しない）): [`skyvern/forge/sdk/copilot/challenge_evidence.py — typed challenge evidence`](https://github.com/Skyvern-AI/skyvern/blob/fa2c00ff7ec9599b495cc52d24a1363613937895/skyvern/forge/sdk/copilot/challenge_evidence.py) · [`skyvern/forge/sdk/copilot/tools/page_challenge.py — challenge flow`](https://github.com/Skyvern-AI/skyvern/blob/fa2c00ff7ec9599b495cc52d24a1363613937895/skyvern/forge/sdk/copilot/tools/page_challenge.py).
[S3] **Jev browser** at `817b09eea6e21aad836b5ed32a613c68e5644a08` (2026-10-30, Code verified: source/test read, not executed; 導入前LICENSE確認): [`src/password.ts — parseTrustedOrigin/readHandoffSecret/makeRedactor`](https://github.com/jkudish/jev-browser/blob/817b09eea6e21aad836b5ed32a613c68e5644a08/src/password.ts) · [`test/unit.test.mjs — password handoff one-shot/redaction tests`](https://github.com/jkudish/jev-browser/blob/817b09eea6e21aad836b5ed32a613c68e5644a08/test/unit.test.mjs) · [`src/navigate.ts — page injection and bot challenge inspection`](https://github.com/jkudish/jev-browser/blob/817b09eea6e21aad836b5ed32a613c68e5644a08/src/navigate.ts).
Playwright BrowserContext.storageStateとtestsはcookie/localStorage等の保存を検証しているがsessionStorage復元・2FA突破を保証しない。Skyvern `challenge_evidence.py`はChallengeEvidenceSourceとtyped carrierを持ち、単なるHTML文字列のキーワード一致だけを確定証拠にしない設計が参考になる。Skyvern `page_challenge.py`にsolver codeが存在しても本HarnessへのCAPTCHA自動突破採用根拠にはならない。Jev `password.ts`はexact-originと単回handoff secret/redactorのコード、test/unit.test.mjsでredaction/one-shot file readを試験。コードのSecret deliveryは参考にするが本Harnessでhost file pathをLLMに渡すものではない。
Code verifiedは**ソースの内容を確認した**の意であり、元の自動テストを実行したとの意味ではない。OSSベンチマーク/コミュニティIssue/PR網羅調査は未完。プロジェクト固有設計をCode verifiedと呼ばない。

## 3. 依存図 / Trust Boundaries
```text
Agent / Tool Gateway → BrowserSession ACTIVE_AGENT → detect challenge → WAITING_INPUT(DB checkpoint)
Verified User (CHAT_09) → REST (CHAT_08) one-time ticket → Browser Worker HUMAN mode
Release/expire/revoke → epoch CAS+auth probe → ACTIVE_AGENT or WAITING_INPUT/EXPIRED
```
- BrowserSession/PageはCHAT_04 Browser Worker所有。Tool approval/side effect classificationはCHAT_03正本。Secret/Authenticator/egressはCHAT_09正本。REST+WebSocket/SSE UXはCHAT_08、state/Event/Evidence/Error/公開UUIDv7はCHAT_01、Task/Run checkpoint/goalRevisionはCHAT_02、物理DDLはCHAT_10。
- CHAT_01のBrowserSessionはtenant+task+optional activeRunIdに束縛。Task.conversationIdは**nullable**で良い設計だがCHAT_10とCHAT_08が異なるDraft。外部から`tenantId/ownerUserId`の任意指定を受けない。
- `recordVersion`初期1 vs DB `revision`初期0、Run `leaseEpoch` number vs bigint/DB列不足、UUIDv7 vs DB `gen_random_uuid()`、Tenant vs Workspaceは**未合意P0**。具体的CASで旧ownerが操作できない保証をOwnerに提案。本書の型はローカルで[PROVISIONAL]。

## 4. Requirements
| ID | Level | 要件 | 期待値（assert可能） | SC |
|---|---|---|---|---|
| HND-001 | MUST | Challengeは確証/疑いを区別し根拠を保存 | 単なるキーワードだけで確定しない | SC-06 |
| HND-002 | MUST | Takeover開始は認証済本人とTask/tenantの所有者照合 | 他ユーザーticket発行0 | SC-06/SC-08 |
| HND-003 | MUST | Ticketはone-time/短期/target session+user+audience bound | replay/他device利用拒否 | SC-06/SC-08 |
| HND-004 | MUST | AgentとHumanのleaseは排他、fencing epoch更新 | 同Tab二重操作0 | SC-06/SC-07 |
| HND-005 | MUST | Handoff中TaskはWAITING_INPUTし新Action発火しない | Background click0 | SC-06/SC-07 |
| HND-006 | MUST | Ticket/URLにCookie/Password/CDP endpointを含めない | 認証情報/endpoint露出0 | SC-08 |
| HND-007 | MUST | Human操作はisolated Browserに閉じ、host OSアクセス不可 | ファイル/シェル特権なし | SC-08 |
| HND-008 | MUST | 2FA/CAPTCHA/Turnstile処理は人間へ委譲し自動成功と断定しない | challengeの成功率保証なし | SC-06/SC-08 |
| HND-009 | MUST | release時に本人/epoch/auth state/Tab/Goalを再検証 | 未ログインならTask再開0 | SC-06/SC-07 |
| HND-010 | MUST | logout/revoke/cancel/expiry時にTicket/lease失効 | 失効後Human action0 | SC-07/SC-08 |
| HND-011 | MUST | Steer中の旧承認やTakeover ticketをinvalidate | 旧revisionでrelease不可 | SC-07 |
| HND-012 | MUST | lost ack/worker crash時はHuman/Agent双方の再接続をfence | 重複ticketと副作用0 | SC-07 |
| HND-013 | MUST | Cookie/storageStateは暗号化し本人/Session scopeを維持 | 別owner restore 0 | SC-06/SC-08 |
| HND-014 | MUST | Login完了後も元Taskの要求/費用/外部副作用承認を維持 | loginを支払い承認に転換しない | SC-04/SC-08 |
| HND-015 | SHOULD | Takeover中に見えた秘密の画面録画/スクショを既定無効 | PII artifact 0 | SC-08 |
| HND-016 | LATER | 自動challenge classifiersを実測で改善 | positive/false rates報告後採用 | SC-09 |

## 5. Type/API/Event/Validation (CH04 local)
```typescript
type ChallengeObservation = {schemaVersion:1; sessionId:string; tabId:string; detectedAt:string; kind:"login"|"two_factor"|"captcha"|"access_denied"|"unknown"; certainty:"suspected"|"confirmed"; evidenceRefs:string[]; source:"dom"|"http"|"vision"};
type HandoffRequest = {sessionId:string; taskId:string; expectedEpoch:string; goalRevision:string; reasonRef:string; requestedBy:string};
type TicketClaim = {opaqueTicketRef:string; userId:string; tenantId:string; taskId:string; browserSessionId:string; audience:"browser-takeover"; expiresAt:string; nonce:string; leaseEpoch:string; consumed:boolean};
type HandoffResult = {status:"waiting_input"|"human_active"|"revalidate"|"resumed"|"expired"|"denied"|"uncertain"; sessionId:string; observedAt:string; verificationRef?:string};
```
Ticketのnonceは予測不能な暗号学的random、DBにはhashのみ、5分TTLの初期提案。Cookie/Authorization/Session tokenはTicketではない。Takeover ticketをURL queryへ入れないことを推奨（一次限りPOST消費）。User token/sessionの再認証はCHAT_09が正本。
- schemaVersion未知major拒否、オプションのmissing/null/unknownを区別。ブラウザ/Task/Runのpublic IDはCHAT_01 UUIDv7 validatorに委譲し、Browser internal id、Artifact hash、Ticket nonceと区別。
- Tool Gatewayから承認済みoperationRef、tenant-qualified ownership、Run/BrowserSession lease/goalRevision snapshotを受け取り、send/commit前に**最新版のfence**を照合。リプレイ時に静的IDだけで権限を証明しない。

## 6. 正常Sequence / 状態遷移
```text
ACTIVE_AGENT → CHALLENGE_DETECTED → WAITING_INPUT → HUMAN_ACTIVE → REVALIDATING → ACTIVE_AGENT
WAITING_INPUT/HUMAN_ACTIVE → EXPIRED|CANCELLED|REVOKED
HUMAN_ACTIVE→WORKER_CRASH→REVALIDATING_or_WAITING_INPUT
EXPIRED/CANCELLED terminal for ticket, new ticket requires new approval
```
1. BrowserWorkerがchallengeを証拠とともに検出→2. Safe checkpoint/intent保存→3. Agent leaseを停止+epoch更新→4. APIで認証済owner確認→5. one-time ticket発行→6. WebUIがTicketを消費→7. BrowserWorkerにhuman-only secure display/input→8. Human release→9. owner/lease/goalRevision/Tab/Login stateを再検証→10. Agent mode lease再取得しResumeを通知。

## 7. アルゴリズム（疑似コード）
```typescript
async function requestHandoff(req,principal) {
 const session=await sessions.readForUpdateByTenant(req.sessionId,principal.tenantId);
 assertOwnerAndTask(session,principal); assertValidRunGoalEpoch(session,req);
 await gatewayCheckpointWaitingInput(req.taskId,req.sessionId);
 await sessions.casModeEpoch(session,"AGENT","HUMAN_PENDING");
 return await ticketStore.createHashedOneTime({user:principal.userId,
     tenant:principal.tenantId,task:req.taskId,session:req.sessionId,
     audience:"browser-takeover",epoch:session.leaseEpoch,ttlSeconds:300});
}
async function releaseHandoff(ticket,principal) {
 const claim=await ticketStore.consumeAtomically(ticket,principal);
 await sessions.assertHumanLease(claim);
 await browserWorker.stopHumanInput(claim.session);
 const observed=await browserWorker.recheckAuthAndTab(claim.session);
 if(!observed.authenticated) return "WAITING_INPUT";
 await sessions.casModeAndIncrementFence("HUMAN","AGENT",claim);
 return checkpoint.resumeAfterFreshGoalPolicyValidation(claim.task);
}
```
関数名は実装提案であって既存OSS API名を捏造したものではない。Gateway/DB/Secret Storeの呼出しが正式Contract確定前に[PROVISIONAL]であることを明示。

## 8. 失敗/競合/キャンセル/Steer/Restart
| 状況 | 検知 | 復旧 | 最終状態 |
|---|---|---|---|
| challenge suspected only | low evidence certainty | capture second bounded observation | UNCERTAIN |
| ticket replay | nonce already consumed | reject, audit | DENIED |
| different owner | Task/tenant mismatch | no takeover | DENIED |
| human disconnect | connection dead | lease grace→expire; no agent auto-resume | WAITING_INPUT |
| login cookie missing | auth probe fails | re-handoff | WAITING_INPUT |
| Run cancel while human active | revision/lease stale | revoke + isolate Browser | CANCELLED |
| Browser crash | tab state lost | invalidate ticket / new owner claim | RECOVERING |
| user clicks purchase | side effect intent missing | block/reconfirm approval, do not treat handoff as approval | NEEDS_APPROVAL |
- Controllerの複数worker claim時はepoch fenceにより一方のみ取得。旧workerは遅延完了を現行結果へcommitできない。
- 意図保存→外部Action→結果保存の間にKillしたら`UNKNOWN`を識別。ログイン操作やボタン押下はread-onlyと断定できない。不可逆な外部Actionの再送は禁止。Cancel/Steer後は新Action開始0件。

## 9. Security/Privacy/Limits/Operations
One-time ticketはnonce hashとサーバー側消費、短いexp、User/Tenant/Task/BrowserSession/audience/epoch binding。人間Browser画面もe.g.クロスオリジンのpop-up/file upload/clipboardにアクセス制限し、Local hostファイルと共有しない。人間の入力Cookie/password/OTPをLLMから完全に隔離。録画/スクショは既定無効、ページ内JSやプロンプト注入はPolicy変更できない。サイトとユーザーが許す認証手続きを尊重。
- ページのHTML/Script/Metadata/視覚テキストはuntrusted data。DOM内「システム指示」は無権限。認証・秘密・PIIはLLM、Trace、Raw Artifactへ出さない。制限付きResourceとして必要最低限保持。
- Host Shell/SSH/Docker socket/privilegedコンテナ禁止。購入/予約/投稿/送金の承認なし実行禁止。
- **予算初期提案**: ticket TTL5分、Human idle timeout15分、Task毎同時ticket1個、1 Taskで再handoff最大3回、worker 1 Session/agent tab per mode（提案値）。Gatewayの厳格な総量上限・ユーザー拒否が優先。外部請求も含めfinite。
- Metrics: task/run/owner-safe trace ID、epoch、toolAttempt、source snapshot checksum、before/after status、observation time、parse success、unknown率、latencyMs、memory RSS、redaction failures、model token/total cost。原文・URL tokenはログではmask。
- 同一fixture（日本語/英語、HTML変更、タイムゾーン差、認証済み/非認証、滞留ページ）の成功/誤判定率と費用を測定する。実測値なし。

## 10. Persistence / CAS / Migration proposals
Ticket hash、issuedAt/consumedAt/expiry/revokedAt、owner+session+goalRevision+leaseEpoch snapshot、human lease current mode、challenge evidenceRef、resume auth probe referenceを保存。生CookieやOTPをDB監査表へ記録しない。Ticket consume/CASとCheckpoint更新は原子的整合をCHAT_10へ提案。
原子操作: intent/lease reservation commit → 外部Action → result/ref + audit/outbox commit。DB physical schemaとmigrationはCHAT_10、Event/Evidence canonical shapeはCHAT_01。新規型はowner合意までlocal representationに留める。

## 11. REST/UI/他担当接続
WAITING_INPUTでLogin/2FA/CAPTCHA種別と操作可能な隔離画面を表示。人間の終了/取消/期限切れボタン、復帰後に再認証結果と許可済みタスク状態を表示。APIはGET session redacted/POST handoff/POST releaseのみ（CHAT_08のDraftと一致要照合）。

## 12. 実装PR順序
1. **04-HND-1** Challenge observation typed evidence + simulator
2. **04-HND-2** Ticket issuance/revoke/atomic consume + auth/lease CAS
3. **04-HND-3** Secure Browser takeover display/input + resume revalidation
4. **04-HND-4** Crash/replay/cross-tenant/side-effect adversarial E2E

## 13. Test design・REQ→TEST→SC
**すべてテスト設計、実行なし**。Unit/Contract/Integration/Fault/Security/E2E/BenchmarkのFixtureと期待値。

| TEST-ID | Setup → Action | Expected | REQ→SC |
|---|---|---|---|
| HND-T01 | Keyword-only false CAPTCHA | 単なるキーワードだけで確定しない | HND-001→SC-06 |
| HND-T02 | A requests B session | 他ユーザーticket発行0 | HND-002→SC-06/SC-08 |
| HND-T03 | reuse same consumed ticket | replay/他device利用拒否 | HND-003→SC-06/SC-08 |
| HND-T04 | Agent click during human lease | 同Tab二重操作0 | HND-004→SC-06/SC-07 |
| HND-T05 | Worker attempts action in WAITING_INPUT | Background click0 | HND-005→SC-06/SC-07 |
| HND-T06 | Ticket leaked in URL query attempt | 認証情報/endpoint露出0 | HND-006→SC-08 |
| HND-T07 | request host path from browser UI | ファイル/シェル特権なし | HND-007→SC-08 |
| HND-T08 | fixture 2FA/challenge human only | challengeの成功率保証なし | HND-008→SC-06/SC-08 |
| HND-T09 | release before login success | 未ログインならTask再開0 | HND-009→SC-06/SC-07 |
| HND-T10 | revoke and expiry | 失効後Human action0 | HND-010→SC-07/SC-08 |
| HND-T11 | Steer during ticket pending | 旧revisionでrelease不可 | HND-011→SC-07 |
| HND-T12 | kill Browser Worker during takeover | 重複ticketと副作用0 | HND-012→SC-07 |
| HND-T13 | A/B storageState restore | 別owner restore 0 | HND-013→SC-06/SC-08 |
| HND-T14 | purchase without approval during handoff | loginを支払い承認に転換しない | HND-014→SC-04/SC-08 |
| HND-T15 | redaction/recording disabled | PII artifact 0 | HND-015→SC-08 |
| HND-T16 | classifier false-positive benchmark | positive/false rates報告後採用 | HND-016→SC-09 |

## 14. Acceptance
上表で各MUSTに安定REQ-ID→TEST-ID→SC-IDを設定。Userのgoal条件に対する完了・原典検証、跨Tenant保護、cancel/steer/failure matrix、有限予算の試験に合格するまで`Locked`を主張しない。

## 15. 未決事項・Cross-team proposal
- **[PROVISIONAL: CHAT_09] P0 ticket and secret:** authenticated owner+audience+one-time ticket+key store。検証: HND-T02/03/06。
- **[PROVISIONAL: CHAT_01/02/10] P0 lease state/DB:** WAITING_INPUT transition, Run/Session epoch CAS, ticket transaction。検証: HND-T04/09/12。
- **[PROVISIONAL: CHAT_08] P0 takeover UI:** Browser Session GET/handoff/release とTicket consume URL固定。検証: HND-T02/03/10。
- **[PROVISIONAL: CHAT_03] P0 side-effect during human:** Human loginが外部購入/投稿のTool Approvalを代替しない。検証: HND-T14。
- **CHAT_11**: Mock auth takeover、悪意あるHTML、複数Worker、照合不能フィールド、SC-05〜16の関係部分のE2E fixtureを正式テスト正本へ追加。
未解決P0はIdentity/Tenant/DB Epoch/Fencing/Authentication Ticket。全件Draft。

## 16. Sources / evidence grade（2026-10-30 JST）
[S1] **Playwright** at `62d25ac221370f6fc5e26c600823792b83c67f1e` (2026-10-30, Code verified: source/test read, not executed; Apache-2.0): [`packages/playwright-core/src/server/browserContext.ts — storageState / setStorageState`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/packages/playwright-core/src/server/browserContext.ts) · [`tests/library/browsercontext-storage-state.spec.ts — restore/state isolation`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/tests/library/browsercontext-storage-state.spec.ts).
[S2] **Skyvern** at `fa2c00ff7ec9599b495cc52d24a1363613937895` (2026-10-30, Code verified: source/test read, not executed; 導入時にLICENSE確認（再利用しない）): [`skyvern/forge/sdk/copilot/challenge_evidence.py — typed challenge evidence`](https://github.com/Skyvern-AI/skyvern/blob/fa2c00ff7ec9599b495cc52d24a1363613937895/skyvern/forge/sdk/copilot/challenge_evidence.py) · [`skyvern/forge/sdk/copilot/tools/page_challenge.py — challenge flow`](https://github.com/Skyvern-AI/skyvern/blob/fa2c00ff7ec9599b495cc52d24a1363613937895/skyvern/forge/sdk/copilot/tools/page_challenge.py).
[S3] **Jev browser** at `817b09eea6e21aad836b5ed32a613c68e5644a08` (2026-10-30, Code verified: source/test read, not executed; 導入前LICENSE確認): [`src/password.ts — parseTrustedOrigin/readHandoffSecret/makeRedactor`](https://github.com/jkudish/jev-browser/blob/817b09eea6e21aad836b5ed32a613c68e5644a08/src/password.ts) · [`test/unit.test.mjs — password handoff one-shot/redaction tests`](https://github.com/jkudish/jev-browser/blob/817b09eea6e21aad836b5ed32a613c68e5644a08/test/unit.test.mjs) · [`src/navigate.ts — page injection and bot challenge inspection`](https://github.com/jkudish/jev-browser/blob/817b09eea6e21aad836b5ed32a613c68e5644a08/src/navigate.ts).
**Design hypothesis:** 本Harness内の構造、limit、状態遷移、権限検証順序、パーサー・Human Handoff Bridgeは未実装。将来OSS仕様が変わればpinned SHAと再比較する。
