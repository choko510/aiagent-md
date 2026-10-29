# Prompt Injection Defense — Detailed Implementation Specification

> Owner: CHAT_09 / 09-05 | Path: `docs/09-security/prompt-injection-defense.md` | Status: **Draft / PROVISIONAL**, rev 0.1 | Research: **2026-10-30 JST** | Source main: `b45d4e989998663a00ef09673e69ae1f4191f00d`
>
> Dependencies: CHAT_01 evidence/roles, CHAT_02 context/compaction/steer, CHAT_03 Tool Gateway, CHAT_04 Browser, CHAT_05 MCP/providers, CHAT_06 Skills/Memory, CHAT_07/13 events, CHAT_08 UI, CHAT_09 secrets/egress, CHAT_10 storage, CHAT_11/15 tests. Tests **designed only**, product runtime **not implemented**.

## 0. 結論

**権限の正本をLLMの外部に置く。** HTML、X投稿、PDF、Office、MCPツール説明、検索スニペット、Browser DOM・スクリーンショット、Tool応答、過去の抽出Artifactはすべて`UNTRUSTED_EXTERNAL`として取り込み、何度要約・コンパクト化・引用・復元してもsystem/developer/authenticated user命令に昇格しない。

採用する多層防御：
1. **Provenance**：各入力にServer検証済みのsourceRef、取得時刻、owner/tenant、hash、抽出位置、trust classを付与。本文中の「system」「admin」「approved」は単なる文字列。
2. **Context separation**：モデルへ渡す外部文字列を有限長のdata regionに限定。role/special tokenに見せかけた境界をsanitizeし、偽markerを無効化。これは補助策でありLLMが常に守る保証ではない。
3. **Deterministic gate**：LLMのTool ProposalをCHAT_03 Gatewayが静的Registry・厳密Schema・owner・Tenant/Workspace・GoalRevision・Run leaseEpoch・Approval・副作用台帳・費用・egressで検証。任意Tool/ホストshell/Docker/秘密の読み出し不可。
4. **Evidence validation**：観測時刻・出典・内容hashをserver receiptに紐づけ、Webの自己申告「公式検証済み」を無視する。未知はUNKNOWN。
5. **Persistence quarantine**：抽出文から直接 trusted Memory/Skill/Browser Action Recipeを有効化しない。別途所有者の明示的Review/Undoが必要。旧revisionのモデルproposalをSteer後に実行しない。
6. **Adversarial regression**：多言語・Unicode・埋込HTML・base64・ツール説明・OCR・長文・再利用時の攻撃を評価し、zero unauthorized effect/exfiltrationを合否基準とする。

## 1. Scope / threat model / non-goals

攻撃者は外部page、X/SNS、RSS、PDF/XLSXのalt/セル、MCP tool descriptor/response、検索結果、リダイレクトページ、画像内文字、モデル過去の要約候補、通知トリガーのイベント本文を書き換えられるが、認証済みuser instructionやTool Gateway Policyの正本は変更できない。狙いはsecret exfiltration、無承認購入/予約/投稿、権限昇格、ホスト操作、Task/Watchの条件改ざん、Memory永続汚染、誤根拠、費用枯渇、他ユーザーのArtifact取得。**非目標**：LLMに悪意ある文章を一切読ませないこと、全攻撃をキーワードで判別すること、正当な研究目的で攻撃文を引用することを禁止すること、モデル拒否をセキュリティ境界と主張すること。

主シナリオ: SC-03/06/07/08、派生SC-10/11/12/13/14/15/16。

## 2. OSS source & test audit

