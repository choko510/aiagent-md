# Feedback Learning — Detailed Implementation Specification (14-02)

> Owner: CHAT_14 | Path: docs/14-adaptive-experience/feedback-learning.md | Status: Draft / [PROVISIONAL] | Revision: 0.1 | Research: 2026-10-30 JST | main baseline: 3044c046b1caeea70c5ef6edfea7c57387c6c6da | Runtime code: NONE | Tests executed: 0

## 0. 結論と採用案

本人の明示的なフィードバックを、具体的な対象、根拠、用途、適用範囲、版、撤回可能性に結びつけた意思表明として処理する。学習とはモデル重みの無断更新ではなく、本人が確認した構造化嗜好をMemory Service正本へ安全に保存し、限定されたconsumerに適用することである。生のEvidence/価格観測/Task結果は変更しない。

例：「この店を除外」は店舗の同一性を検証し、このTask・Watch・Project・User全体のどこまで適用するか本人に示す。「利益が薄い」は利益閾値・対象・通貨を勝手に推測せず、明示された評価だけを記録する。「この価格は間違っている」は好みではなくFact Disputeであり元観測を上書きせず再検証へ回す。「この通知はいらない」はsoftな重要度signalであり、Watch停止やhard trigger抑制を意味しない。

採用は、typed intentの検証→本人確認→CHAT_06 Memory WriterのCAS・履歴・outbox→consumer再検証→Undo可能な投影、という方式。CHAT_06以外の第二のMemory正本は作らない。共通契約の未決事項は各Ownerへのproposalに留める。

## 1. Scope・非目標・受入

- SC-16: ユーザーは低利益候補を拒否し、ある小売店を将来候補から除外し、適用範囲を閲覧・変更・Undoできる。過去の観測Evidenceは保全される。通知優先度設定は明示Watchのhard通知を消さない。
- SC-01/02/04/05: 商品照合・利益・ホテルの日時/人数/予算等の現行Task明示条件を、一般的嗜好が上書きしてはならない。監視閾値はWatch Ownerの明示操作のみ変更可能。
- SC-07/08/09: CAS衝突、ACK喪失、Worker再起動、Cancel/Steer、非信頼HTML・Prompt Injection、越境閲覧・コスト上限に合格する。
- SC-10/13/14: Entity resolution・Learned Browser Actions・source reliabilityのデータに影響を与える場合は意味上の権限を区別。好みは事実やTool実行許可でない。
- 非責務: Canonical Memory/Skill schemaや物理DDL、Task Goal変更、Browser操作、Tool承認、Watch/通知配送、Event envelope、公開REST、実環境の機械学習、第三者提供データからの無断ユーザープロファイリング。

## 2. 関連OSSの実装・テスト・Issue調査（2026-10-30）

| OSS・固定版/Commit | 実際に確認したコード/テスト/Issue | 採否と限界 | License / grade |
|---|---|---|---|
| Hermes Agent fcc26fbd9f1641755e49793d7ed608f3b7214c3d | tools/memory_tool_store.py の MemoryStore.load_from_disk, _file_lock, apply_batch と tests/tools/test_memory_tool.py の曖昧置換・batch・overflow・Prompt Injection検証 | 対象曖昧時の拒否・保存前一括validationを参考。ファイル単位MemoryはPG/tenantを持つ本Harnessへ直接移植しない | MIT、Code verified、テスト読解のみ |
| Hermes Agent 67d47601bc5f8ed322451a672c843ed0e1a7c42c | agent/memory_provider.py の MemoryProvider.sync_turn, on_memory_write(add/replace/remove), prefetch | provider interface分離は参考。turn後syncを確定的な本人意図や整合性保証とはみなさない | MIT、Code verified |
| Letta Code 571867caa5e3718de8f7d4968c8d488078c6c51d | src/agent/memory-operation.ts claimMemoryOperation/withMemoryOperation、src/agent/memory-worktree.ts、src/agent/memory-git.ts、src/agent/memory-operation.test.ts の他プロセスlock・cancel、src/agent/subagents/memory-worker.test.ts のmerge conflict/false-success検出 | writer排他と競合検証は参考。Git CLI/Host shell/ファイル共有をAgentへ与える実装は却下し、PG CASで置換 | Apache-2.0（一部別ライセンス資産の注意）、Code verified、テスト読解のみ |
| Letta Code PR #4097、Issue #4029 | PRのdiff/本文・Issueの再現報告を実読。2026-10-30時点でPRはOPEN、未マージ。mainのreflection.mdでは依然としてnewest-wins文面あり | 弱い逸話による確定Factの無断上書き防止の反面教師。PR作者が示した評価値は我々の実測ではない | PR/Issue report only |
| LangGraph 6f2df053f4487ce6ea8decc0d7482c8e46e0f132 | libs/langgraph/tests/test_delta_channel_update_state.py のfresh/ordered updates, snapshot, history検証 | event versionとProjection整合のテスト観点のみ参考。LangGraph StateをMemory正本にしない | Code verified (test source)、テスト未実行、直接導入時はライセンス再確認 |

