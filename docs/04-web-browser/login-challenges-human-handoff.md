# Login Challenges and Human Handoff — Detailed Implementation Specification

> CHAT_04 | `docs/04-web-browser/login-challenges-human-handoff.md` | Status: **Draft / PROVISIONAL** | Rev 0.1 | 2026-10-30 JST | Main `b45d4e989998663a00ef09673e69ae1f4191f00d` | **Harness tests executed: 0**

## 0. 採用設計・非目標・理由
ログイン資格情報・2FA/OTPはHuman Handoffを維持する。一方、CAPTCHA/TurnstileはCapSolverによる自動解決を第一候補とし、失敗・非対応・許可不明の場合はHuman Handoffまたは安全停止へフォールバックする。BrowserSession/Owner/Fence/Tabとユーザー承認の境界は維持し、取得したトークンやCookieをLLMへ渡さない。CapSolver成功応答だけではサイト通過・ログイン成功とみなさずBrowser Workerが事後確認する。

**採用:** Browser Worker所有のContextを保持し、Challenge Policy Gateで適格性を判定。許可済みCAPTCHAは秘密分離したCapSolver Adapterに委譲し、成功後は再観測。2FA/OTP・対象外・解決失敗はHuman-only modeへのDB CAS lease切替、one-time ticket with audience/user/tenant/task/session binding、expire/revokeを使う。
**却下:** サイト許可を確認せずCAPTCHAを外部サービスへ送信すること、自動成功保証、Agentによる2FA/OTP処理、CDP remote endpointの公開、HumanとAgent/solverの同時Tab操作、Cookie/SecretをLLM/RESTへ返すこと。
**非目標:** ログイン資格をモデルに保存、OTP/TOTP自動回答、無許可の防御回避やサイトアクセス、ユーザー承認なしの予約/購入、サーバー管理権限の貸与。Turnstileも利用許可とAdapter対応が確認できた場合に限り対象。

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
Agent / Tool Gateway → BrowserSession ACTIVE_AGENT → detect challenge → Challenge Policy Gate
Authorized CAPTCHA → SOLVING (exclusive Browser Worker fence) → provider result → fresh site verification → ACTIVE_AGENT or WAITING_INPUT
Unauthorized/unknown CAPTCHA or 2FA/OTP → WAITING_INPUT(DB checkpoint)
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
| HND-008 | MUST | 2FA/OTPは人間へ。CAPTCHA/Turnstileは許可済みのみCapSolver経路を選択、結果の通過保証なし | 無許可solve=0、2FA自動処理=0 | SC-06/SC-08 |
| HND-009 | MUST | release時に本人/epoch/auth state/Tab/Goalを再検証 | 未ログインならTask再開0 | SC-06/SC-07 |
| HND-010 | MUST | logout/revoke/cancel/expiry時にTicket/lease失効 | 失効後Human action0 | SC-07/SC-08 |
| HND-011 | MUST | Steer中の旧承認やTakeover ticketをinvalidate | 旧revisionでrelease不可 | SC-07 |
| HND-012 | MUST | lost ack/worker crash時はHuman/Agent双方の再接続をfence | 重複ticketと副作用0 | SC-07 |
| HND-013 | MUST | Cookie/storageStateは暗号化し本人/Session scopeを維持 | 別owner restore 0 | SC-06/SC-08 |
| HND-014 | MUST | Login完了後も元Taskの要求/費用/外部副作用承認を維持 | loginを支払い承認に転換しない | SC-04/SC-08 |
| HND-015 | SHOULD | Takeover中に見えた秘密の画面録画/スクショを既定無効 | PII artifact 0 | SC-08 |
| HND-016 | LATER | 自動challenge classifiersを実測で改善 | positive/false rates報告後採用 | SC-09 |
| HND-017 | MUST | CapSolver実行前に対象origin/tenant/task/purposeのサイト許可・ToS/robots・権限・対象種別を決定的に審査 | 許可不明・禁止サイトの外部API送信0 | SC-06/SC-08 |
| HND-018 | MUST | 認証済み設定で有効化したCapSolverを専用Adapterで実行し、APIキー・トークンはLLM/ログから遮断 | LLM/Trace/RESTでsecret露出0 | SC-08 |
| HND-019 | MUST | セッション/タブ/goalRevision/leaseEpoch/challenge identityを束縛し、同時solveと古い結果の注入を拒否 | stale result適用0 | SC-06/SC-07 |
| HND-020 | MUST | CapSolverのreadyは成功確定ではなく、対象サイトの再観測後のみresume | 未検証resume=0 | SC-06/SC-07 |
| HND-021 | MUST | API費用・時間・試行回数・同一origin反復・キャンセルを有限に制限 | budget超過の外部solve要求0 | SC-07/SC-09 |
| HND-022 | MUST | 不許可・非対応・失敗・不確定・2FAは既存Human Handoffまたは安全停止 | 無限自動再試行0 | SC-06/SC-07 |
| HND-023 | MUST | 外部へ送るChallengeパラメータをallowlistで最小化、cookie/session/PIIを原則渡さない | 無関係HTML/資格情報送信0 | SC-08 |

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
ACTIVE_AGENT → CHALLENGE_DETECTED → ELIGIBILITY_CHECK
ELIGIBILITY_CHECK (authorized captcha) → SOLVING → REVALIDATING → ACTIVE_AGENT
ELIGIBILITY_CHECK (2FA/unknown/denied) → WAITING_INPUT → HUMAN_ACTIVE → REVALIDATING → ACTIVE_AGENT
SOLVING (timeout/error/failed verification) → WAITING_INPUT or SAFE_STOP
WAITING_INPUT/HUMAN_ACTIVE/SOLVING → EXPIRED|CANCELLED|REVOKED
HUMAN_ACTIVE→WORKER_CRASH→REVALIDATING_or_WAITING_INPUT
EXPIRED/CANCELLED terminal for ticket, new ticket requires new approval
```
**以下はELIGIBILITY_CHECKでHuman Handoffに振り分けた場合のSequence。** 1. BrowserWorkerがchallengeを証拠とともに検出→2. Safe checkpoint/intent保存→3. Agent leaseを停止+epoch更新→4. APIで認証済owner確認→5. one-time ticket発行→6. WebUIがTicketを消費→7. BrowserWorkerにhuman-only secure display/input→8. Human release→9. owner/lease/goalRevision/Tab/Login stateを再検証→10. Agent mode lease再取得しResumeを通知。許可済みCapSolver経路は§18.2を参照。

## 7. アルゴリズム（疑似コード）
```typescript
async function requestHandoff(req,principal) {
  // 原子的境界: owner/Run/goalチェック + WAITING_INPUT checkpoint +
  // session mode/leaseEpoch CAS + hashed ticket作成を1 transactionに束ねる。
  return await db.transaction(async tx => {
    const s=await tx.sessions.lockByTenant(req.sessionId,principal.tenantId);
    assertOwnerAndTask(s,principal); assertValidRunGoalEpoch(s,req);
    await tx.checkpoint.waitingInput(req.taskId,req.sessionId);
    const next=await tx.sessions.casModeAndIncrementEpoch(s,"AGENT","HUMAN_PENDING");
    return tx.tickets.createHashedOneTime({user:principal.userId,
      tenant:principal.tenantId,task:req.taskId,session:req.sessionId,
      audience:"browser-takeover",epoch:next.leaseEpoch,ttlSeconds:300});
  });
}
async function beginHumanTakeover(oneTimeTicket,principal) {
  // チケットはHUMAN開始時に1度だけ消費。releaseには再使用しない。
  const claim=await ticketStore.consumeAndClaimHumanModeAtomically(oneTimeTicket,principal);
  await browserWorker.bindHumanInput(claim.session,claim.epoch);
  return issueShortLivedAuthenticatedTakeoverConnection(claim);
}
async function releaseHandoff(authenticatedConnection,principal) {
  const claim=await ticketStore.lookupConsumedGrantForOwner(authenticatedConnection,principal);
  await sessions.assertHumanLeaseAndCurrentGoal(claim);
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
| CapSolver permission missing / denied | site policy or operator configuration not eligible | do not call provider; authorized Human Handoff or safe stop | WAITING_INPUT / DENIED |
| provider error / timeout / browser still blocked | bounded attempt or failed page verification | never mark passed; human handoff or safe stop | WAITING_INPUT / UNKNOWN |
| provider task ready after Cancel / goal change | stale epoch/revision/tab | discard old result without application or auto-retry | CANCELLED / UNKNOWN |
| ticket replay | nonce already consumed | reject, audit | DENIED |
| different owner | Task/tenant mismatch | no takeover | DENIED |
| human disconnect | connection dead | lease grace→expire; no agent auto-resume | WAITING_INPUT |
| login cookie missing | auth probe fails | re-handoff | WAITING_INPUT |
| Run cancel while human active | revision/lease stale | revoke + isolate Browser | CANCELLED |
| Browser crash | tab state lost | invalidate ticket / new owner claim | RECOVERING |
| human manually attempts external purchase | direct human operation (not Agent Tool) | user-directed UI actions are distinct; cannot guarantee every website-side effect blocked; no implicit AI approval or automatic replay | HUMAN_DIRECT_ACTION |
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
`SOLVING`中はCapSolverの実行状態・上限費用・取消操作・失敗理由を秘密除去して表示し、ユーザー本人のHuman takeoverは排他終了後にのみ開始できる。WAITING_INPUTでLogin/2FA/CAPTCHA種別と操作可能な隔離画面を表示。人間の終了/取消/期限切れボタン、復帰後に再認証結果と許可済みタスク状態を表示。既存APIはGET session redacted/POST handoff/POST releaseのみ（CHAT_08のDraftと一致要照合）。Solve状態の表示用API追加はCHAT_08に提案し、本書で新規エンドポイントを確定しない。

## 12. 実装PR順序
1. **04-HND-1** Challenge observation typed evidence + simulator
2. **04-HND-2** Ticket issuance/revoke/atomic consume + auth/lease CAS
3. **04-HND-3** Secure Browser takeover display/input + resume revalidation
4. **04-HND-4** Crash/replay/cross-tenant/side-effect adversarial E2E
5. **04-HND-5 (proposed)** Challenge eligibility gate + CapSolver Adapter + budget/secret isolation
6. **04-HND-6 (proposed)** Exclusive solve lease + site revalidation + fault/replay tests; cross-owner contracts approved first

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
| HND-T08 | 2FA/OTPと許可済み/無許可CAPTCHAのfixture | 2FAはHuman限定、無許可solve=0、成功保証なし | HND-008→SC-06/SC-08 |
| HND-T09 | release before login success | 未ログインならTask再開0 | HND-009→SC-06/SC-07 |
| HND-T10 | revoke and expiry | 失効後Human action0 | HND-010→SC-07/SC-08 |
| HND-T11 | Steer during ticket pending | 旧revisionでrelease不可 | HND-011→SC-07 |
| HND-T12 | kill Browser Worker during takeover | 重複ticketと副作用0 | HND-012→SC-07 |
| HND-T13 | A/B storageState restore | 別owner restore 0 | HND-013→SC-06/SC-08 |
| HND-T14 | purchase without approval during handoff | loginを支払い承認に転換しない | HND-014→SC-04/SC-08 |
| HND-T15 | redaction/recording disabled | PII artifact 0 | HND-015→SC-08 |
| HND-T16 | classifier false-positive benchmark | positive/false rates報告後採用 | HND-016→SC-09 |
| HND-T17 | denied/unknown/allowed origin fixture | denied/unknown site CapSolver call=0 | HND-017→SC-06/SC-08 |
| HND-T18 | malicious DOM reads API key / trace dumps token | model-visible secret=0 | HND-018→SC-08 |
| HND-T19 | Cancel/Steer/lease change during delayed provider ready | stale token applied=0 | HND-019→SC-07 |
| HND-T20 | provider ready but website still challenges | resume=0, fallback | HND-020→SC-06 |
| HND-T21 | repeated CAPTCHA / rate-limit / charge cap | bounded cost/attempt, no storm | HND-021→SC-09 |
| HND-T22 | unsupported type, API error, timeout, offline | human or safe stop, no loop | HND-022→SC-06/SC-07 |
| HND-T23 | malicious external-content/cookie-injection attempt | allowlisted parameters only; secret disclosure=0 | HND-023→SC-08 |

## 14. Acceptance
上表で各MUSTに安定REQ-ID→TEST-ID→SC-IDを設定。Userのgoal条件に対する完了・原典検証、跨Tenant保護、cancel/steer/failure matrix、有限予算の試験に合格するまで`Locked`を主張しない。

## 15. 未決事項・Cross-team proposal
- **[PROVISIONAL: CHAT_09] P0 ticket and secret:** authenticated owner+audience+one-time ticket+key store。検証: HND-T02/03/06。
- **[PROVISIONAL: CHAT_01/02/10] P0 lease state/DB:** WAITING_INPUT transition, Run/Session epoch CAS, ticket transaction。検証: HND-T04/09/12。
- **[PROVISIONAL: CHAT_08] P0 takeover UI:** Browser Session GET/handoff/release とTicket consume URL固定。検証: HND-T02/03/10。
- **[PROVISIONAL: CHAT_03] P0 side-effect during human:** Human loginが外部購入/投稿のTool Approvalを代替しない。検証: HND-T14。
- **CHAT_11**: Mock auth takeover、悪意あるHTML、複数Worker、照合不能フィールド、SC-05〜16の関係部分のE2E fixtureを正式テスト正本へ追加。
- **[PROVISIONAL: CHAT_00/03/09] P0 CapSolver policy change:** ADR-009の許容範囲、サイト権限/第三者提供の同意、Provider費用/Secret正本を合意するまでは有効化しない。HND-T17/T18/T21/T23。
- **[PROVISIONAL: CHAT_01/02/08/10] P1 solving lifecycle:** 状態・checkpoint・試行台帳とUIの共有契約を定義する。HND-T19/T20/T22。
未解決P0はIdentity/Tenant/DB Epoch/Fencing/Authentication Ticket。全件Draft。

## 16. Sources / evidence grade（2026-10-30 JST）
[S1] **Playwright** at `62d25ac221370f6fc5e26c600823792b83c67f1e` (2026-10-30, Code verified: source/test read, not executed; Apache-2.0): [`packages/playwright-core/src/server/browserContext.ts — storageState / setStorageState`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/packages/playwright-core/src/server/browserContext.ts) · [`tests/library/browsercontext-storage-state.spec.ts — restore/state isolation`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/tests/library/browsercontext-storage-state.spec.ts).
[S2] **Skyvern** at `fa2c00ff7ec9599b495cc52d24a1363613937895` (2026-10-30, Code verified: source/test read, not executed; 導入時にLICENSE確認（再利用しない）): [`skyvern/forge/sdk/copilot/challenge_evidence.py — typed challenge evidence`](https://github.com/Skyvern-AI/skyvern/blob/fa2c00ff7ec9599b495cc52d24a1363613937895/skyvern/forge/sdk/copilot/challenge_evidence.py) · [`skyvern/forge/sdk/copilot/tools/page_challenge.py — challenge flow`](https://github.com/Skyvern-AI/skyvern/blob/fa2c00ff7ec9599b495cc52d24a1363613937895/skyvern/forge/sdk/copilot/tools/page_challenge.py).
[S3] **Jev browser** at `817b09eea6e21aad836b5ed32a613c68e5644a08` (2026-10-30, Code verified: source/test read, not executed; 導入前LICENSE確認): [`src/password.ts — parseTrustedOrigin/readHandoffSecret/makeRedactor`](https://github.com/jkudish/jev-browser/blob/817b09eea6e21aad836b5ed32a613c68e5644a08/src/password.ts) · [`test/unit.test.mjs — password handoff one-shot/redaction tests`](https://github.com/jkudish/jev-browser/blob/817b09eea6e21aad836b5ed32a613c68e5644a08/test/unit.test.mjs) · [`src/navigate.ts — page injection and bot challenge inspection`](https://github.com/jkudish/jev-browser/blob/817b09eea6e21aad836b5ed32a613c68e5644a08/src/navigate.ts).
**Design hypothesis:** 本Harness内の構造、limit、状態遷移、権限検証順序、パーサー・Human Handoff Bridgeは未実装。将来OSS仕様が変わればpinned SHAと再比較する。

### Round 2 ticket consumption / side-effect self-audit（2026-10-30）

初稿は`releaseHandoff(ticket)`に`consumeAtomically`を呼び、TicketがTakeover開始時に**既に消費されている**場合release不能になる矛盾があった。修正版は`requestHandoff`の短いDB transactionでWAITING_INPUT+HUMAN_PENDING+新epoch+ticket hashを一貫コミットし、**開始時にだけconsume**、終了時は認証済Takeover connectionに紐づく使用済grantを検証する。人間の操作がブラウザを通じて外部サイトへ送る行為はAgentの承認付きTool実行とは別だが、本Harnessが人間操作の全副作用を技術的に必ず阻止すると保証してはならない。**HND-T03/04/09/10/12/14**にはOne-shot reuse拒否、開始済connection release成功、古いepoch/Steer拒否、cancelとの競合、ユーザー自身の操作をAI Toolの承認に昇格させない負のテストを追加する。

## 18. User-approved policy proposal (2026-10-30) — CapSolver opt-in for authorized CAPTCHA

> **Status: PROPOSED / NOT IMPLEMENTED.** User requested that CAPTCHA encounters prefer CapSolver. This is a change from the prior human-only policy, not proof that every destination allows outsourcing verification. **Cross-owner ADR-009/CHAT_00 and CHAT_03/09 approval is required before marking Locked.**

### 18.1 Eligibility / priority

1. Detect a confirmed CAPTCHA/Turnstile with stable evidence; a generic 403/WAF block is **not** automatically a solvable CAPTCHA. Login credential, MFA, OTP, TOTP, device verification and access-denied remain Human Handoff or safe stop. CapSolver's own Terms prohibit certain activities including automatic ticket purchasing; do not enable the adapter for vendor-prohibited uses.
2. Browser Worker asks Tool Gateway/CHAT_09 site-policy authority: destination origin and purpose are explicitly authorized for automation **and** third-party CAPTCHA processing. Unknown, absent, prohibited or revoked permission fails closed. Robots/ToS and user authorization are evaluated separately; user direction alone does not override site constraints.
3. An authenticated operator configures `capsolver.enabled`, a site-specific allowlist, a dedicated secret reference and finite `maxSolveAttempts/maxSolveSpend/maxSolveSeconds`. Configuration alone creates no authority for an origin.
4. If permitted and supported, call the **deterministic** CapSolver adapter inside the existing Tool Gateway executor; never expose direct provider tools or API keys to the model. If not permitted/supported, use existing Human Handoff only where authorized, otherwise stop with a machine-readable reason.
5. CAPTCHA resolution grants no purchase/booking/posting/login privilege and cannot elevate Task policy or bypass rate limits.

### 18.2 Adapter and state contract (CHAT_04 proposal; Shared types belong to CHAT_01/03)

```typescript
type CaptchaDecision = "ELIGIBLE"|"NOT_AUTHORIZED"|"UNSUPPORTED"|"DISABLED"|"BUDGET_EXCEEDED"|"NEEDS_HUMAN";
type CaptchaAttempt = {
  schemaVersion: 1;
  attemptId: string; taskId: string; runId: string;
  sessionId: string; tabId: string; goalRevision: string; leaseEpoch: string;
  origin: string; challengeFingerprint: string; kind: "captcha";
  provider: "capsolver"; permissionRef: string; budgetReservationRef: string;
  state: "ELIGIBILITY_CHECK"|"SOLVING"|"PROVIDER_READY"|"REVALIDATING"|"PASSED"|"FALLBACK"|"DENIED"|"UNKNOWN"|"CANCELLED";
  providerTaskIdRef?: string; // encrypted / restricted, not model-visible
  evidenceRef?: string; verificationRef?: string; errorCategory?: string;
};
```

- Browser Worker alone owns the live Context/Page and the solver application. Start `SOLVING` after a checkpoint and a **single solver-exclusive fenced lease**, with agent/human page actions blocked. Double-solve of the same challenge fingerprint in the same session must coalesce or reject. Never keep a DB row lock open during provider polling.
- Provider request assembly is server-side from validated visible challenge metadata. Only mandatory, type-specific parameters (for example CAPTCHA type, approved page origin/site key) are allowlisted. No arbitrary DOM, cookies, auth tokens, history, storageState, credentials or screenshots are automatically sent to an external service. Any additional personal/session data requires separate policy and consent review.
- CapSolver's documented API uses `createTask` and, for async jobs, `getTaskResult`; task type and response shape vary. The adapter normalizes `processing`, `ready`, API errors, timeouts and UNKNOWN. Provider `ready` is **only a provider observation**, not proof that the destination accepted verification. Test locally against permitted fixtures, not an unapproved real target.
- With a live matching epoch/goal/origin/challenge fingerprint, Browser Worker may apply the provider result **only in the explicitly authorized flow**, then re-observe the original page and required authentication condition. If the challenge remains, classify failure; do not retry indefinitely or treat page transition as authorization for further actions.
- Fixed, configurable initial *proposals* (not benchmarks): 1 paid attempt per challenge fingerprint, maximum 2 per Task, 30-second provider deadline per attempt, per-Task spend cap set by operator, and per-origin circuit breaker on repeated challenges. Tool Gateway's stricter global/site budgets always win. The vendor documents task result retention as bounded (5 minutes) and polling as limited (120 queries per task); do not poll near those limits.
- Crash/lost ACK after `createTask`: persist the task reference and reconcile existing attempt before further charging. Never blindly issue a new paid task if create status is ambiguous. Cancel/Steer/logout/owner or epoch change: forbid new outgoing request/result injection and fence old callbacks; provider-side in-flight charges may still occur and are recorded as uncertain costs.
- Persist safe audit metadata only: attempt ID, redacted origin key, permission reference, provider task reference protected as secret, result category, spend/latency, challenge evidence/verification refs and why fallback occurred. No raw token/cookie/PII in evidence or telemetry.

### 18.3 Integration boundaries / acceptance

- **CHAT_00**: ADR-009 currently rejects CAPTCHA evasion. Update its approved scope to distinguish *explicitly permitted verification assistance* from unauthorized access-control circumvention. Do **not** silently change the ADR from CHAT_04.
- **CHAT_03/09**: provider billing/approval, allowlisted egress and secret management; capability and site-policy decisions must not be controlled by webpage text or an LLM.
- **CHAT_01/02/10**: typed lifecycle, BrowserSession lease fencing, durable attempt ledger and migration, crash recovery.
- **CHAT_08**: show `Solving with CapSolver` (not `verified`) and explicit fallback action. Redact API key/token, billing data and screenshot.
- **CHAT_04**: Browser Worker integrates eligibility and per-page exclusive solver; Jev/Scrapling adapters must not start independent, uncontrolled solving.
- **CHAT_11**: extend SC-06/07/08/09 fixtures with HND-T17…T23. All tests listed are **designs only**, no Harness/CapSolver live API verification was executed.

Vendor documentation reviewed 2026-10-30 (**Docs verified**, not integration tested):
- [CapSolver API: createTask](https://docs.capsolver.com/en/guide/api-createtask/)
- [CapSolver API: getTaskResult](https://docs.capsolver.com/en/guide/api-gettaskresult/)
- [CapSolver: supported task types](https://docs.capsolver.com/en/guide/api-support/)
- [CapSolver for AI agents](https://docs.capsolver.com/en/guide/ai/capsolver-for-ai-agents/)
- [CapSolver Terms and acceptable-use restrictions](https://www.capsolver.com/legal/terms)

**Definition of Done:** HND-T08 and HND-T17…T23 pass in approved fixtures; no provider call for unapproved origin; no cross-tenant token or API-key leak; no duplicate spend under replay; no stale result after revoke; provider-ready-but-page-blocked never resumes; cross-owner ADR/permissions contract is approved. Until then Draft / PROVISIONAL.