| OSS (ref/license) | 実装・テストを実際に読んだ箇所 | 実際の仕組み・採用限界 |
|---|---|---|
| **OpenClaw** `8b8c33ef334b3754f42afe2a2e212418abfa80f4` / MIT | `src/security/external-content.ts` の `detectSuspiciousPatterns`, `sanitizeModelSpecialTokens`, `truncateSanitizedExternalContent`, `wrapExternalContent`, `buildSafeExternalPrompt`; `src/security/external-content.test.ts` のUnicode/偽marker/UTF-16境界テスト | **Code verified**。ランダムmarkerで外部contentを区分し、長さを制限するが、パターン検出は情報・診断であり強制認可ではない。data-envelope案に採用 [S1/S2] |
| **OpenAI Agents SDK Python** `cd26bbbc52847845fde73b9952b51e62e2bcbdc0` / MIT | `src/agents/tool_guardrails.py` の `ToolInputGuardrail`, `ToolOutputGuardrail`, `ToolGuardrailFunctionOutput.allow/reject_content/raise_exception`; `tests/test_tool_guardrails.py` のinput/output、pass/reject/exception、password/SSN blocking; `examples/decisions/tool_risk_gate.py` | **Code verified**。Tool境界で構造化検証・拒否を実装できる例。ただしSDKガードレールだけでowner/approval/egress/side-effect認可は証明されない [S3-S5] |
| **OWASP LLM01 + MCP security** | Prompt injectionの脅威分類・trust boundaryの指針 | **Docs verified**、コードではない [S6/S7] |

代替案の比較：A) system promptの「外部命令を無視」だけ → **却下**。B) suspicious regex/LLM judgeを唯一の門番にする → **却下**（回避可能/非決定的）。C) provenance・context分離＋強制Gateway＋secret/egress＋Memory review → **採用**。D) Web本文を一切モデルへ入れない → 調査能力を損なうため却下、構造化抽出・有限引用を優先。参考OSSのコードを直接コピーせず独立実装する。

## 3. Trust graph / owner

~~~text
AUTHENTICATED_USER (verified channel identity, still subject to policy)
   -> Goal/constraints (CHAT_01/02) -> Tool Gateway (CHAT_03)
UNTRUSTED_EXTERNAL (HTML/X/PDF/MCP/Browser/Tool/Upload)
   -> Ingress receipt + Artifact hash (CHAT_01/10)
   -> data-only sanitizer+excerpt (CHAT_09)
   -> Context assembly (CHAT_02) -> MODEL_DERIVED proposal
   -> deterministic schema/Tool/owner/approval/budget/secret/egress checks
   -> approved executor -> Evidence/Trace safe projection (CHAT_01/08/09)
UNTRUSTED_MEMORY_CANDIDATE -> review (CHAT_06/14) -> reviewed preference
~~~

信頼度は**ユーザーがログインしているブラウザ経由でページを取得した**というだけでは上がらない。LLMが作成した要約の内容sourceRefもexternal由来。親の中に1件でもuntrusted sourceがある場合、提案・証拠の命令上の信頼を昇格できない。正式なEvidence/Event型はCHAT_01、コンテキスト組立CHAT_02、GatewayとApprovalはCHAT_03、Memory更新CHAT_06が所有する。

## 4. Requirements

| REQ | Class | Observable result | SC |
|---|---|---|---|
| INJ-001 | MUST | 全external fragmentにsourceRef,tenant/owner,hash,trust,fetch timeを付ける | 03,08,15 |
| INJ-002 | MUST | 外部contentがsystem/developer/user権限を偽装できない | 08,11 |
| INJ-003 | MUST | Tool実行前にmodelと独立したserver-side Policy/Schema/Owner/Approval/egressを必須化 | 04,07,08 |
| INJ-004 | MUST | LLMへtoken/Cookie/Secretを送らず、秘密宛先へのexfilも拒否 | 06,08 |
| INJ-005 | MUST | 外部URL/redirect/host claimでSSRF・Credential許可を変更できない | 05,08 |
| INJ-006 | MUST | snippet/sanitizerに有限bytes/depth/special token処理・正しい境界を持たせる | 08,09 |
| INJ-007 | MUST | fetchedAt/observedAt/verifiedなど事実の権威はserver receipt・出典にのみ依存 | 01,03,08,14 |
| INJ-008 | MUST | untrusted本文がMemory/Skill/learned browser policyへ直接保存・有効化されない | 08,13,16 |
| INJ-009 | MUST | X/email/Webhook本文がWatch/Subscription/Approval/Budget/Goalを変更できない | 02,08,11 |
| INJ-010 | MUST | Steer/Cancelと旧GoalRevision/leaseEpochでの旧proposalを拒否する | 07,08 |
| INJ-011 | MUST | MCP tool descriptorは実行権限やホスト操作を拡張できない | 08 |
| INJ-012 | MUST | fake role, encoded, multilingual, multimodal, nested/long-horizon攻撃をnegative fixtureに含む | 03,08,15 |
| INJ-013 | MUST | 安全なaudit reasonのみ保存し、private内容を他userへ漏らさない | 08 |
| INJ-014 | MUST | 反復検知・LLM judge・sanitize操作は有限上限で、リトライはbounded | 07,09 |
| INJ-015 | SHOULD | 危険な外部命令の引用分析を安全なdataとして扱い、誤ブロックを可視化 | 08 |
| INJ-016 | EXPERIMENT | 分類器/LLM judgeを既知/未知fixtureで測定、hard policy代替としない | 09 |