比較:
- A. 毎ターン会話から好み/事実を自動要約して最終Memoryへ上書き: **不採用**。出典/本人意図が混在しUndo不能。
- B. 最後の発言を優先してFact/Watch/Policyまで書き換え: **不採用**。未検証sourceと競合で安全性を損なう。
- C. 本人明示・typed rule・scope・CAS・Undo・Memory Store正本・consumer revalidation: **採用**。コストはrevision履歴と確認UI。
- D. オンラインRLHF/重み更新: **不採用**。ユーザー別隔離と透明なRollbackを実装しにくい。
- E. Local Git/ファイルをSelf-learning Skillが書き換える: **不採用**。Host Shell権限は絶対非目標。

## 3. 所有権、接続、Trust Boundary

| 正本Owner | 参照・変更を提案する契約 |
|---|---|
| CHAT_01 | Canonical ID/Event/Task/Run/Evidence/Artifactとsource identity、時刻、RecordVersion。Feedbackは元Evidenceを書き換えない |
| CHAT_02 | Task GoalRevision、Steer/Pause/Cancel、Run leaseEpochとVerifier。PreferenceはGoalを直接改定しない |
| CHAT_03/04 | Tool Policy/ApprovalとBrowserSession/Action。フィードバックから権限・ブラウザ操作へ直結させない |
| CHAT_06 | MemoryRecord/Profile/Skill candidate、MemoryWritePort、scope/revision/consent/delete/Undoの唯一の正本。PR #4のMEM-DATA-022を実装Portへ具体化 |
| CHAT_07 | Watchの定義/閾値/通知Outbox。FeedbackはWatch停止/編集コマンドではない |
| CHAT_08 | Web/Telegram本人認証・REST・UI・SSEへの投影 |
| CHAT_09 | Principal, consent, tenant/owner/workspace mapping、Privacy/Retention/Secret |
| CHAT_10 | PG DDL、FK/unique/CAS/outbox、migration/backup restore |
| CHAT_11 | Contract/E2E/fault/security/benchmarkテスト |
| CHAT_12 | Entity IDとmapping version、利益計算と未知値の扱い |
| CHAT_13 | 通知重要度のsoft rank/digest、hard watch条件との優先度 |
| CHAT_14-01 | Browser Recipeのdisable/invalidating、Permissionは変えない |

外部Web、添付、X投稿、LLM提案、検索結果、他人のチャット本文は**常に非信頼データ**。Web上の「店Aを除外して」という文章は本人意思の証明でない。Principalは受信テキスト内のuserIdではなくCHAT_09認証で導く。Skill文書やFeedback本文をSYSTEM命令やTool Policyへ昇格させない。

[PROVISIONAL: CHAT_01/02/09/10] 共有P0: UUIDv7公開ID vs gen_random_uuid(), nullable Task.conversationId vs SQL NOT NULL, recordVersion初期1 vs SQL revision初期0、leaseEpoch/goalRevision/activeRunIdの原子的保存、tenantId/ownerUserIdとworkspace_idの写像。本書は独自のShared型やDDLで矛盾を解消しない。

## 4. MUST/SHOULD/LATER要件

| REQ-ID | Level | 具体的な不変条件 |
|---|---|---|
| FBL-001 | MUST | 明示的本人操作だけをactive適用。引用/外部data/モデル推定からの誤学習0 |
| FBL-002 | MUST | Targetは認可済みEntity/Result/Watch/Notification/RecipeのID・version・出典で固定、曖昧同名/別容量を誤適用しない |
| FBL-003 | MUST | Scopeは本人が選ぶ。曖昧なら最小限の一回評価、Task→Userへの黙った昇格0 |
| FBL-004 | MUST | Preference / Fact Dispute / Rule Change Request / Skill Suggestion / Notification Feedbackを区別し、相互代用禁止 |
| FBL-005 | MUST | typed schema、通貨・minorUnits・日時・有効期限・Entity mapping versionのstrict validation。UNKNOWNを0や勝手な閾値にしない |
| FBL-006 | MUST | Memory Writerへscope・revision CAS・idempotency keyで渡し、Mutationと履歴/receipt/outboxをatomic commit |
| FBL-007 | MUST | Undo/訂正は補償イベントと新revisionを生成。競合、削除・Consent撤回後は古い値の自動復活0 |
| FBL-008 | MUST | Evidence/Observation/Taskの原記録はimmutable、feedback lineageと訂正申告だけを追加 |
| FBL-009 | MUST | Fact disputeは再検証待ち・contestedにし、弱い新情報で確認済みFactを直接置換しない |
| FBL-010 | MUST | Watch hard trigger/quiet hours/Task Goal/Tool Policy・Approvalは正本Ownerの明示コマンドなしに変更0 |
| FBL-011 | MUST | Consumerは実効scope/Consent/最新revisionを安全境界で再照合し、旧Projection・削除済み意図を使用0 |
| FBL-012 | MUST | 並行編集・ACK loss・Worker crash・Cancel/Steer・Outbox順不同・Restoreに耐え、旧値再活性化0 |
| FBL-013 | MUST | Cross-tenant/user leakage/秘密・PII出力/Prompt Injectionによるcapability昇格0 |
| FBL-014 | MUST | 文字・件数・頻度・LLM token/費用・deadline/履歴保持は有限上限で止める |
| FBL-015 | MUST | 本人が対象・scope・適用状態・理由・履歴を確認でき、Undo/削除の効果が表示される |
| FBL-016 | MUST | 信頼度・ユーザー好みをデータの真実性やPolicy権限として使わない |
| FBL-017 | SHOULD | 誤除外/正答/Undo反映遅延・モデル呼数を同じfixtureで評価 |
| FBL-018 | LATER | 暗黙推定の学習や共有プロファイルは別Consent/ADR承認まで無効 |

