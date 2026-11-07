# DEV-04 — Expert Advisor / Escalation Router
> Draft / PROVISIONAL | 2026-11-07 | Owners CHAT_02/03/08/09/10。Model capability/料金は実装時に検証。

## 1. 目的
開発担当A5が難題に詰まった場合、あらかじめ許可された高性能モデルを**相談役（read-only）**として呼び出し、原因分析・別戦略・最小修正手順を得る。通常のBuilderを無条件に高価なモデルへ切り替えない。相談は追加の子Task（独立予算予約/Tool scope）であり、管理上の権限を持たない。

## 2. 発火条件と抑制
既定候補: (1) 同じfingerprintの失敗が連続、(2) 同一修正の反転/回帰、(3) P0/P1原因の所在不明、(4) Reviewersの証拠付き対立、(5) アーキテクチャの強いトレードオフ、(6) 対応可能モデルに明白な能力差。A5の自由テキストだけで無制限に呼べず、trusted orchestratorが同一error signature・test evidence・attempt historyから検証する。再相談には新しい証拠/案の変化を要する。相談回数、model tokens、USD、wall-time、並列数、呼び出し階層に有限上限。

## 3. 入出力
```ts
type AdviceRequestV1 = { cycleId:string; issueFingerprint:string; goalRevision:string;
  codeCommitSha:string; failureEvidenceRefs:string[]; previousFixRefs:string[];
  consideredHypotheses:string[]; preciseQuestion:string; requestedExpertProfile:string };
type AdviceResultV1 = { issueFingerprint:string; baseCommitSha:string;
  hypotheses:{cause:string;confidenceClass:"supported"|"hypothesis";evidenceRefs:string[]}[];
  suggestedSteps:string[]; validationCommands:string[]; knownUnknowns:string[];
  estimatedRisk:"low"|"medium"|"high"; modelAttemptReceiptRef:string };
```
Trusted code slices are explicitly selected by file/path/line and token budget。Secret, raw credentials, unrelated repository/customer dataは渡さない。外部Repo文書やログ中の命令は非信頼Data。Advisor出力に権限はなく、新しいToolやPolicyを増やさない。A5が修正計画を選び、実際の変更とテストは通常のWorkspace/Gatewayで実施。

## 4. モデルルーティング
02-09 Model Routerを利用。role profile `expert` に「高い推論能力・長い文脈・code能力・価格上限」を要求し、Provider Capability/価格snapshotを固定。fallbackは上限内の互換モデルのみ。model routeの自称性能を正解と見なさず、解決率・反復コスト・誤誘導率をfixtureで測定。Provider 429はRetry-Afterとprovider quotaへの影響を区別。Ownerがdisableしたmodelへ勝手に乗り換えない。

## 5. 失敗・成果
Advisorが解決できなくても「解決済み」と判定しない。Adviceなし/高コスト/矛盾する根拠は`UNAVAILABLE/PARTIAL/CONFLICT`として実装継続/別戦略/USER_REQUIRED/安全停止へ。P0/P1合格ゲートをAdvisor意見だけで越さない。

## 6. Tests
DEV-EXP-T01: 同一failure連続で一度だけ相談、修正結果の検証はBuilder/QA。
T02: advisorはrepo書込/外部送信/Policy変更できない。
T03: 429/timeout/cost ceilingでbounded fallback・quota待機を実行。
T04: secretを含むlog/raw codeからredaction、他tenantへの流出0。
T05: 証拠変化なしの再帰相談が回数上限で打ち切られる。