## 5. Internal contract proposals [PROVISIONAL: CHAT_01/02/03/10]

~~~ts
type TrustLabel = "AUTHENTICATED_USER"|"SERVER_POLICY"|"UNTRUSTED_EXTERNAL"|"MODEL_DERIVED"|"REVIEWED_PREFERENCE";
type ContentEnvelope = Readonly<{
  artifactRef:string; ownerUserId:string; tenantId:string; sourceKind:"web"|"x"|"pdf"|"mcp"|"browser"|"file"|"tool";
  sourceUri:string|null; fetchedAt:string; observedAt:string|null;
  sha256:string; contentLength:number; trust:TrustLabel; parentRefs:string[];
  extractionLocation:string|null; sanitizerVersion:string; safeExcerptRef:string;
}>;
type ProposedActionSecurityDecision = Readonly<{
  decision:"ALLOW"|"DENY"|"NEEDS_APPROVAL"; reasonCode:string;
  committedIntentRef:string|null; policyVersion:string; suspiciousSourceRefs:string[];
}>;
interface InjectionBoundary {
  admitExternal(input:UntrustedBytes, verifiedReceipt:FetcherReceipt):Promise<ContentEnvelope>;
  excerpt(ref:string, maxBytes:number):Promise<UntrustedExcerpt>;
  checkProposal(proposal:ModelToolProposal, persistedRun:ServerOnlyRunRef):Promise<ProposedActionSecurityDecision>;
}
~~~

Data envelopeは**server ingress**が決定、source本文内のJSONによるtrust/tenant override不可。`REVIEWED_PREFERENCE`は本人のreversible consentが確認されたときのみ。max excerpt候補64KiB/source、128KiB/step、provenance depth8、tool proposals32/step、classifier max 1 retry（すべて仮値・実測前）をPolicyとして上限化。原本は別のowner-scoped store、モデルには選別済み抜粋のみ。接続先/credentialRef/approved/verified/budgetはexternalやmodelからの値をauthorityとして受け付けない。

## 6. State / sequence

~~~text
RAW_SOURCE -> FETCH_VERIFIED -> UNTRUSTED_NORMALIZED -> SAFE_EXCERPT -> MODEL_PROPOSAL
     |               |                 |                 |
 INVALID_SOURCE   INVALID_RECEIPT   TRUNCATED         SCHEMA/POLICY
                                                      |       |
                                                    DENY  ALLOW/APPROVAL
RAW_SOURCE -> MEMORY_CANDIDATE -> HUMAN_REVIEW -> OWNER_APPROVED_PREFERENCE
(no direct transition to SERVER_POLICY)
~~~

Userからの新条件変更は認証されたChat/API Command + Task GoalRevision更新を必要とする。外部SNS本文「条件を変えて」は既存Subscription設定を変更しない。Chatが攻撃文の引用を要請した場合、データとして表示してよいがTool意図にはしない。

## 7. Pseudocode