## 5. 内部API / TypeScript型 / Version / Validation

以下はCHAT_14のintake adapter専用の契約**提案**。公開REST/共通Entity/Canonical MemoryRecordの新定義ではない。CHAT_06 Ownerが承認したPortへ変換する。

~~~typescript
type Ref = string; // [PROVISIONAL: CHAT_01] server-generated canonical UUIDv7
type WireVersion = string; // ^(0|[1-9][0-9]*)$, <= 9223372036854775807, BigInt compare
type Scope =
  | { level:"task"; taskId:Ref }
  | { level:"watch"; watchId:Ref }
  | { level:"project"; projectRef:Ref }
  | { level:"user" };
type Target =
  | { kind:"research_result"; taskId:Ref; resultRevision:WireVersion; itemRef:string }
  | { kind:"entity"; entityRef:Ref; mappingVersion:string; class:"retailer"|"product"|"hotel"|"source" }
  | { kind:"watch"; watchId:Ref; definitionRevision:WireVersion }
  | { kind:"notification"; notificationId:Ref; decisionId:Ref }
  | { kind:"browser_recipe"; recipeId:Ref; revision:WireVersion };
type Preference =
  | { op:"exclude_entity"; entityRef:Ref; mappingVersion:string; class:"retailer"|"product"|"hotel"|"source" }
  | { op:"minimum_margin"; amountMinorUnits:WireVersion; currency:string;
      domain:"profit_estimate"; unknownHandling:"do_not_qualify" }
  | { op:"ranking_weight"; axis:"price"|"reliability"|"freshness"|"relevance"; value:"low"|"normal"|"high" }
  | { op:"notification_noise"; value:"less"|"normal"|"more" };
type FeedbackPayload =
  | { kind:"user_preference"; preference:Preference }
  | { kind:"fact_dispute"; evidenceRef:Ref; reason:"wrong_value"|"wrong_identity"|"stale"|"missing_condition";
      correctionEvidenceRef?:Ref }
  | { kind:"rule_change_request"; requestedOwner:"task"|"watch"|"policy"; summary:string }
  | { kind:"skill_suggestion"; taskId:Ref; purpose:string }
  | { kind:"notification_feedback"; signal:"useful"|"irrelevant"|"duplicate"|"too_frequent" };
interface FeedbackCommandV1 {
  schemaVersion:1; operationId:Ref; action:"submit"|"correct"|"retract"|"undo";
  target:Target; scope:Scope; payload?:FeedbackPayload;
  feedbackId?:Ref; expectedRevision?:WireVersion;
  undoOfOperationId?:Ref; sourceMessageRef?:Ref; expiresAt?:string;
}
type FeedbackResultV1 =
  | { status:"APPLIED"; id:Ref; revision:WireVersion; scope:Scope; receiptRef:string }
  | { status:"REVIEW_REQUIRED"|"AMBIGUOUS_TARGET"|"CONFLICT"|"DENIED"|
     "INVALID_SCOPE"|"STALE"|"FAILED_CLOSED"; reason:string; currentRevision?:WireVersion };
interface FeedbackPort {
  propose(cmd:FeedbackCommandV1, auth:ServerPrincipal):Promise<FeedbackResultV1>;
  apply(cmd:FeedbackCommandV1, auth:ServerPrincipal):Promise<FeedbackResultV1>;
  undo(cmd:FeedbackCommandV1, auth:ServerPrincipal):Promise<FeedbackResultV1>;
}
~~~

Validation:
1. schemaVersion=1、未知enum/余計なfieldはreject。UUID、positive non-overflow decimal wire、UTC date parseと実際のInstant comparisonを使い、JSON numberへ変換しない。
2. 認証済みprincipalからtenant/owner/scopeを導く。RequestのtenantId等を受け付けない。Target IDをversion込みで読み、存在・権限・mappingを検証し、他人のIDは非公開の統一エラー。
3. sourceMessageRefがある場合、CHAT_01 Message本体と本人/認証チャネルの一致を検証。引用内での発言やLLM生成文面は意図の根拠にならない。構造化UIの本人確定操作は署名済み内部receiptを要する。
4. Preferenceは決定的DSLのみ。rule_change_requestはオーナー申請にのみなり、自動処理には渡さない。skill_suggestionは候補保存だけ。fact_disputeはsourceのcontest/recheckへ分岐。
5. minimum_marginはISO 4217通貨コード+規定小数桁+nonnegative minor units。最大9,000,000,000,000 minor unitsを初期構成上限とする（市場/法律の値ではない）。利益計算はCHAT_12の決定的関数へ委譲し、FX/送料/額不明を0としない。
6. Entity ID/mappingVersion/classが一致しない場合はREVIEW_REQUIRED。似た小売名や色・容量違いの商品、同じ系列店舗へ自動拡張しない。
7. scope変更は旧scope retractと、新scope fresh consent付きsubmitの別操作。Task/Watch所属が曖昧なら永続化せず対象結果に対する単発評価だけに留める。
8. action correct/retract/undoにはexpectedRevisionとfeedbackId、undoにはundoOfOperationId必須。同じoperationId+同内容は既存receiptを返し、異なるpayloadなら409提案。異なる新版の編集を古いUndoで上書きしない。
9. 認証情報・原文の長い本文・Cookie・個人URL queryはpayloadに保存しない。説明欄が必要な場合は最大512文字、PII/redactionと保存Consentが別途必要。

