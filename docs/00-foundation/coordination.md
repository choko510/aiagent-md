# Parallel Authorship & Conflict Resolution

## Ownership
CHAT_00は`docs/00-foundation/`の**予定された5つの仕様MDのみ**。CHAT_01〜CHAT_11はそれぞれの番号のカテゴリのみ。bootstrapの`architecture-baseline.md`, `spec-index.md`, `coordination.md`, `research-protocol.md`は管理担当のみ編集可。`AGENTS.md`/`prompts/`/`templates/`も管理担当のみ。

## Procedure
1. 各Chatは最新mainとガイダンスを実際に読み、担当ファイルと既存PRを調査する。
2. 優先順で1仕様（最大2つ）を詳細に調査・執筆する。
3. `spec/chat-XX-<module>`ブランチをmainから新規作成。自分のファイルだけコミットし**Draft PR**提出。
4. PR本文には根拠URL・調査日・確認できたコード位置、決定理由、他担当への提案、未解決、実際に実施した検証を明記する。
5. 人間のレビュー・マージを待つ。次の仕様では最新mainを再取得。自動マージ・main直push禁止。

## Canonical ownership & provisional fields
共有型・Event・StateはCHAT_01。ビジネス要求とADRはCHAT_00。Tool契約はCHAT_03。物理DDLはCHAT_10。安全性の共通規則はCHAT_09。他領域の正本を複製しない。確定前は`[PROVISIONAL: CHAT_XX]`として「提案対象のファイル・変更内容・理由・影響・試験」をPRで渡す。

## Parallel timing
CHAT_00とCHAT_01が設計の中心だが、残り10チャットは独立した**既存OSS調査・Draft仕様作成を同時開始可能**。共通型が未Lockedの間、依存部分をLockedにしない。

## Status
Draft → Reviewed → Locked → Implemented → Verified。初期段階の74MDは全てDraft/未作成。実装コードが存在するという意味ではない。

## New independent ownership categories (2026-10-30)
User approved 11 additional features. CHAT_12 owns `docs/12-intelligence/`; CHAT_13 owns `docs/13-event-ingestion/`; CHAT_14 owns `docs/14-adaptive-experience/`; CHAT_15 owns `docs/15-multimodal/`. All coordinator-owned guidance/index docs remain coordinator-only. Original CHAT_00–11 ownership is unchanged. Interface proposals to CHAT_01/03/04/05/06/07/08/09/10 are mandatory; no cross-owner edits. Each new author works on at most 1–2 module specs per Draft PR, no main push or auto-merge. `docs/00-foundation/feature-expansion-2026-10.md` defines the initial accepted expansion scope.