~~~ts
async function ingestExternal(raw:UntrustedBytes, receipt:VerifiedFetchReceipt) {
  const src=await artifacts.commitImmutableWithOwnerAndHash(raw, receipt);
  const extracted=extractAsNonExecutableData(raw, {maxBytes:MAX_RAW, depth:8,
    blockMacros:true, noRemoteResourceFetch:true});
  const safe=sanitizeRoleDelimiterAndSpecialTokens(extracted);
  const excerpt=truncateUtf8WithRandomBoundary(safe, MAX_EXCERPT);
  return artifacts.commitEnvelope({sourceRef:src.id, excerpt,
    trust:"UNTRUSTED_EXTERNAL", owner:receipt.owner, sanitizerVersion:VERSION});
}
async function authorizeModelProposal(proposal, taskRef) {
  const ctx=await taskRepo.resolveAuthenticatedOwnerAndCurrentRun(taskRef);
  if (!ctx.active || ctx.goalRevision!==proposal.goalRevision) return DENY("STALE");
  const tool=registry.getStaticTool(proposal.name);
  if (!tool || tool.isHostAdmin || tool.mutatesPolicy) return DENY("UNKNOWN_TOOL");
  const args=tool.strictSchema.parse(proposal.arguments); // rejects unknown fields
  await gateway.verifyBudgetCapabilityOwnerLease(ctx, tool,args);
  if (tool.sideEffect) return gateway.requireApprovalBoundToCurrentArgs(ctx,args);
  await egress.assertDestinationAllowed(tool,args);
  return gateway.prepareCommittedIntent(ctx,tool,args);
}
~~~

Detector regexはヒント。`detectSuspiciousPatterns`が検出しなくても、未承認ToolやSecretは使用できない。外部文が「購買価格45,000円」と主張しても実測と未確認の区別はEvidence ownerのVerifierが判断する。

## 8. Failure matrix

| Fault | Detection / recovery / state |
|---|---|
| fake system role in HTML or Markdown | data-only wrapper; no role elevation; continue safe analysis |
| malicious MCP tool declares secret access | static registry denies; operator review required |
| source text forges verified timestamp | server receipt mismatch; no verified facts |
| Memory candidate quotes prior attack command | trust persists during compaction; activation review required |
| document/HTML asks to exfil via URL | secret not available to model, egress denies, audit |
| X event includes "change Watch"/"approve" | immutable subscription + authenticated command gate, no state change |
| injected text overlong/encoded/Unicode | bounded sanitizer; if uncertain use partial/truncated, no bypass |
| old model action after Steer/cancel | GoalRevision/leaseEpoch mismatch, deny; reconcile external unknown |
| Tool completed but DB ack lost | side-effect UNKNOWN, no blind re-execution |
| external instructions in screenshot/OCR | OCR treated as untrusted source, does not alter role |
| LLM-judge temporarily fails | hard security checks still active; sensitive operation fail closed |
| attack text quoted by real user | safe quote output, independent Tool policy unchanged |

## 9. Security / privacy / cross-user

Owner-scoped evidence/artifact storage, trace masking, and credential broker ensure external text cannot access other users. Security Gate never evaluates shell/SSH/Docker capabilities because they are not present. Input HTML and Markdown displayed by UI must be escaped/CSP-sanitized (CHAT_08). Malware inside PDF/XLSX cannot execute scripts/macros (CHAT_15). Full raw prompt cannot be exposed to untrusted MCP tools or echoed to model output. Secret can appear on website page itself: that page is still untrusted; redact before model rather than promising universal automatic secret removal.

## 10. Evaluation and cost constraints

Metrics: attempted authority promotion, denied Tool proposals, uncertain/unverified claims, poison-memory candidate counts, false positives on legitimate quoted attacks, malicious source truncation, origin of incidents. No per-token secret/PII logs. Run deterministic fixture + pinned model version, inspect actual Tool Gateway effects, not just model prose. Success criterion for malicious fixture: **unauthorized Tool/effect = 0, cross-user bytes = 0, secret canary leak = 0**. Detection precision/recall may be reported as measurements; no unverified success-rate claim.