**JSON入力例（架空ID）**:

~~~json
{
  "schemaVersion":1,
  "operationId":"018fa55e-2e00-7bd0-8a00-000000000031",
  "action":"submit",
  "target":{"kind":"entity","entityRef":"018fa55e-2e00-7bd0-8a00-000000000032",
    "mappingVersion":"m7","class":"retailer"},
  "scope":{"level":"task","taskId":"018fa55e-2e00-7bd0-8a00-000000000033"},
  "payload":{"kind":"user_preference","preference":{"op":"exclude_entity",
    "entityRef":"018fa55e-2e00-7bd0-8a00-000000000032",
    "mappingVersion":"m7","class":"retailer"}}
}
~~~

これらは本番IDや現存APIではない。SourceのconfidenceとUserのPreference strengthをPolicy/Tool実行権限に換算してはいけない。

## 6. 状態遷移とシーケンス

~~~text
USER_EXPLICIT_INPUT -> PARSED_PROPOSAL -> REVIEW_REQUIRED or VERIFIED_INTENT
VERIFIED_INTENT -> APPLYING -> ACTIVE or CONTESTED or RULE_PENDING or SKILL_CANDIDATE
ACTIVE -> CORRECTED (new immutable revision; old superseded)
ACTIVE -> RETRACTED (tombstone/revocation epoch)
ACTIVE -> EXPIRED / DELETED
CORRECTED/RETRACTED -> UNDO_REQUESTED -> NEW_REVISION_APPLIED or CONFLICT or DENIED
DELETED/EXPIRED -> never restore ACTIVE without fresh consent and source validation
~~~

~~~mermaid
sequenceDiagram
 participant U as User
 participant UI as UI/Telegram CHAT08
 participant F as Feedback Adapter CHAT14
 participant A as Auth CHAT09
 participant M as Memory Writer CHAT06
 participant DB as PG/Outbox CHAT10
 participant C as Consumer CHAT02/12/13
 U->>UI: explicit feedback on known target
 UI->>A: identity and permission
 UI->>F: typed command and user confirmation
 F->>A: target ownership and current scope/consent
 F->>M: conflict and current revision check
 F->>M: typed approved preference mutation
 M->>DB: CAS record + immutable history + idempotency receipt + outbox (one TX)
 DB-->>M: committed revision/receipt
 M-->>UI: outcome with effective scope
 UI-->>U: visible status / Undo
 DB-->>C: versioned invalidation event
 C->>M: refresh scoped active-only projection
 M-->>C: latest scope and policy-version checked preferences
~~~

ConsumerがOutboxを再配達/順不同で受けても、scope+recordRevision/high-water markが現在より古ければ破棄する。IndexやSSEの非同期更新だけでは許可を証明できず、重要なDecision/通知送信前にはDB正本とConsentを再確認する。DB transaction中に外部Tool/モデルを呼ばない。Mutex/Lockをブラウザ・ネットワーク待ち中に保持しない。

## 7. アルゴリズム、優先順位、Undo

最も強い制約は最新Task Goalの明示内容と正式Watch definition / Tool Policy。Memory Preferenceはそれらを変更できない。同じ対象/用途で複数scopeが重なる場合、compatible scopeに限りtask-specific > watch-specific（そのWatch内限定）> project > user として精度高いものを採用する。TaskとWatchを全順序の包含関係にはしない。同じscope+conflictKeyに複数の互いに矛盾する明示フィードバックがあれば最新revisionを検証し、単なるtimestamp/LLM confidenceによるlast-writer-winsは禁止。競合はユーザー確認へ。

~~~typescript
async function applyFeedback(cmd:FeedbackCommandV1, principal:ServerPrincipal) {
  strictValidate(cmd);
  assertAuthenticatedExplicitUserIntent(cmd,principal);
  const target=await targetOwner.readVersionedAndAuthorize(principal,cmd.target);
  if (!target) return fail("AMBIGUOUS_TARGET");
  const scope=await policy.resolveSameOrNarrowerScope(principal,cmd.scope,target);
  if (!scope) return fail("INVALID_SCOPE");
  const interpretation=classifyTypedIntent(cmd.payload);
  if (interpretation.unknown || interpretation.ambiguous)
    return fail("REVIEW_REQUIRED");
  if (interpretation.kind==="fact_dispute")
    return memoryOwner.recordDisputeWithoutOverwritingEvidence(cmd,principal);
  if (interpretation.kind==="rule_change_request")
    return ruleOwner.submitReviewRequestOnly(cmd,principal);
  if (interpretation.kind==="skill_suggestion")
    return skillOwner.stageCandidateOnly(cmd,principal);
  await policy.recheckConsentAndGoalIfTaskScoped(principal,cmd,target,scope);
  return memoryOwner.mutateWithCASIdempotencyHistoryOutbox({
    principal, scope, target, expectedRevision:cmd.expectedRevision,
    operationId:cmd.operationId, payloadHash:canonicalHash(cmd),
    validatedPreference:interpretation
  });
}
async function undoFeedback(cmd:FeedbackCommandV1, principal:ServerPrincipal) {
  assert(cmd.action==="undo" && cmd.undoOfOperationId
    && cmd.feedbackId && cmd.expectedRevision);
  return memoryOwner.compensateWithCAS({
    principal, id:cmd.feedbackId, priorOperation:cmd.undoOfOperationId,
    expectedRevision:cmd.expectedRevision, checkOtherEditors:true,
    freshConsent:true, preserveAudit:true, invalidateConsumers:true
  });
}
~~~