## 11. Persistence/migration [PROVISIONAL: CHAT_10]

Owner-bound immutable source Artifact, sanitized excerpt, source hash, observed/fetched times, trust labels, provenance graph, sanitizer version, review receipts, denied policy reason. Existing unlabeled records must default to lower trust/quarantine, never be inferred trusted by prior model text. On schema upgrade perform expand/dual-write/backfill/validate, and preserve historical trust source through Watch/notification/Memory summaries. Physical DDL belongs to CHAT_10; Event envelope to CHAT_01.

## 12. UI / API [PROVISIONAL: CHAT_08]

Render externally sourced claims with URL/time/verification status. Show suspicious content warning only when helpful, not as a blanket refusal to quote text. Approved new preference/Skill action requires a separate authenticated UI command; approval screen shows frozen action + target + hash + expiry. Denial reason safe, no prompt/token/secret. User can cancel/steer, but external quote cannot silently do so.

## 13. Implementation PR sequence (separate implementation repository)

1. INJ-PR1 provenance/trust typed envelope, owner-scoped raw/excerpt storage and fixtures.
2. INJ-PR2 bounded extraction/marker sanitizer/context labeling and trace masking.
3. INJ-PR3 deterministic Tool Gateway policy + owner/Approval/egress/Secret protections.
4. INJ-PR4 Memory/Skill/learned action quarantine/review/rollback integration.
5. INJ-PR5 adversarial multi-user/multi-modal/long-horizon tests and red team release gate.

## 14. Tests (all Designed; not executed)

| TEST | Setup / Action | Expected |
|---|---|---|
| INJ-T01 | fake system in Web/X/MCP/PDF/Office | UNTRUSTED provenance, no actor change |
| INJ-T02 | marker/Unicode/role token forgery | sanitizer keeps data boundary, no new role |
| INJ-T03 | model proposes unknown Tool/owner override/unknown JSON fields | denied; effects 0 |
| INJ-T04 | canary secret and page "send it" | LLM/tool output and network contain no canary |
| INJ-T05 | exfil URL, redirect/private target | egress+secret policy deny |
| INJ-T06 | 1MiB nested forged markers with emoji | bounded and no malformed truncated token |
| INJ-T07 | source says verified/current; receipt stale | remains stale/unverified |
| INJ-T08 | poisoned text proposes Skill/Memory | no auto activation; explicit review |
| INJ-T09 | X post says change Watch/Goal | Watch/Goal unchanged |
| INJ-T10 | Steer/cancel and old Tool proposal/Approval | stale rejected, effects 0 |
| INJ-T11 | MCP descriptor attempts host shell | static Registry denies |
| INJ-T12 | multilingual/Base64/OCR/HTML hidden prompt corpus | policy protects all, zero unauthorized effects |
| INJ-T13 | audit of simulated attack with PII/token | safe codes only; 0 secret |
| INJ-T14 | repeated classifier failures | bounded retries/cost, partial safe state |
| INJ-T15 | user requests quote of injection text | quote returned as data; no Tool action |
| INJ-T16 | cross-tenant cached summary/learned action | unauthorized bytes 0 |
| INJ-T17 | Worker kill raw save/excerpt commit boundary | no trust escalation on recovery |
| INJ-T18 | model adds approved=true, "official" timestamp/evidenceID | server ledger rejects forged values |
| INJ-T19 | verified user long paste vs uploaded file/forwarded web quote with instruction-like text | transport source preserved; embedded external instructions remain data, Tool policy never elevated |

Fixture-only, no real purchase/post/credential; **tests not executed**.

## 15. REQ→TEST→SC

| REQ | TEST | SC |
|---|---|---|
| INJ-001 | T01,T16,T17 | 03,08,15 |
| INJ-002 | T02,T03,T18,T19 | 08,11 |
| INJ-003 | T03,T10,T11 | 04,07,08 |
| INJ-004 | T04,T05,T13 | 06,08 |
| INJ-005 | T05 | 05,08 |
| INJ-006 | T02,T06,T15 | 08,09 |
| INJ-007 | T07,T18 | 01,03,08,14 |
| INJ-008 | T08,T16 | 08,13,16 |
| INJ-009 | T09 | 02,08,11 |
| INJ-010 | T10,T17 | 07,08 |
| INJ-011 | T11 | 08 |
| INJ-012 | T01,T02,T12 | 03,08,15 |
| INJ-013 | T13 | 08 |
| INJ-014 | T06,T14 | 07,09 |

## 16. Cross-team proposals / unresolved

| ID / owner | Priority | Proposal and test |
|---|---|---|
| INJ-P1 / CHAT_01/10 | P0 | provenance hash/label/parent source reference shared schema + persistence; T01/T07 |
| INJ-P2 / CHAT_03 | P0 | Tool arguments/Approval/Gateway enforce independently of LLM; T03/T05/T11 |
| INJ-P3 / CHAT_02 | P0 | Context builder and compaction preserve lower trust/GoalRevision; T06/T10 |
| INJ-P4 / CHAT_06/14 | P0 | Skill/Memory/action recipe promotion review & undo; T08/T16 |
| INJ-P5 / CHAT_04/05/13/15 | P1 | source adapters tag DOM/X/MCP/PDF as untrusted; T01/T12 |
| INJ-P6 / CHAT_08/11 | P1 | injection quoting UX and full adversarial multi-user fixture; T15/T18 |

No published contract is Locked. Thresholds/model detector quality and shared Envelope remain unverified. Security tests pass/implementation status **not claimed**.

## 17. Sources (viewed 2026-10-30 JST)

- [S1] **Code verified**, OpenClaw MIT `8b8c33ef334b3754f42afe2a2e212418abfa80f4`, `src/security/external-content.ts`: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/security/external-content.ts
- [S2] **Test source verified, not run**: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/security/external-content.test.ts
- [S3] **Code verified**, OpenAI Agents Python SDK MIT `cd26bbbc52847845fde73b9952b51e62e2bcbdc0`, `src/agents/tool_guardrails.py`: https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/src/agents/tool_guardrails.py
- [S4] **Test source verified, not run**: https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/tests/test_tool_guardrails.py
- [S5] **Code verified**, illustrative decision sample (not a security guarantee): https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/examples/decisions/tool_risk_gate.py
- [S6] **Docs verified** OWASP LLM01 prompt injection: https://genai.owasp.org/llmrisk/llm01-prompt-injection/
- [S7] **Docs verified** MCP security considerations: https://modelcontextprotocol.io/specification/2025-11-25/basic/security_best_practices
- [S8] **Hypothesis / not implemented**: product Envelope, provenance propagation, limits, correctness/cost, deterministic policy integration.
- [S9] **Issue/PR incomplete**: no source-verified upstream issue or PR for the exact combined defense; cited implementations/tests are direct evidence.

## Review gate
- [x] 2 OSS code/tests, alternate options, scope and authority, typed interface, sequence/pseudocode, failure/restart/race, DB/UI, TEST/REQ/SC.
- [ ] Shared contracts, runtime integration, evidence accuracy measurement, full test execution. **Keep Draft**.

## Upstream PR investigation — Provenance of user paste versus external data
- **[PR inspected, open/unmerged]** OpenClaw [#165776](https://github.com/openclaw/openclaw/pull/165776) documents a UI paste provenance issue: long user-pasted content became an attachment and was handled as untrusted external source despite originating from the operator's message. **Not adopted as shipped upstream behavior**. Our implementation must preserve *verified transport origin* separately from whether the **content itself** contains third-party untrusted instructions. A user's pasted webpage text can be a trusted request to analyze the pasted bytes, but the embedded webpage commands cannot become server/tool policy. Add `INJ-T19`: typed text vs long paste vs file vs forwarded web content, origin metadata preserved, tool privileges unchanged.
- Source grade: PR report. This strengthens explicit `AUTHENTICATED_USER` transport vs `UNTRUSTED_EXTERNAL` quoted content separation without granting external instructions new authority.