UNDOのケース:
- ADDのUndoは現在有効なレコードをtombstone化。削除したユーザー嗜好を別タスクの古いWorkerから復活させない。
- CORRECTのUndoは、そのcorrectが現在のheadで、後続の別編集が存在せず、旧値を再有効化する権限/ConsentとSourceがある時のみ、新しいrevisionとして元の値を作る。二人目の編集があればCONFLICT、旧値上書き禁止。
- RETRACTのUndoは、削除・Retention期限・Consent失効なら復活を拒否。許される場合も本人fresh consent + 新しいrevisionを必要とする。
- Undoは将来の推薦/優先度を変える。すでに行われた通知、外部Toolの副作用、元Evidenceの記録は取り消せない。

Consumerへの具体的影響:
- CHAT_12推薦: matchが確定した店舗IDのみ除外。結果原本は残し、ユーザー設定による除外として説明。mapping更新や不明値のときは要確認。価格の観測を改変しない。
- CHAT_02 Task: Task開始/次の安全境界で読み、現在Goal明示制約を優先。以前のProfileを既存Runの旧Goalに無断注入しない。
- CHAT_07 Watch: Feedbackを根拠にstop/threshold/dueを書き換えない。Watch操作は正式Ownerコマンドが必要。
- CHAT_13 Notification: soft importance hintのみ。明示watch hard trigger、criticalやquiet hoursを勝手にDROPしない。
- CHAT_06 Skill: 手順の候補としてのみ提出し、自動enableやTool Capability拡張禁止。
- CHAT_14-01 Recipe: 個別recipeの無効化は別OwnerのCAS操作。Feedback本文をBrowser命令として再生しない。

## 8. 障害マトリクス

| ケース | 判定・復旧 | 最終不変条件 |
|---|---|---|
| 非信頼HTMLが本人の好みを宣言 | 出典とPrincipal不一致で拒否 | active記録0 |
| 店名同じ・別系列/別容量 | entityRef+mappingVersion mismatch | 誤除外0、本人確認 |
| 閾値無しの「利益薄い」 | 数値/通貨欠損 | UNKNOWNのまま候補評価のみ |
| Task explicit goal vs profile好み | scope+goalRevision照合 | Goal保持、衝突表示 |
| DB commit直後ACK喪失 | operation receiptを照会 | 二重書込0 |
| 同時複数端末correct | CAS conflict | silent overwrite0 |
| Undo直前に他者がcorrect | head revision mismatch | CONFLICT、古い値の復活0 |
| 本人削除と非同期Job競合 | revocationEpoch/Consentを再照合 | 旧jobの再活性0 |
| Backup restoreが削除前snapshot | 高水位tombstone照合 | fail-closed、古いACTIVE0 |
| 元EvidenceがPrivacy削除済 | source visibility invalid | 投影停止/レビュー、勝手な再保存0 |
| Outbox順不同/retry | scope+revision確認 | 最新だけ有効 |
| Cancel/Steerとtask scope feedback | current Goal/fence照合 | 旧Taskに新規適用0 |
| Token/DB/Auth provider timeout | bounded fail closed | 無制限再推論・自動許可0 |
| hard Watch通知が有効 | Owner hard ruleを優先 | soft feedbackで黙ってDROP0 |
| Raw textにsecret/PIIが入る | validator/redactor/consent | Model/Logへの秘匿値0 |

## 9. Security・Privacy・Trust boundary

- OAuth token、Cookie、Secret、User contact等をモデルに出さない。SourceMessageRefのURIを自動Fetchしない。外部URLの取得は常にTool Gateway/CHAT_09 SSRF・egress制限経由。
- 同じUser本人でも別workspace/ownerに所有された対象の存在をAPI count・error・timingで漏らさない。TenantはUUID文字列だけで認可できない。
- Web/Telegramでのフィードバックはそれぞれセッションを本人Principalへbindする。Telegram chatIdだけをUserIdとみなさない。
- Personal attributesの自動推測、機微属性の無断生成、組織横断的な嗜好共有は禁止。将来の改善案でも本人明示opt-inと別ADRを要求。
- Memoryのstatusはactiveだけを通常の投影対象とする。candidate/contested/expired/tombstonedは確定好みとして扱わない。
- Delete/consent revokeはread-timeに効き、index/outbox/SSEの遅延更新を許可境界に利用しない。バックアップの古いMemoryを高水位Revocationから復活させない。
- Tool side-effectの実行権限はCHAT_03にあり、PreferenceやLLMのconfidenceでは獲得できない。Server host Shell/SSH/Docker socket/privilegedコンテナ禁止。

## 10. Budget / Observability / 性能

安全な初期値**案**（実測値でない）: command JSON最大4KiB、説明文最大512文字、同一User active<=500 records、pending<=100、1 batch<=10操作、1User<=10 writes/minかつ<=50 writes/hour、request deadline<=5秒、LLM parse最大1 call/操作・<=5秒・Run予算以下、typed UIでは0 call。Projection最大200件/Task、トークン数はCHAT_02 Context Budgetとmin。高負荷・limit超過はエラーで止めて古い設定に勝手に戻さない。

Scopeごとの有效期限: Task scopeはTask lifecycleに束縛。Watch scopeはWatch停止/失効と整合。Project/Userはユーザー明示expiresAtか本人撤回まで機能設定として生きても、原文・PII・auditのRetentionはCHAT_09方針で有限にする。Undo UI history30日という初期案はPrivacy deletion優先、実データ保存期限の確定ではない。Idempotency retentionはCHAT_10/09の正本に従う。

Metrics: feedback_intake_total{kind,status}, feedback_ambiguous_target_total, feedback_scope_confirmation_total, feedback_undo_total{status}, feedback_revision_conflict_total, feedback_source_revoked_total, feedback_projection_age_ms, feedback_effective_count, feedback_llm_calls, feedback_cost_usd。LabelにUser/店舗名/URL query/原文を記録しない。Traceにはopaque operation/target/refとpolicy version、source/decision、CAS outcomeを含む。

Benchmarkは同一の20 fixture（直截の除外、曖昧な利益、Fact矛盾、同名店舗、Scope変更、通知/Watch衝突、Undo/競合）でfalse exclusion、false preference learning、p95 Undo反映、memory projection tokens/cost、cross-user leak件数を評価。実測0で速度・正確さを断定しない。

## 11. 永続化・Transaction・Migration proposal [PROVISIONAL: CHAT_06/10]

Canonical MemoryRecordはCHAT_06。追加的な**論理要請**は次のとおりであり、本書はDDL正本ではない。

- feedback_commands: (tenant, actor, operationId)一意、payloadHash、sourceMessageRef、targetRef+version、requestedScope、statusとreceiptRef。
- feedback_event_history: feedbackId、source intent、actor、old/new revision、operation、timestamp、consent/policyVersion、immutable references、既存Evidence ref。Privacy削除が優先。
- feedback_effective_projection: (tenant, owner, scope, conflictKey) -> current active MemoryRecord Ref+revision、revocation epoch。派生物でありDB正本と整合を確認する。
- feedback_invalidation_outbox: (consumer, scope, version, epoch)重複/順不同安全。Memory mutation/命令Receipt/履歴/outboxは一PG transactionでコミットし、ACKが失われた場合はreceiptを読み直す。Memory index更新遅延時もrevocation watermarkで旧Preferenceをブロック。
- Option AはMemoryRecordのprofile/user preference拡張、Option Bは専用Feedback aggregateでMemoryReferenceを紐付ける。正本二重化は禁止。決定権はCHAT_06/10、MigrationとFK/RLSはOwnerが作成。

Migration: M0 flag OFF+契約/Schema追加、M1 User明示操作のcandidate/dry run、M2 scoped active/Undo・CASが合格してからopt-in、M3 CHAT_12/13 consumerのsoft適用、M4 fault/restore/privacy合格後に広域有効化。Rollbackはfeature flag OFF+projection invalidationのみ、過去の承認/Preferenceを勝手に再有効化しない。未知schemaを旧Workerが読んだらfail closed。

## 12. UI / REST / Telegram / Event proposal [PROVISIONAL: CHAT_08/01]

- POST /feedback/proposals: 自然文からtyped candidateを提示するだけ、保存なし。
- POST /feedback: 本人承認済みの確定Create、Idempotency-Key必須。PATCH /feedback/{id}: expectedRevision/If-Match。POST /feedback/{id}/undo: undoOfOperationId + If-Match、GET /feedback, GET /feedback/{id}/historyは本人専用scope-filter。DELETE/retractは個別イベント記録と即時投影無効化。
- UIは「このTaskだけ」「このWatchだけ」「このProjectだけ」「自分の今後すべて」を明示選択し、Entityの正確な識別子・影響範囲・現在revision・expiresAt・出典・Undo導線を提示。曖昧な「利益薄い」はそのまま将来の数値閾値にしない。
- Recommendation一覧ではSource evidenceを削除せず、excludedByUserPreferenceの説明と個別Undoを表示。通知の「少なくする」と「Watch停止」は異なる操作として明示。
- Event名は FEEDBACK_PROPOSED/APPLIED/CONFLICT/UNDONE/REVOKED/EXPIRED **提案のみ**。Envelope/イベントIDの正本はCHAT_01。Task/Owner-SSE scope filterはCHAT_08/09。
- Telegramでは正規の本人リンク＋確認UIを通さず永久scopeの設定を作らない。

## 13. 実装PR分割（このrepoで実装しない）

1. FBL-I1: typed intent DSL/strict parser/本人証拠・target scope validation、誤入力fixtures。
2. FBL-I2: CH06 Memory Adapter + CH10 atomic CAS/history/idempotency/outbox、Migration。
3. FBL-I3: Scope precedence/consumer projection、read-time revocation guard。
4. FBL-I4: Undo compensating event、concurrent edits、Fact dispute validation/recheck。
5. FBL-I5: CH08 Web+Telegram review/API、CH12 Entity、CH13 Ranking integration。
6. FBL-I6: CH11 fault/security/privacy/replay/backup benchmarkとrollout gates。
依存するP0共通契約解決前にLocked不可。

## 14. Unit / Contract / Integration / E2E / Fault / Security / Benchmark tests

**以下はすべて設計のみ、実行0件。** Mock/fixtureを使い実サービスへの通知・購入・投稿は行わない。

| TEST-ID | Setup → Action | Expected | REQ | SC |
|---|---|---|---|---|
| FBL-T01 | HTML/他人引用/LLM推測と本人クリックを同文で送信 | authenticated explicitのみACTIVE | FBL-001 | SC-08/16 |
| FBL-T02 | 同名小売・異なる容量/Mapping version変更でexclude | exact entityだけ一致、誤除外0 | FBL-002 | SC-01/10/16 |
| FBL-T03 | あるTaskでscopeを選び別Task/Project/Userへ問合せ | 選択以外に反映0 | FBL-003 | SC-05/16 |
| FBL-T04 | Preference/Fact dispute/Watch停止/Skill候補を入力 | 型別Ownerへ分類、Tool/Watch自動変更0 | FBL-004 | SC-02/08/16 |
| FBL-T05 | 「利益薄い」だけ、JPY/USD混同、負値、未知FX | REVIEW_REQUIRED、数値捏造0 | FBL-005 | SC-01/16 |
| FBL-T06 | 同operation 100並行、ACK lost、payload違い再送 | 1 Receipt/1 mutation、payload違いCONFLICT | FBL-006 | SC-07/16 |
| FBL-T07 | ADD/CORRECT/RETRACT後Undo、他editor同時更新、Privacy delete | 安全時新revision、競合/削除時旧値復活0 | FBL-007 | SC-07/16 |
| FBL-T08 | 過去の価格Evidence保持後、店除外→Undo | Evidence hash/time/数値不変、Projectionのみ変化 | FBL-008 | SC-01/16 |
| FBL-T09 | confirmed factと弱い逸話でFactCorrection | contested+recheckのみ、旧Fact変更0 | FBL-009 | SC-05/16 |
| FBL-T10 | Watch hard triggerと「通知少なく」、Goal固定とprofile preference競合 | hard/Goal/Policy不変、Owner明示操作のみ変更 | FBL-010 | SC-02/04/16 |
| FBL-T11 | Consumer rev4 cache→Undo rev5→outbox rev4遅配 | rev5有効、古い設定投影0 | FBL-011 | SC-07/13/16 |
| FBL-T12 | DB commit前後Worker kill、Steer、restore古snapshot | 二重適用0、旧値復活0 | FBL-012 | SC-07/16 |
| FBL-T13 | 2 user×2 tenant、ID推測、敵対HTML、Secret混入 | 他user leak/権限昇格/Secret露出0 | FBL-013 | SC-06/08/16 |
| FBL-T14 | >4KiB入力、501 active、51/hour、model parse二重要求 | LIMIT、追加呼出0 | FBL-014 | SC-09/16 |
| FBL-T15 | Web/Telegram scope preview→Apply→History→Undo | 対象/版/状態を本人確認、実効状態更新 | FBL-015 | SC-16 |
| FBL-T16 | Source confidence高値かつWeb指示「除外」を与える | Fact/Preference/Policy混同0、UNKNOWN保持 | FBL-016 | SC-08/14/16 |
| FBL-T17 | 同一20 fixtureの調整前後を比較 | False exclusion/p95/$報告、根拠なき優位主張なし | FBL-017 | SC-09/16 |
| FBL-T18 | 自動推定/共有Profile要求でfeature flag OFF | 本人Consentなしactive0 | FBL-018 | SC-08/16 |

## 15. Acceptance Criteria / Traceability

上表のFBL-001〜016（全MUST）からFBL-T01〜16への1:1追跡を受入最低条件とする。FBL-017/018はShould/Laterの評価のみ。SC-16で本人の明示除外が対象scopeへ正しく反映/Undoできること、SC-01で元価格Evidence不変、SC-02でWatch hard trigger不変、SC-07でACK loss/競合/旧Workerから再適用0、SC-08で越境/Prompt Injection0を合格基準とする。テストを実施していないためImplemented/Verified/Lockedと表記しない。

## 16. Cross-team proposals / 未決と解決Owner

| ID / Severity | Owner / Target | 提案するBefore→After、Migration、Contract test |
|---|---|---|
| FBL-P0-01 | CHAT_06 memory-data-model.md, memory-retrieval-and-update.md | 現在MEM-DATA-022は概念要求。→ scoped typed feedback, conflictKey, sourceRefs, CAS/Undo, active/contested/tombstoneとrevocationEpochのCanonical Port決定。Memoryと二重書込み禁止。FBL-T03/06/07/08/11 |
| FBL-P0-02 | CHAT_01/02/10 Core/Run/DDL | UUIDv7 vs v4 DB default, nullable Task conversation, recordVersion 1 vs revision0、goalRevision/leaseEpoch/activeRunId mappingを正本とSQL migration/DB contract testで一致。FBL-T06/12 |
| FBL-P0-03 | CHAT_09/10 principal/privacy | tenant+owner+workspaceのAuth mapping、Feedback/Entity/Memoryの複合FK認可、delete/restored backup high-water。FBL-T01/07/12/13 |
| FBL-P0-04 | CHAT_07/13 Watch/Notification | soft preferenceとhard configured triggerのprecedenceを正式化。Outbox idempotency、quiet hours、explicit Watch stopのみhard変更。FBL-T10/11 |
| FBL-P1-05 | CHAT_08 Web/REST/Telegram | scope選択UI+If-Match/idempotency+target review+Undo history; unknown targetは保留。FBL-T02/03/05/15 |
| FBL-P1-06 | CHAT_12 Entity resolution/risk | entity mappingVersion固定、別容量/系列店false merge、JPY/minorUnits/unknown profit。FBL-T02/05/16 |
| FBL-P1-07 | CHAT_06 Skill lifecycle | feedbackの手順は候補のみ、SkillがTool能力を増やさない。FBL-T04/16 |
| FBL-P1-08 | CHAT_03/04/14-01 | Browser Recipe disableを別Ownerへ提案、FeedbackからGateway/Policyへ直結しない。FBL-T04/13 |
| FBL-P1-09 | CHAT_11 Acceptance Harness | cross-user/2 tenant、複雑なUndo、outbox順不同、Privacy/backup restore、false exclusion measurement。FBL-T01..T18 |
| FBL-P1-10 | CHAT_00/09 Privacy/ADR | 個人嗜好保存、原文/履歴/バックアップ保持、暗黙推定の許可について人間ADR判断。FBL-T13/14/18 |

Ownerが承認するまで本提案をCanonical型やDB migrationとして固定しない。他担当のDraft/PRに無断Pushしない。必要な差分・受入テストは各OwnerのPRへ提案し、本書をRevisionで追従させる。

## 17. Sources（2026-10-30 JST、Evidence grades）

- [S1] Hermes Agent @ fcc26fbd9f1641755e49793d7ed608f3b7214c3d: [MemoryStore](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/tools/memory_tool_store.py), [memory tests](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/tests/tools/test_memory_tool.py), [MIT License](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/LICENSE). Code verified/test source read, none run.
- [S2] Hermes Agent @ 67d47601bc5f8ed322451a672c843ed0e1a7c42c: [MemoryProvider](https://github.com/NousResearch/hermes-agent/blob/67d47601bc5f8ed322451a672c843ed0e1a7c42c/agent/memory_provider.py). Code verified.
- [S3] Letta Code @ 571867caa5e3718de8f7d4968c8d488078c6c51d: [MemoryOperation](https://github.com/letta-ai/letta-code/blob/571867caa5e3718de8f7d4968c8d488078c6c51d/src/agent/memory-operation.ts), [Operation tests](https://github.com/letta-ai/letta-code/blob/571867caa5e3718de8f7d4968c8d488078c6c51d/src/agent/memory-operation.test.ts), [Memory Worker tests](https://github.com/letta-ai/letta-code/blob/571867caa5e3718de8f7d4968c8d488078c6c51d/src/agent/subagents/memory-worker.test.ts), [Worktree](https://github.com/letta-ai/letta-code/blob/571867caa5e3718de8f7d4968c8d488078c6c51d/src/agent/memory-worktree.ts), [License](https://github.com/letta-ai/letta-code/blob/571867caa5e3718de8f7d4968c8d488078c6c51d/LICENSE). Code verified, Apache-2.0 plus asset caveat; tests not run.
- [S4] Letta Code [PR #4097](https://github.com/letta-ai/letta-code/pull/4097) (OPEN/unmerged as inspected), [Issue #4029](https://github.com/letta-ai/letta-code/issues/4029) and [reflection source](https://github.com/letta-ai/letta-code/blob/571867caa5e3718de8f7d4968c8d488078c6c51d/src/agent/subagents/builtin/reflection.md). PR/Issue report only; external tests not reproduced.
- [S5] LangGraph @ 6f2df053f4487ce6ea8decc0d7482c8e46e0f132: [delta-channel update_state tests](https://github.com/langchain-ai/langgraph/blob/6f2df053f4487ce6ea8decc0d7482c8e46e0f132/libs/langgraph/tests/test_delta_channel_update_state.py). Test source verified/not run, license reverify before adoption.
- [S6] Local Draft contracts inspected on their actual HEADs: [CHAT_06 Memory #4](https://github.com/choko510/aiagent-md/pull/4), [Memory/Skill #15](https://github.com/choko510/aiagent-md/pull/15), [Self-improving Skill #30](https://github.com/choko510/aiagent-md/pull/30), [CHAT_03 approval #20](https://github.com/choko510/aiagent-md/pull/20), [CHAT_01 Core #2](https://github.com/choko510/aiagent-md/pull/2), [CHAT_10 DB #10](https://github.com/choko510/aiagent-md/pull/10), [CHAT_14 Learned Browser #52](https://github.com/choko510/aiagent-md/pull/52). None Locked.
- [S7] 本文の内部型・数値上限・状態機械・Undo/DB/API/Ranking契約は**Design proposal/Hypothesis**であり、OSSが提供する動作や実測保証ではない。
