# Database Schema and Migrations — 詳細実装仕様（10-01）

> Owner: CHAT_10 | Canonical path: `docs/10-infrastructure/database-schema-and-migrations.md` | Status: **Draft / PROVISIONAL dependencies** | Revision: 0.1 | Research: 2026-10-30 JST | Source main: `92b5827ed16a7d4ae61e8146ce382795398293cb`  
> 読み手: 後日実装するCodex、CHAT_01/02/03/07/09/11、レビュー担当。**本MDは提案でありDBを作成・試験した記録ではない。**

## 0. 結論と採用案

PostgreSQLを業務データの**唯一のトランザクション正本**とし、`agent` schemaにTask/Run/Tool Intent/Approval/Evidence/Watch/Observation/Notification/Outbox等の**物理的な配置、参照整合性、複合一意制約、検索インデックス、移行手順**を持つ。Artifact原本・ブラウザCookie・Secret値はDBへ直書きしない。別Storeの不変オブジェクトへ参照を張る。Queue内部のジョブ管理は同じPostgres上でも`pgboss`等の**別schema**に隔離する。DBスキーマをAgent推論プロンプトやHTMLから動的作成・変更しない。

**権限の線引き:** `docs/01-contracts/` がTask/Run/Event/State/Error/Evidenceの意味と公開型、`docs/02-runtime/` がcheckpoint/復帰、`docs/03-tools/` がTool Intent/Approval/Side-effect、`docs/07-automation/` がWatch/通知、`docs/09-security/` が認証・secret・保持方針の正本。ここでは状態値・権限判断・Delivery semanticsを確定せず、`[PROVISIONAL: CHAT_XX]` として物理列を提案する。現時点で他担当のLocked仕様は確認できなかった。*DraftをLockedにしてはならない。*

**採用判断:**
1. Postgresの`FK` / `CHECK` / `UNIQUE` / 同一Transactionによる一貫性を最優先。大きなJSONBだけの「万能テーブル」は却下。
2. 単一Postgresデータベースに`agent` schemaを作り、`pgboss` schemaとは分離。Durable Taskの正本は**queue行ではない**。
3. Tool Intentと承認の確定状態を**外部実行より前にcommit**。不明な副作用は「未確定」を残し再承認/照会する。DB transactionがHTTPやTelegramとatomicになる保証はない。
4. 追加カラム→両版読取→バックフィル→切替→制約強化→旧列削除のexpand/contract方式。非transactionalな`CREATE INDEX CONCURRENTLY`は別migration stepに分離。
5. Phase-1単一Ubuntuサーバー前提。ただし`tenant_id`でデータ分離の物理土台を置く。実際のidentity/tenant認可はCHAT_09が決めるまで`tenant_id`だけを安全境界と**見なさない**。

## 1. 目的・責務・非責務・受入シナリオ

**目的:** Webチャットから数時間の調査、価格監視、通知まで、プロセスが死んでも復旧する状態・時点・出典・重複防止の保存基盤を実装可能な形で定義する。SC-01〜09、特にSC-02/06/07/08/09を対象とする。

**責務:** SQL DDL、レコードIDと複合FK、索引設計、Commit境界、Version/unique、migration実行、バックフィル、稼働中Read/Write互換、保持と復旧用DB問い合わせ。

**非責務:** 認証方式、ユーザーRole、Task/Run状態遷移の合法性、AgentLoopのcheckpoint意味論、Side-effect Policy、Watch predicate、Notification retry数、Browser cookie暗号化、実ジョブconsumerの実装、バックアップ手順。これらは各Ownerの正本に委譲する。

**前提/仮定:** PostgreSQL >=16を導入候補、PG18公式ドキュメントを今回のSQL確認根拠に使用。初期実装のPostgreSQL**正確なmajor versionは未Lock**。UUIDv7はtrusted server側で生成し、DBにUUIDv4を生成する既定値を置かない。採用するPostgreSQL majorへのDDL実行試験が必須。Timeは`timestamptz`で保持し、ユーザーTZは別文字列で保持、DBセッションTZはUTCに固定する。大きなメディア/HTMLはArtifact Storeへ退避。

## 2. 既存OSS横断調査（2026-10-30、実ソース/テストを読んだもの）

| 対象 / 検証版 | 実際に確認したコード・テスト | 利用できる設計 | そのまま採用しない点 / License |
|---|---|---|---|
| pg-boss `master@71ea068b`, package `12.38.0` [S1] | `src/plans.ts`: `create()`でversion/queue/schedule/job/dependency等を分離。`src/migrationStore.ts`: `migrateCommands()`系でtransactional DDLとCONCURRENTLY Indexを区別。`test/migrationTest.ts`にmigration/rollback/schema検証、`test/attemptFenceTest.ts`に古いworkerが新しいattemptを完了できない検証。[S2][S3][S4] | ジョブschema分離、別段階の索引移行、attempt fencingの試験設計。 | 独自のTask/Tool業務状態をpg-boss jobに押し込まない。今回のTaskはジョブより長寿命でWAITING_INPUT等を含む。MIT [S5]。 |
| LangGraph checkpoint-postgres `main@ffc33363`, package `3.2.0` [S6] | `base.py`: `MIGRATIONS`でcheckpoints/blobs/writesを別表＋複合PK、`UPSERT_CHECKPOINTS_SQL` / `UPSERT_CHECKPOINT_WRITES_SQL`。`__init__.py`: `PostgresSaver.setup()`がmigration versionを順に登録。`tests/test_sync.py`: `test_nonnull_migrations()` / `test_pending_sends_migration()`。[S7][S8][S9] | checkpointとpending writesを分け、再取得/移行テストの観点を流用。 | Python SDK専用の`thread_id`を汎用Task IDに直結しない。pending writesをこのプロジェクトのSide-effect結果として誤認しない。MIT [S10]。 |
| Temporal `main@3eb5fb80`, PostgreSQL v12 schema, versioned v1.19 [S11] | `schema/postgresql/v12/temporal/schema.sql`: executions/current_executions/history/queue等の複合PK。`versioned/v1.19/current_chasm_executions.sql`と`manifest.json`: `CurrVersion 1.19`, `MinCompatibleVersion 1.0`を確認。[S12][S13] | 現行参照と履歴の分離、manifestに互換floorとversionを持つ運用方針。 | Temporalサーバー・workflow再現エンジンはこの規模の単一ホストには過剰。schema/データ型の直接コピーはしない。MIT [S14]。 |
| PostgreSQL Official Docs 18 [S15-S18] | FK/一意B-tree、partial/partitioned index、MVCC、`CONCURRENTLY`制約を調査。 | 複合uniqueと索引、行ロック、制約と運用の限界を定義。 | PG18固有機能は導入major決定まで利用前提にしない。PostgreSQL License。 |

**採否比較（最低2案）:** (A) 業務永続化をPostgres + キュー用`pgboss` schema + Object Store（採用候補）。(B) 単一`jobs` JSONBテーブルに会話・Task・Evidence・Watchを全収納（却下、FK・保持・時点/状態検証・再開時の競合が脆い）。(C) 全体をTemporalのワークフローDBに依存（現段階で却下、サーバーの運用とWorkflow semantics依存が大きい。将来Adapter比較可能）。(D) LangGraph Postgres Saverをそのまま流用（却下、Runtimeの正本状態・Event envelopeの対応が未確定）。案Aも可用性/負荷特性は**未実測**であり、pg-boss採用自体はCHAT_10-02との合意待ち。

## 3. 依存関係・正本リンク・所有権

`
Product Brief / Architecture Rules (CHAT_00)
        ↓
CHAT_01: Core Models, Event Envelope, State, Evidence, Errors
        ├── CHAT_02: checkpoint / Run ownership
        ├── CHAT_03: Tool Intent / Approval / Ledger
        ├── CHAT_07: Watch / Observation / Notification
        └── CHAT_09: Workspace identity / Secret refs / Retention
                         ↓ physical projection only
                 CHAT_10: PostgreSQL agent.*
                        ├── queue adapter → pgboss.* (CHAT_10-02)
                        ├── artifact keys → opaque Artifact Store
                        └── backup / observability (CHAT_10-04/05)
                 CHAT_11: DB consistency / race / migration tests
`

- `tenant_id`はデータの物理スコープ識別子。`[PROVISIONAL: CHAT_09]` identity・authorization・Single-user modeとの写像。**全SELECT/UPDATE/DELETEは認証済みscopeを明示**し、サーバー側でチェックする。RLSは将来の防御層候補、未定義のRoleをSQLで仮定しない。
- `task_id`, `run_id`, `event_id`等はUUID（物理表現の提案）。API/イベント型が確定すれば変換レイヤーを用意する。`[PROVISIONAL: CHAT_01]`。
- Raw body/HTML/スクリーンショット: `artifact_object_key`は非公開ストアの不透明キー。外部URLをそのまま読み込むツールではない。実URIの有効期限・署名は別管理。
- `status_code` / `kind_code`: **enum値を本MDで独自定義しない**。CHAT_01/03/07の正本値をDB adapterでvalidate。無効値を無検証で保存する設計ではない。`[PROVISIONAL: CHAT_01, CHAT_03, CHAT_07]`。
- Physical tablesと制約はCHAT_10の正本。共有イベント・API responseがDB rowそのものを露出しない。

## 4. 要求定義（全MUSTにテスト割当）

| REQ-ID | Priority | 実装上の条件・測定可能な結果 | SC |
|---|---|---|---|
| DB-001 | MUST | 全業務書込は単一Postgresを正本とし、Commit後の再起動で参照できる | 02,07 |
| DB-002 | MUST | 同一tenant内で全親子参照に複合FKを張り、別scope参照をDBが拒否 | 06,08 |
| DB-003 | MUST | Task/Run/Watch/Observation/Tool/ApprovalのIDと関係を構造化、共有状態値はOwnerに委譲 | 01,02,07 |
| DB-004 | MUST | Event sequenceをTaskごとに単調一意に割当、状態変更+Event+Outboxを1 transactionでcommit | 07 |
| DB-005 | MUST | Tool Intentが確実に保存される前に外部Executorを呼ばない | 07,08 |
| DB-006 | MUST | 同一intentの試行・通知・登録requestの衝突にunique制約。異なるpayloadの同Keyは409扱い | 02,07 |
| DB-007 | MUST | attempt fencingで旧Workerの更新を0行にし、外部副作用を盲目的再送しない | 07 |
| DB-008 | MUST | Evidenceに観測/取得時刻、出典、raw artifact参照と検証状態を分ける。失敗値を0として保存しない | 01〜05 |
| DB-009 | MUST | HTML/認証Cookie/secret値を業務DB JSON・監査ログへ埋め込まない | 06,08 |
| DB-010 | MUST | Watch停止状態でも履歴が残り、schedule/next run情報を再起動後に復元 | 02 |
| DB-011 | MUST | Outboxはdomain changeと同じCommitで記録し、Dispatch未達を再発見できる | 02,07 |
| DB-012 | MUST | schema_migrationsの順序・checksum・互換floor・実行主体の排他を管理 | 07 |
| DB-013 | MUST | 破壊的DDL前にexpand/backfill/contractとrollback policyを定義 | 07 |
| DB-014 | MUST | Indexは実Queryに紐付け、EXPLAIN・WAL・write負荷を実測して承認 | 09 |
| DB-015 | MUST | Query時間・connection・保持容量・migration時間に運用上限/alertを設定 | 09 |
| DB-016 | MUST | Secret referenceと内部object keyはClient/LLM・非権限Workerへ漏らさない | 08 |
| DB-017 | MUST | 通知の外部送信が成功したか不明でも正本を消さず状態照会/重複抑制 | 02,07 |
| DB-018 | MUST | Backup restore後にSchema/Artifact/Run参照の整合性監査ができる | 07 |
| DB-019 | SHOULD | 大表のIndex buildをCONCURRENTLYで分離し業務停止を短縮 | 09 |
| DB-020 | SHOULD | 大きいEvent/Observationは実測に基づき保持・partitionを決定 | 02,09 |
| DB-021 | LATER | RLSを追加するならAPI Role、migration Role、connection poolに分離してFault test | 08 |
| DB-022 | EXPERIMENT | 旧データの分割・BRIN・圧縮は実測と復旧試験が通る場合のみ | 09 |

## 5. 物理モデルとバージョン管理の契約

### 5.1 ID / スコープ / Time / JSON / トランザクション

- すべての業務テーブルに`tenant_id uuid not null`と`(tenant_id,id)`複合PKを使う（単一ユーザーでも将来の交差参照事故を防ぐ）。UUID生成は**APIのtrusted persistence adapter**で行ってよい。DBのdefault生成は補助。IDsをquery parameterとして渡す（SQL文字列連結禁止）。
- `created_at`、`observed_at`、`fetched_at`、`due_at`、`expires_at`はUTC解釈の`timestamptz`、`timezone_id`はIANAのTZ名（CHAT_07正本でvalidate）。実測時刻/サイト表示時刻/DB記録時刻は異なる。`updated_at`はDB triggerに依存させず更新SQLで明示する。
- `jsonb`は正本Schemaバージョン列と共に使用し、trusted adapterでJSON Schema/Zod等を検証。重要な外部キー、query key、時間、idempotencyはJSONB内部に隠さない。
- DBは使える限り`READ COMMITTED` + row lock + uniqueで実装。複数Entityをまたぐ不変条件は明示的なlock順序を使用、必要な箇所だけSerializable+`40001`全Transaction再試行。**外部HTTP requestをSQL transaction内に入れない**。
- 非null status textの値は未決。初期migrationはCHECK enumを付けず、状態validatorがOwner canonicalに一致した時点で`CHECK ... NOT VALID`→validationとして後続migrationで追加する。
- 並列write時の楽観排他には`record_version bigint NOT NULL DEFAULT 1`を使い、`UPDATE ... WHERE revision = previous RETURNING`で0行なら競合。Event writerはDomain row→aggregate counter→tenant counterの固定順序でTX内でロックする。

### 5.2 Physical v1 proposal（初期migrationのSQL）

以下は**採用提案SQL**。DB上の整数/UUID等の型はCHAT_01/09確認後に確定。特に`tenant`、Tool/Watchのstatus/payload schemaは暫定。アプリからDDLを動的実行するためのコードではなく、repository内`migrations/0001_initial.sql`（**実装repo、未作成**）に格納する想定。

```sql
BEGIN;
CREATE SCHEMA IF NOT EXISTS agent;

CREATE TABLE agent.schema_migrations (
  version integer PRIMARY KEY CHECK (version > 0),
  checksum_sha256 char(64) NOT NULL,
  name text NOT NULL,
  phase text NOT NULL CHECK (phase IN ('transactional','postcommit')),
  applied_at timestamptz NOT NULL DEFAULT now(),
  app_min_reader integer NOT NULL,
  app_min_writer integer NOT NULL
);
CREATE TABLE agent.schema_control (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  schema_version integer NOT NULL CHECK (schema_version >= 0),
  min_compatible_reader integer NOT NULL CHECK (min_compatible_reader >= 0),
  min_compatible_writer integer NOT NULL CHECK (min_compatible_writer >= 0),
  changed_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO agent.schema_control VALUES (true, 0, 0, 0, now());

CREATE TABLE agent.tenants (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL DEFAULT now(),
  disabled_at timestamptz
);
-- tenantはauth principalではない。identity bindingはCHAT_09が決定。

CREATE TABLE agent.users (
 id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(), disabled_at timestamptz
);
CREATE TABLE agent.tenant_memberships (
 tenant_id uuid NOT NULL REFERENCES agent.tenants(id),
 user_id uuid NOT NULL REFERENCES agent.users(id),
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,user_id)
);
CREATE TABLE agent.conversations (
  tenant_id uuid NOT NULL REFERENCES agent.tenants(id),
  id uuid NOT NULL,
  external_channel_ref text,
  owner_user_id uuid NOT NULL,
  root_thread_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  record_version bigint NOT NULL DEFAULT 1 CHECK (record_version BETWEEN 1 AND 9223372036854775807),
  PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id,owner_user_id) REFERENCES agent.tenant_memberships(tenant_id,user_id)
);

CREATE TABLE agent.threads (
 tenant_id uuid NOT NULL, id uuid NOT NULL, conversation_id uuid NOT NULL,
 parent_thread_id uuid, next_message_seq bigint NOT NULL DEFAULT 1 CHECK(next_message_seq>0),
 record_version bigint NOT NULL DEFAULT 1,
 PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,conversation_id,id),
 FOREIGN KEY(tenant_id,conversation_id) REFERENCES agent.conversations(tenant_id,id),
 FOREIGN KEY(tenant_id,conversation_id,parent_thread_id) REFERENCES agent.threads(tenant_id,conversation_id,id)
);
ALTER TABLE agent.conversations ADD CONSTRAINT conversations_root_thread_fk
 FOREIGN KEY(tenant_id,id,root_thread_id) REFERENCES agent.threads(tenant_id,conversation_id,id)
 DEFERRABLE INITIALLY DEFERRED;
CREATE UNIQUE INDEX threads_one_root ON agent.threads(tenant_id,conversation_id) WHERE parent_thread_id IS NULL;
CREATE TABLE agent.messages (
 tenant_id uuid NOT NULL, id uuid NOT NULL, thread_id uuid NOT NULL, seq bigint NOT NULL CHECK(seq>0),
 role_code text NOT NULL, content_ref text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 record_version bigint NOT NULL DEFAULT 1,
 PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,thread_id,seq),
 FOREIGN KEY(tenant_id,thread_id) REFERENCES agent.threads(tenant_id,id)
);
CREATE TABLE agent.tasks (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  conversation_id uuid,
  owner_user_id uuid NOT NULL,
  origin_thread_id uuid,
  parent_task_id uuid,
  retry_of_task_id uuid,
  created_from_watch_id uuid,
  trigger_key text,
  goal_revision bigint NOT NULL DEFAULT 1 CHECK(goal_revision BETWEEN 1 AND 9223372036854775807),
  active_run_id uuid,
  status_code text NOT NULL,
  canonical_schema_version integer NOT NULL CHECK (canonical_schema_version > 0),
  request_json jsonb NOT NULL CHECK (jsonb_typeof(request_json)='object'),
  constraint_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  deadline_at timestamptz,
  record_version bigint NOT NULL DEFAULT 1 CHECK (record_version BETWEEN 1 AND 9223372036854775807),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(tenant_id,id),
  FOREIGN KEY(tenant_id,conversation_id)
    REFERENCES agent.conversations(tenant_id,id),
 FOREIGN KEY(tenant_id,owner_user_id) REFERENCES agent.tenant_memberships(tenant_id,user_id),
 FOREIGN KEY(tenant_id,parent_task_id) REFERENCES agent.tasks(tenant_id,id),
 FOREIGN KEY(tenant_id,retry_of_task_id) REFERENCES agent.tasks(tenant_id,id),
 FOREIGN KEY(tenant_id,conversation_id,origin_thread_id) REFERENCES agent.threads(tenant_id,conversation_id,id),
 CHECK(origin_thread_id IS NULL OR conversation_id IS NOT NULL),
 CHECK(retry_of_task_id IS NULL OR retry_of_task_id <> id)
);
CREATE INDEX tasks_by_conversation ON agent.tasks
  (tenant_id,conversation_id,created_at DESC);
CREATE INDEX tasks_by_status ON agent.tasks
  (tenant_id,status_code,updated_at DESC);

CREATE UNIQUE INDEX task_trigger_once ON agent.tasks(tenant_id,created_from_watch_id,trigger_key) WHERE created_from_watch_id IS NOT NULL AND trigger_key IS NOT NULL;
CREATE TABLE agent.task_goal_revisions (
 tenant_id uuid NOT NULL, task_id uuid NOT NULL,
 revision bigint NOT NULL CHECK(revision BETWEEN 1 AND 9223372036854775807),
 goal_ref text NOT NULL, goal_sha256 char(64) NOT NULL CHECK(goal_sha256 ~ '^[0-9a-f]{64}$'),
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,task_id,revision),
 FOREIGN KEY(tenant_id,task_id) REFERENCES agent.tasks(tenant_id,id)
);
ALTER TABLE agent.tasks ADD CONSTRAINT tasks_current_goal_fk FOREIGN KEY(tenant_id,id,goal_revision)
 REFERENCES agent.task_goal_revisions(tenant_id,task_id,revision) DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE agent.runs (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  task_id uuid NOT NULL,
  attempt_no integer NOT NULL CHECK (attempt_no > 0),
  input_goal_revision bigint NOT NULL CHECK(input_goal_revision>0),
  lease_epoch bigint NOT NULL DEFAULT 0 CHECK(lease_epoch BETWEEN 0 AND 9223372036854775807),
  lease_owner_ref text, lease_expires_at timestamptz,
  status_code text NOT NULL,
  state_schema_version integer NOT NULL CHECK (state_schema_version > 0),
  record_version bigint NOT NULL DEFAULT 1 CHECK (record_version BETWEEN 1 AND 9223372036854775807),
  started_at timestamptz,
  ended_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(tenant_id,id),
  UNIQUE(tenant_id,task_id,id),
  UNIQUE(tenant_id,task_id,attempt_no),
  FOREIGN KEY(tenant_id,task_id) REFERENCES agent.tasks(tenant_id,id),
 FOREIGN KEY(tenant_id,task_id,input_goal_revision) REFERENCES agent.task_goal_revisions(tenant_id,task_id,revision)
);
ALTER TABLE agent.tasks ADD CONSTRAINT task_active_run_fk FOREIGN KEY(tenant_id,id,active_run_id)
 REFERENCES agent.runs(tenant_id,task_id,id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX runs_by_task ON agent.runs(tenant_id,task_id,attempt_no DESC);

CREATE TABLE agent.artifacts (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  object_key text NOT NULL,
  sha256_hex char(64) NOT NULL CHECK (sha256_hex ~ '^[0-9a-f]{64}$'),
  bytes bigint NOT NULL CHECK (bytes >= 0),
  media_type text NOT NULL,
  classification_code text NOT NULL,
  object_state_code text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz,
  PRIMARY KEY(tenant_id,id),
  UNIQUE(tenant_id,object_key),
  CHECK (length(object_key) BETWEEN 1 AND 1024)
);
-- object_keyは検証済みStore key、HTTP URLではない。署名URLは保存しない。

-- Append-only checkpoint history: multiple commits at the same step are legitimate.
CREATE TABLE agent.run_checkpoints (
 tenant_id uuid NOT NULL, id uuid NOT NULL,
 task_id uuid NOT NULL, run_id uuid NOT NULL,
 checkpoint_revision bigint NOT NULL CHECK(checkpoint_revision BETWEEN 1 AND 9223372036854775807),
 step_no bigint NOT NULL CHECK(step_no >= 0),
 phase_code text NOT NULL CHECK(length(phase_code) BETWEEN 1 AND 64),
 writer_lease_epoch bigint NOT NULL CHECK(writer_lease_epoch BETWEEN 0 AND 9223372036854775807),
 applied_goal_revision bigint NOT NULL CHECK(applied_goal_revision BETWEEN 1 AND 9223372036854775807),
 checkpoint_schema_version integer NOT NULL CHECK(checkpoint_schema_version > 0),
 checkpoint_json jsonb, artifact_id uuid,
 snapshot_sha256 char(64) NOT NULL CHECK(snapshot_sha256 ~ '^[0-9a-f]{64}$'),
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,id),
 UNIQUE(tenant_id,run_id,checkpoint_revision),
 FOREIGN KEY(tenant_id,task_id,run_id) REFERENCES agent.runs(tenant_id,task_id,id),
 FOREIGN KEY(tenant_id,task_id,applied_goal_revision) REFERENCES agent.task_goal_revisions(tenant_id,task_id,revision),
 FOREIGN KEY(tenant_id,artifact_id) REFERENCES agent.artifacts(tenant_id,id),
 CHECK ((checkpoint_json IS NOT NULL) <> (artifact_id IS NOT NULL))
);
CREATE INDEX checkpoints_latest ON agent.run_checkpoints(tenant_id,run_id,checkpoint_revision DESC);
ALTER TABLE agent.run_checkpoints ADD CONSTRAINT checkpoint_belongs_to_run UNIQUE(tenant_id,id,run_id);
ALTER TABLE agent.runs ADD COLUMN current_checkpoint_id uuid;
ALTER TABLE agent.runs ADD CONSTRAINT runs_checkpoint_head_fk
 FOREIGN KEY (tenant_id,current_checkpoint_id,id) REFERENCES agent.run_checkpoints(tenant_id,id,run_id)
 DEFERRABLE INITIALLY DEFERRED;

-- Canonical CHAT_01 Event Envelope: Task/Run/Watch/Tool/etc, not just Task events.
-- Both counters are advanced ONLY within the domain mutation + Event + Outbox transaction.
CREATE TABLE agent.event_aggregate_counters (
 tenant_id uuid NOT NULL REFERENCES agent.tenants(id),
 aggregate_type text NOT NULL CHECK(aggregate_type IN
  ('task','run','watch','approval','tool','evidence','notification')),
 aggregate_id uuid NOT NULL,
 next_aggregate_sequence bigint NOT NULL DEFAULT 1
  CHECK(next_aggregate_sequence BETWEEN 1 AND 9223372036854775807),
 PRIMARY KEY(tenant_id,aggregate_type,aggregate_id)
);
CREATE TABLE agent.tenant_event_counters (
 tenant_id uuid PRIMARY KEY REFERENCES agent.tenants(id),
 next_delivery_offset bigint NOT NULL DEFAULT 1
  CHECK(next_delivery_offset BETWEEN 1 AND 9223372036854775807)
);
-- The tenant-wide replay cutoff is durable metadata, not inferred from filtered SSE results.
CREATE TABLE agent.event_retention_floors (
 tenant_id uuid PRIMARY KEY REFERENCES agent.tenants(id),
 earliest_replayable_offset bigint NOT NULL DEFAULT 1
  CHECK (earliest_replayable_offset BETWEEN 1 AND 9223372036854775807),
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE agent.domain_events (
 tenant_id uuid NOT NULL REFERENCES agent.tenants(id),
 id uuid NOT NULL,
 aggregate_type text NOT NULL CHECK(aggregate_type IN
  ('task','run','watch','approval','tool','evidence','notification')),
 aggregate_id uuid NOT NULL,
 aggregate_sequence bigint NOT NULL CHECK(aggregate_sequence > 0),
 delivery_offset bigint NOT NULL CHECK(delivery_offset > 0),
 event_kind text NOT NULL,
 schema_version text NOT NULL CHECK(schema_version ~ '^[1-9][0-9]*[.][0-9]+$'),
 payload_schema_version text NOT NULL CHECK(payload_schema_version ~ '^[1-9][0-9]*[.][0-9]+$'),
 payload_json jsonb NOT NULL CHECK(jsonb_typeof(payload_json)='object'),
 actor_json jsonb NOT NULL CHECK(jsonb_typeof(actor_json)='object'),
 audience_code text NOT NULL CHECK(audience_code IN ('owner_private','operator_audit')),
 owner_user_id uuid, task_id uuid, run_id uuid, watch_id uuid,
 causation_event_id uuid,
 correlation_id text NOT NULL CHECK(length(correlation_id) BETWEEN 1 AND 256),
 trace_id text,
 occurred_at timestamptz NOT NULL,
 committed_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,id),
 UNIQUE(tenant_id,aggregate_type,aggregate_id,aggregate_sequence),
 UNIQUE(tenant_id,delivery_offset),
 FOREIGN KEY(tenant_id,aggregate_type,aggregate_id)
  REFERENCES agent.event_aggregate_counters(tenant_id,aggregate_type,aggregate_id),
 FOREIGN KEY(tenant_id,owner_user_id) REFERENCES agent.tenant_memberships(tenant_id,user_id),
 FOREIGN KEY(tenant_id,task_id) REFERENCES agent.tasks(tenant_id,id),
 FOREIGN KEY(tenant_id,task_id,run_id) REFERENCES agent.runs(tenant_id,task_id,id),
 FOREIGN KEY(tenant_id,causation_event_id) REFERENCES agent.domain_events(tenant_id,id)
  DEFERRABLE INITIALLY DEFERRED,
 CHECK(run_id IS NULL OR task_id IS NOT NULL),
 CHECK(audience_code <> 'owner_private' OR owner_user_id IS NOT NULL)
);
CREATE INDEX domain_events_task_replay ON agent.domain_events(tenant_id,task_id,delivery_offset) WHERE task_id IS NOT NULL;
CREATE INDEX domain_events_watch_replay ON agent.domain_events(tenant_id,watch_id,delivery_offset) WHERE watch_id IS NOT NULL;
CREATE INDEX domain_events_aggregate ON agent.domain_events(tenant_id,aggregate_type,aggregate_id,aggregate_sequence DESC);

CREATE TABLE agent.tool_intents (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  task_id uuid NOT NULL,
  run_id uuid NOT NULL,
  tool_key text NOT NULL,
  invocation_key text NOT NULL,
  request_sha256 char(64) NOT NULL,
  request_schema_version integer NOT NULL CHECK (request_schema_version > 0),
  safe_arguments_json jsonb NOT NULL CHECK (jsonb_typeof(safe_arguments_json)='object'),
  status_code text NOT NULL,
  policy_decision_ref text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  record_version bigint NOT NULL DEFAULT 1 CHECK (record_version BETWEEN 1 AND 9223372036854775807),
  PRIMARY KEY(tenant_id,id),
  UNIQUE(tenant_id,run_id,invocation_key),
  FOREIGN KEY(tenant_id,task_id,run_id)
    REFERENCES agent.runs(tenant_id,task_id,id)
);
CREATE INDEX tool_intents_by_run ON agent.tool_intents
  (tenant_id,run_id,created_at DESC);

CREATE TABLE agent.approval_records (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  tool_intent_id uuid NOT NULL,
  request_sha256 char(64) NOT NULL,
  decision_code text NOT NULL,
  expires_at timestamptz,
  record_version bigint NOT NULL DEFAULT 1 CHECK (record_version BETWEEN 1 AND 9223372036854775807),
  requested_at timestamptz NOT NULL DEFAULT now(),
  decided_at timestamptz,
  PRIMARY KEY(tenant_id,id),
  FOREIGN KEY(tenant_id,tool_intent_id)
    REFERENCES agent.tool_intents(tenant_id,id)
);
CREATE INDEX approvals_for_intent ON agent.approval_records
  (tenant_id,tool_intent_id,requested_at DESC);

CREATE TABLE agent.tool_attempts (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  tool_intent_id uuid NOT NULL,
  attempt_no integer NOT NULL CHECK (attempt_no > 0),
  fence_token bigint NOT NULL CHECK (fence_token > 0),
  worker_ref text NOT NULL,
  status_code text NOT NULL,
  started_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz,
  ended_at timestamptz,
  result_artifact_id uuid,
  PRIMARY KEY(tenant_id,id),
  UNIQUE(tenant_id,tool_intent_id,attempt_no),
  UNIQUE(tenant_id,tool_intent_id,fence_token),
  FOREIGN KEY(tenant_id,tool_intent_id)
    REFERENCES agent.tool_intents(tenant_id,id),
  FOREIGN KEY(tenant_id,result_artifact_id)
    REFERENCES agent.artifacts(tenant_id,id)
);
CREATE INDEX tool_attempts_by_intent ON agent.tool_attempts
  (tenant_id,tool_intent_id,attempt_no DESC);

CREATE TABLE agent.side_effect_records (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  tool_intent_id uuid NOT NULL,
  effect_key text NOT NULL,
  request_sha256 char(64) NOT NULL,
  outcome_code text NOT NULL,
  external_receipt_ref text,
  result_artifact_id uuid,
  last_checked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(tenant_id,id),
  UNIQUE(tenant_id,effect_key),
  FOREIGN KEY(tenant_id,tool_intent_id)
    REFERENCES agent.tool_intents(tenant_id,id),
  FOREIGN KEY(tenant_id,result_artifact_id)
    REFERENCES agent.artifacts(tenant_id,id)
);
-- outcome_code (including UNKNOWN) is canonical CHAT_03-owned contract.

CREATE TABLE agent.evidence_records (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  task_id uuid NOT NULL,
  run_id uuid,
  artifact_id uuid,
  source_locator text NOT NULL,
  source_kind_code text NOT NULL,
  observed_at timestamptz,
  fetched_at timestamptz NOT NULL,
  confidence_code text NOT NULL,
  verification_code text NOT NULL,
  canonical_schema_version integer NOT NULL CHECK (canonical_schema_version > 0),
  evidence_json jsonb NOT NULL CHECK (jsonb_typeof(evidence_json)='object'),
  PRIMARY KEY(tenant_id,id),
  FOREIGN KEY(tenant_id,task_id) REFERENCES agent.tasks(tenant_id,id),
  FOREIGN KEY(tenant_id,task_id,run_id)
    REFERENCES agent.runs(tenant_id,task_id,id),
  FOREIGN KEY(tenant_id,artifact_id)
    REFERENCES agent.artifacts(tenant_id,id)
);
CREATE INDEX evidence_by_task ON agent.evidence_records
  (tenant_id,task_id,fetched_at DESC);
-- source_locatorはcredentials/URL query token除去済み。非null observed_atを強制しない（不明はNULL）。

CREATE TABLE agent.browser_session_refs (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  task_id uuid NOT NULL,
  run_id uuid,
  browser_worker_ref text NOT NULL,
  session_state_code text NOT NULL,
  storage_artifact_id uuid,
  expires_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(tenant_id,id),
  FOREIGN KEY(tenant_id,task_id) REFERENCES agent.tasks(tenant_id,id),
  FOREIGN KEY(tenant_id,task_id,run_id)
    REFERENCES agent.runs(tenant_id,task_id,id),
  FOREIGN KEY(tenant_id,storage_artifact_id)
    REFERENCES agent.artifacts(tenant_id,id)
);
-- Cookie/sessionデータの実体はBrowser/Secret Store側で管理。

CREATE TABLE agent.watches (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  task_id uuid,
  status_code text NOT NULL,
  owner_user_id uuid NOT NULL, created_by_task_id uuid,
  definition_revision bigint NOT NULL DEFAULT 1 CHECK(definition_revision>0),
  condition_schema_version integer NOT NULL CHECK (condition_schema_version > 0),
  condition_json jsonb NOT NULL CHECK (jsonb_typeof(condition_json)='object'),
  schedule_json jsonb NOT NULL CHECK (jsonb_typeof(schedule_json)='object'),
  timezone_id text NOT NULL,
  next_due_at timestamptz,
  record_version bigint NOT NULL DEFAULT 1 CHECK (record_version BETWEEN 1 AND 9223372036854775807),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(tenant_id,id),
  FOREIGN KEY(tenant_id,task_id) REFERENCES agent.tasks(tenant_id,id),
 FOREIGN KEY(tenant_id,owner_user_id) REFERENCES agent.tenant_memberships(tenant_id,user_id),
 FOREIGN KEY(tenant_id,created_by_task_id) REFERENCES agent.tasks(tenant_id,id)
);
ALTER TABLE agent.tasks ADD CONSTRAINT task_watch_fk FOREIGN KEY(tenant_id,created_from_watch_id) REFERENCES agent.watches(tenant_id,id);
ALTER TABLE agent.domain_events ADD CONSTRAINT domain_events_watch_fk
 FOREIGN KEY(tenant_id,watch_id) REFERENCES agent.watches(tenant_id,id);
CREATE INDEX watches_due ON agent.watches(next_due_at,id)
  WHERE next_due_at IS NOT NULL;
CREATE INDEX watches_by_tenant ON agent.watches(tenant_id,status_code,next_due_at);

CREATE TABLE agent.watch_observations (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  watch_id uuid NOT NULL,
  observation_key text NOT NULL,
  acquisition_code text NOT NULL,
  observed_at timestamptz,
  fetched_at timestamptz NOT NULL DEFAULT now(),
  evidence_id uuid,
  observation_schema_version integer NOT NULL CHECK (observation_schema_version > 0),
  observation_json jsonb NOT NULL CHECK (jsonb_typeof(observation_json)='object'),
  PRIMARY KEY(tenant_id,id),
  UNIQUE(tenant_id,watch_id,observation_key),
  FOREIGN KEY(tenant_id,watch_id) REFERENCES agent.watches(tenant_id,id),
  FOREIGN KEY(tenant_id,evidence_id) REFERENCES agent.evidence_records(tenant_id,id)
);
CREATE INDEX observations_recent ON agent.watch_observations
  (tenant_id,watch_id,fetched_at DESC);

CREATE TABLE agent.notification_records (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  watch_id uuid,
  notification_key text NOT NULL,
  destination_ref text NOT NULL,
  delivery_status_code text NOT NULL,
  trigger_observation_id uuid,
  delivery_receipt_ref text,
  created_at timestamptz NOT NULL DEFAULT now(),
  last_attempt_at timestamptz,
  PRIMARY KEY(tenant_id,id),
  UNIQUE(tenant_id,notification_key),
  FOREIGN KEY(tenant_id,watch_id) REFERENCES agent.watches(tenant_id,id),
  FOREIGN KEY(tenant_id,trigger_observation_id)
    REFERENCES agent.watch_observations(tenant_id,id)
);

CREATE TABLE agent.outbox_messages (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  topic text NOT NULL,
  dedupe_key text NOT NULL,
  event_id uuid,
  notification_id uuid,
  payload_schema_version integer NOT NULL CHECK (payload_schema_version > 0),
  safe_payload_json jsonb NOT NULL CHECK (jsonb_typeof(safe_payload_json)='object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  dispatched_at timestamptz,
  dispatch_attempts integer NOT NULL DEFAULT 0 CHECK (dispatch_attempts >= 0),
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  relay_lease_epoch bigint NOT NULL DEFAULT 0 CHECK (relay_lease_epoch BETWEEN 0 AND 9223372036854775807),
  relay_owner_ref text,
  relay_lease_until timestamptz,
  last_error_code text,
  PRIMARY KEY(tenant_id,id),
  UNIQUE(tenant_id,topic,dedupe_key),
  FOREIGN KEY(tenant_id,event_id) REFERENCES agent.domain_events(tenant_id,id),
  FOREIGN KEY(tenant_id,notification_id)
    REFERENCES agent.notification_records(tenant_id,id),
  CHECK (event_id IS NOT NULL OR notification_id IS NOT NULL)
);
CREATE INDEX outbox_ready ON agent.outbox_messages(next_attempt_at,id)
  WHERE dispatched_at IS NULL;

CREATE TABLE agent.secret_references (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  secret_handle text NOT NULL,
  purpose_code text NOT NULL,
  rotated_at timestamptz,
  revoked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(tenant_id,id),
  UNIQUE(tenant_id,secret_handle)
);
-- 禁止: plaintext secret / cookie / access token / signed access URL を格納。

CREATE TABLE agent.api_idempotency (
  tenant_id uuid NOT NULL,
  actor_user_id uuid NOT NULL, operation_name text NOT NULL,
  operation_key text NOT NULL,
  request_sha256 char(64) NOT NULL,
  response_artifact_id uuid, response_http_status integer, response_json jsonb, completed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  PRIMARY KEY(tenant_id,actor_user_id,operation_name,operation_key),
 FOREIGN KEY(tenant_id,actor_user_id) REFERENCES agent.tenant_memberships(tenant_id,user_id),
  FOREIGN KEY(tenant_id,response_artifact_id)
    REFERENCES agent.artifacts(tenant_id,id)
);
CREATE INDEX api_idempotency_expiry ON agent.api_idempotency(expires_at);

CREATE TABLE agent.cost_usage_records (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  task_id uuid NOT NULL,
  run_id uuid,
  usage_key text NOT NULL,
  metering_kind_code text NOT NULL,
  pricing_snapshot_ref text,
  input_units bigint CHECK (input_units >= 0),
  output_units bigint CHECK (output_units >= 0),
  cached_input_units bigint CHECK (cached_input_units >= 0),
  cached_write_units bigint CHECK (cached_write_units >= 0),
  reasoning_output_units bigint CHECK (reasoning_output_units >= 0),
  usage_kind text NOT NULL,
  provider_key text,
  provider_attempt_id text,
  logical_call_id text,
  source_receipt_sha256 char(64),
  price_snapshot_ref text,
  currency_code char(3),
  amount_minor_units numeric(38,0),
  amount_scale smallint CHECK (amount_scale BETWEEN 0 AND 18),
  measured_at_source timestamptz,
  estimated_cost_micro_usd bigint CHECK (estimated_cost_micro_usd >= 0),
  measured_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(tenant_id,id),
  UNIQUE(tenant_id,usage_key),
  FOREIGN KEY(tenant_id,task_id) REFERENCES agent.tasks(tenant_id,id),
  FOREIGN KEY(tenant_id,task_id,run_id)
    REFERENCES agent.runs(tenant_id,task_id,id)
);
CREATE INDEX cost_by_task ON agent.cost_usage_records
  (tenant_id,task_id,measured_at DESC);

CREATE TABLE agent.audit_records (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  subject_ref text,
  action_code text NOT NULL,
  target_type text NOT NULL,
  target_ref text,
  redacted_detail_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(tenant_id,id)
);
CREATE INDEX audit_recent ON agent.audit_records(tenant_id,recorded_at DESC);

-- 0001適用はrunnerがchecksumとversionを transaction内で更新。
INSERT INTO agent.schema_migrations
  (version,checksum_sha256,name,phase,app_min_reader,app_min_writer)
  VALUES (1,:'migration_checksum_sha256','0001_initial','transactional',1,1);
UPDATE agent.schema_control
  SET schema_version=1,min_compatible_reader=1,min_compatible_writer=1,
      changed_at=now() WHERE singleton=true;
COMMIT;
```

**Important implementation note:** `:'migration_checksum_sha256'` はpsql専用の例示で、通常のSQL driverではそのまま実行できない。実装runnerが実際のmigrationファイルのSHA-256を計算し、適用後の別SQL statementにbindしてchecksumを記録する。したがって上記SQLは**そのまま本番投入禁止**であり、テスト可能な設計の提示。物理列もProvisional contractsに従う。上記の`watches_due`はscope列を含まない全体scheduler用の索引だが、アプリによる無条件全scope列挙は許可せずscheduler roleのみのアクセスとする。Cross-tenantの読み出し禁止はCHAT_09のpolicyで実施。

### 5.3 所有権別の対応表 / 意味論の侵食禁止

| Physical table | 正本Owner | 何を**このMDでは決めない**か |
|---|---|---|
| conversations / tasks / runs / domain_events | CHAT_01, CHAT_02 | state enum、Event envelope、Task完了判定、Steer semantics |
| run_checkpoints | CHAT_01, CHAT_02 | context snapshotの内容、safe boundary、replay |
| tool_intents / tool_attempts / approval_records / side_effect_records | CHAT_03 | approval authorization、effect category、Tool retry許可 |
| artifacts / evidence_records | CHAT_01, CHAT_04/05 | Evidence contract、source interpretation、Artifact store adapter |
| watches / watch_observations / notification_records | CHAT_07 | trigger/threshold、失敗観測、通知de-dup期間 |
| browser_session_refs | CHAT_04, CHAT_09 | Page ownership、cookie lifecycle |
| secret_references / audit_records / tenants | CHAT_09 | user model、permission、retention policy |
| outbox_messages / schema_migrations / schema_control | CHAT_10 | 10-02でdispatch contractの最終化 |
| cost_usage_records | CHAT_02, CHAT_10-04 | budget enforcement、価格表、prompt/model tokenカウント |

未列挙のMemory/Skill/Provider-specific recordsは対応契約のレビューを待ち、後続migrationで追加する。`jsonb`を根拠なく作って暫定業務テーブルに流用しない。

## 6. 状態/シーケンス/正常系

### 6.1 Task受付とEvent+Outboxのatomic保存

1. APIはCHAT_09の認証で`tenant_id`を確定し、CHAT_01のSchemaでrequestを検証。
2. `api_idempotency`を同一transaction内で占有。Key既存でrequest hash一致なら既存結果参照。相違なら`409 conflict`。
3. `conversation_id`が指定された場合だけ同tenant/ownerのConversation・origin Threadを照合。Watch/API直起動のConversationなしTaskは合法として`tasks`をinsert。
4. 同一TXでaggregate/tenant counterを採番して`domain_events`を追加。Taskに属さないWatch/Eventにも対応。
5. 対応`outbox_messages`を同一transactionでinsert。
6. commitが成功してからAPIへTask IDを返す。レスポンス途絶時でもKeyで結果を再照会できる。
7. Outbox consumerは別transactionで少なくとも一度dispatchedする。**送信結果が不明ならduplicateを排除できるとは限らない**。

### 6.2 Tool intent → execution → observation

1. Tool Gatewayが認可・予算を確認（CHAT_03正本）。
2. `tool_intents`をinsert（`(tenant_id,run_id,invocation_key)` UNIQUE）。`approval_records`（必要時）+ Event+Outboxをcommit。
3. 承認未完了ならExecutorは未呼び出し。許可後、attempt rowを採番/leaseし、**current attempt token**を確定してcommit。
4. Executor実行。ResultはArtifactを先に耐久保存・hash照合し、`tool_attempts`と`side_effect_records`の結果、`evidence_records`、Event、Outboxをcommit。
5. Crash後はIntent/side-effect ledgerを読み戻す。非冪等操作で外部結果が不明なら`UNKNOWN`のまま再照会/人間確認を行う。**「DBでリトライ可能」から「外部操作もリトライ可能」を導いてはいけない。**

### 6.3 Watch保存 / 取得失敗

`watches`のcondition/schema/TZ/next_dueをCommitし、watch IDを返す。観測は成功/取得失敗を`acquisition_code`で分離し、失敗なら数値ゼロを捏造しない。観測のprovenanceをEvidence参照へ紐づける。trigger成立の場合だけ`notification_records`と`outbox_messages`を同じtransactionで作成。通知が未配達でもWatchとObservationは消さない。

## 7. 実装可能な内部疑似コード / SQLプロトコル

### 7.1 汎用Event序数採番と一貫Snapshot（CHAT_01 / CHAT_08統合）

従来の`tasks.next_event_sequence`とTask専用`task_events`は廃止し、**aggregate別`aggregateSequence`とtenant別`deliveryOffset`** を分離する。両者はCHAT_01 PR #23の正本に一致し、`deliveryOffset`はCHAT_08 PR #17/#8のSSE cursorを支える。Taskに属さないWatch等のイベントも合法。`event_id`はtrusted UUIDv7を生成し、Owner/Actor/Payload SchemaはCHAT_01/09で検証する。

```sql
BEGIN;
-- (1) 対象Domain Mutation（Task例）。0行はStaleRevision、全TX rollback。
UPDATE agent.tasks SET record_version=record_version+1,updated_at=now()
 WHERE tenant_id=$tenant AND id=$task AND record_version=$old_version
 RETURNING record_version;
-- (2) Aggregate counter: first insert then row-locking update (same TX)
INSERT INTO agent.event_aggregate_counters
 (tenant_id,aggregate_type,aggregate_id,next_aggregate_sequence)
 VALUES ($tenant,$kind,$aggregate,1) ON CONFLICT DO NOTHING;
UPDATE agent.event_aggregate_counters
 SET next_aggregate_sequence=next_aggregate_sequence+1
 WHERE tenant_id=$tenant AND aggregate_type=$kind AND aggregate_id=$aggregate
 AND next_aggregate_sequence < 9223372036854775807
 RETURNING next_aggregate_sequence-1 AS aggregate_sequence;
-- (3) Per-tenant committed delivery order (same TX, no external calls while locked)
INSERT INTO agent.tenant_event_counters(tenant_id,next_delivery_offset)
 VALUES($tenant,1) ON CONFLICT DO NOTHING;
UPDATE agent.tenant_event_counters SET next_delivery_offset=next_delivery_offset+1
 WHERE tenant_id=$tenant AND next_delivery_offset < 9223372036854775807
 RETURNING next_delivery_offset-1 AS delivery_offset;
-- (4) INSERT agent.domain_events with both returned counters, validated V1 envelope
-- (5) INSERT agent.outbox_messages referencing same event_id, unique delivery key
COMMIT;
```

`$tenant`等は**説明用の擬似bind変数**でありそのまま実行できるSQLではない。正式実装ではdriver bindと`RETURNING`取得、所有権検証、型/認可/状態チェック、全TX rollback、deadlock時の有限retryを実装する。Lock順: domain resource → aggregate counter → tenant counter → event/outbox。**Write-after-rollbackでEventだけを配信しない。** 逆順Outbox relayはDB replayのoffset昇順で正規化し、遅延/重複はeventIdで排除する。

`GET Task snapshot`はTask/Run/Evidenceの公開projectionとtenant counterのhigh-waterを、**同一 `REPEATABLE READ READ ONLY` Snapshot**で取得。非同期Read Modelを使う場合は`applied_delivery_offset`までのCursorのみ署名。Event間の非認可offsetが飛ぶことは正常で、Cursor欠損と解釈しない。Cursorはtenant/viewer/task filter/expiryでHMAC保護、owner認可を再検証する。実際のEventテーブルに関係するWatch/Tool/ApprovalはTask linkを任意に持ち、Task非関連EventをユーザーTask SSEへ無理に投影しない。

### 7.1.1 Append-only CheckpointのAtomic Head CAS

`run_checkpoints`の各行はimmutable。一つの`step_no`にMODEL, INTENT, EXECUTE, VERIFYなど複数の`checkpoint_revision`を持つ。**正本は`runs.current_checkpoint_id`が指す行だけ**であり、DBへの行追加だけではstep前進しない。

```text
BEGIN
SELECT task FOR UPDATE; verify activeRunId, goalRevision, status/owner
SELECT run FOR UPDATE; verify leaseEpoch=current, lease expiry, expected recordVersion
current=SELECT current_checkpoint_id; verify current.checkpoint_revision==expected
nextRevision=(current? current.checkpoint_revision+1 : 1)
INSERT immutable run_checkpoints (step_no,phase_code,writer_lease_epoch,
 applied_goal_revision,nextRevision,snapshot/hash)
UPDATE runs SET current_checkpoint_id=newId, record_version=record_version+1
 WHERE tenant_id/id/lease_epoch/record_version match current and current_checkpoint_id IS NOT DISTINCT FROM previous
append canonical Event and Outbox in same TX if phase has public committed event
COMMIT
```

古いWorkerが`lease_epoch`変更後にHEADを進めることは不可。復帰直後に古いsnapshot内のwriter_lease_epochが新epochと違うこと自体は正常なので、復旧時はRunの**現在epoch**を認可し、snapshotは適用済みGoal/Hash/Revisionを検証した後、新epochで次revisionを書く。コミット成功直前にArtifactが欠落した場合は破損として中断。Append-only revisionが途中で失敗した場合はROLLBACKし、同じrevisionを安全にリトライ可能（外部副作用の再実行とは独立）。Checkpoint Phase変更とRun/Tool Effectの状態変更は原子的。検証試験DB-R3-T04/05。

### 7.1.2 Tenant Event retention floor / Replay 410の正本

`agent.event_retention_floors.earliest_replayable_offset`はTenantごとの**現在保持している再生可能な範囲の下限**。Task/owner filter後の最小Event offsetや、`domain_events MIN`の有無から勝手に推測しない。初期値1は「まだGCで削除していない」を示す。新しいtenantの初回Eventと同TX、またはGC開始前に`INSERT ... ON CONFLICT DO NOTHING`で行を確保する。

Retention GCは、(1) event holder/backup/cursor契約の保持期限、(2) OutboxのRelay/Downstream重複確認・処理済み、(3) Artifact/監査参照が終了、を満たす**連続した先頭prefix**のみ削除可能。GCは`tenant_event_counters`行をlockしてoffset割当と直列化し、参照中の`outbox_messages`を送信処理完了と重複ウィンドウ期限を照合して先に消去したうえで、旧`domain_events`の連続prefixをdeleteし、**同一TX**で`earliest_replayable_offset`を前進させる。途中の未完了Eventを飛び越して水位だけ上げない。Watermarkの縮小は禁止。GC直後の参照を期待するClientは`410 STREAM_CURSOR_EXPIRED`→GET Snapshotから再開。

```sql
-- In a trusted GC transaction with tenant_event_counters row locked:
UPDATE agent.event_retention_floors
 SET earliest_replayable_offset=$new_floor, updated_at=now()
 WHERE tenant_id=$tenant
   AND earliest_replayable_offset <= $new_floor
   AND $new_floor BETWEEN 1 AND 9223372036854775807;
-- Atomic with deletion of contiguous old domain_events after safe Outbox cleanup.
```

`$new_floor`は正規10進整数をtrusted driverでbindする説明用値。SQLの条件だけではGC安全性を証明しないため、保持期間・DB Lock・Outbox/Artifact referenced rows・証拠参照・監査ポリシーのGuardをCHAT_09/10/11が必ずContract Testする。`DB-R4-T08`: 非認可で隠れたoffsetがあってもfalse 410なし、期限切れcursorは410、pending outboxがあるprefixはGC拒否、Tx kill中も整合したfloorのみ残る。試験設計のみ。正式Cleanup Worker実装は未存在。

### 7.2 Tool Attempt fencing

```text
function startAttempt(intentId, currentState):
  BEGIN
  SELECT intent FOR UPDATE in tenant scope
  check CHAT_03 approval + expiry + budget + cancel boundary
  SELECT max(attempt_no) FROM tool_attempts for this intent
  insert attempt_no = max+1; fence_token = max+1 (same row-lock)
  mark intent status via canonical validator
  COMMIT
  return { attempt_id, attempt_no, fence_token }

function settleAttempt(intentId, attemptId, token, result):
  BEGIN
  SELECT tool_intents FOR UPDATE
  SELECT current latest attempt FOR UPDATE
  if current.id != attemptId OR current.fence_token != token:
      ROLLBACK; return STALE_ATTEMPT (never overwrite newer result)
  validate Owner-required transitions
  persist artifact ref, result, ledger, Event, Outbox
  COMMIT
  return COMMITTED

on crash after executor call but before commit:
  external-effect status UNKNOWN until provider receipt/lookup resolves
  if non-idempotent and no verified result: no automatic re-execution
```

`fence_token`は同一intent内の単調世代。ToolのHTTP呼出自体を事後的に取り消せる保証ではない。Timeout後に旧Workerが外部作用を完了し得るため、復旧後は`side_effect_records`の照会により安全性を判定する。

### 7.3 Idempotency key

```text
create(tenant, operationKey, canonicalPayload):
  h = sha256(canonicalSerialize(canonicalPayload))
  BEGIN
  INSERT key + h ON CONFLICT DO NOTHING
  existing = SELECT ... FOR UPDATE
  if existing.h != h: ROLLBACK; return 409
  if existing durable result exists: COMMIT; return result
  if request still in progress: COMMIT; return 202 + status locator
  otherwise create domain row + durable response pointer + Event + Outbox
  COMMIT
```

`canonicalSerialize`は正本schemaに依存。取り決めなしの`JSON.stringify`はKey順や型で衝突/非一致を起こし得るため禁止。Key namespaceはtenant+operation（CHAT_08/03合意対象）。

### 7.4 上限 / DB pool / retry

初期**設定値の提案（実測目標でなく安全な導入用config）**: API/Worker合計DB pool最大20接続（API 8、runtime worker 8、browser/aux 2、migration 2を上限に環境別調整）、statement timeout通常`10s`、管理migrationは個別設定、`idle_in_transaction_session_timeout 15s`、`lock_timeout 2s`、Tx retries`最大3回`かつ全体deadline遵守。Checkpoint JSON inline上限64 KiB（超過はArtifactへ）。Event JSON inline上限64 KiB、Evidence JSON inline上限16 KiB、watch JSON inline上限16 KiBをadapterで制限し、raw内容はArtifactへ。これらは**暫定の初期ガード**であり、CHAT_02/11の負荷実測で見直す。

`SERIALIZABLE 40001` / `deadlock 40P01`: SQL-only transactionならjitterを入れ最大3回再試行。`23505`（UNIQUE）: key+payload比較してidempotent/409を判別。`23503`（FK）: 参照不整合として失敗。`55P03`（lock timeout）: 状態を変更せず有限backoff。`57014`（cancel/statement_timeout）: commit有無を照会、外部side-effect無条件retry禁止。DB接続が切れcommit応答を失った場合は**結果不確定**としてDB正本を再読込する。

## 8. 障害マトリクス

| 境界 / 障害 | DBに残るもの | 復旧/検出 | ユーザー表示・副作用 |
|---|---|---|---|
| Task insert前にAPI crash | なし | 同Keyで再依頼可 | 未登録。重複作成を防ぐ |
| Task/Outbox transaction COMMIT直後に応答断 | 完全なTask+Event+Outbox | idempotency keyで照会 | 既存Task IDを返す |
| Eventだけ書く途中でcrash | 同Tx rollback | 一貫性監査 | Event/Outboxだけ孤立しない |
| Executor前のcrash | Intentはある、attemptなし/未実行 | policy再評価 | 許可がある場合だけ開始 |
| 非冪等Executor完了後、DB result前crash | Intent + 未確定effect | receipt照会/人間判断 | UNKNOWN、勝手に二度実行しない |
| 旧Workerが新Workerの結果を上書き | 世代違い | fencing UPDATE 0行 | 旧結果は採用しない |
| Watch poll中にサイト取得失敗 | acquisition=failure | 次回監視/診断 | **0円通知は禁止** |
| Notification send後、ack保存前crash | Outbox未完了 | channel idempotency/receipt照会 | duplicate riskを明示、無期限retryなし |
| DB connection切断/commit不明 | 確定またはrollback | Key/Revision照会 | Partial/Uncertain |
| マイグレーション途中/DDLロック超過 | schema_control旧版 | transaction rollback / nonTx checkpoint | 旧版継続または停止、安全側 |
| Artifact upload成功、DB rowなし | orphan object | GC grace period後に監査 | 直ちに削除せず復元可能性を保つ |
| DB rowあるがArtifactが欠ける | FKは通るがStore不整合 | restore audit / hashチェック | 結果不完全、Evidenceを信用しない |

## 9. Security / Privacy / Trust Boundary

- **AgentにDB資格情報、DDL権限、SQL実行ツール、ホストshellを渡さない。** DBへのwrite/readはtrusted API/Worker adapter経由のみ。migration用roleは起動時workerから分離。
- 接続はDocker内部ネットワーク、DBポートはpublicにpublishしない。DBは専用role（API/worker/readonly/migrator）を分離。`search_path`を固定。識別子やschema名を外部入力から組み立てない。
- 全Queryに`tenant_id`フィルタと複合FK。`tenant_id`をクライアント任意指定値から受け取らない。RLS導入前はtrusted adapterと統合テストが不可欠。DB roleを分けても**相互参照自体は認可の代用ではない**。
- URL含有credential、OAuth token、cookie、生HTML、全文プロンプト、credit card等はaudit/notification/event JSONへ保存しない。`safe_arguments_json`と`redacted_detail_json`はCHAT_09のallowlist serializerのみ。
- `secret_handle`は秘密値ではなく不透明な参照。Secret自体の保存、暗号鍵、rotation、アクセス監査はCHAT_09。Browser session cookie原本はBrowser/Secret Store Ownerが保存。
- 外部ページ/SNS由来JSONを`event_type`、`tool_key`、DDL、Queryに昇格しない。生データは非信頼artifactとしてclassification付き保存、LLM実行上限/egressはPolicy経由。
- 大量write、巨大JSON、悪意あるソースのresource exhaustionを制限。対象URLや利用者IDを高カーディナリティのmetric labelにしない。
- `audit_records`の保存期間と削除権限、法令対応はCHAT_09の決定待ち。秘密情報が入ったauditを「後から消せる」ことに依存しない。

## 10. 性能・Trace・観測の設計

- 指標: `db_tx_duration_ms`（operation type別）、`db_pool_wait_ms`、`db_pool_in_use`、`db_deadlocks_total`、`db_serialization_retries_total`、`db_lock_timeout_total`、`migration_duration_ms`、`migration_lock_wait_ms`、`checkpoint_bytes`、`event_rows/task`、`outbox_oldest_age_seconds`、`FK_errors_total`、`artifact_missing_total`。
- Trace相関は`task_id`・`run_id`を**ログ相関フィールド**として保持し、metrics labelは低cardinality値に限定。生prompt/credentialsはTrace属性に入れない。
- Query profile: Task一覧、Run最新Checkpoint、Event stream page、due Watch、Observation最新N件、unpublished Outboxを実際のsample volumeで`EXPLAIN (ANALYZE, BUFFERS)`。本番で`ANALYZE`を乱用しない。
- Benchmark環境: PostgreSQL major/CPU/RAM/SSD、connection、dataset件数/分布/JSON平均サイズ、同時Worker数、WAL増加、p50/p95/p99、Error rate、restore後のQuery consistencyを記録。Index追加はread gainだけでなくwrite overheadとindex sizeを比較。
- **SLO数値は未確定**（SC-09の測定後にCHAT_10-04/CHAT_11と決定）。仮の10s timeout等を性能保証と書かない。

## 11. Migration / Rollout / Rollback

### 11.1 Migration manifest（コード管理）

```ts
type Migration = {
  version: number;                     // strictly increasing
  filename: string;                    // immutable once applied
  sha256: string;                      // hash actual bytes
  phase: "transactional" | "postcommit";
  minCompatibleReader: number;
  minCompatibleWriter: number;
  upSql: string;                       // reviewed static SQL
  verifySql: string;                   // idempotent integrity probe
  downPolicy: "rollback" | "forward-only";
};
// 0001: transactional; 0002: add columns; 0003: dual-read; 0004: backfill batches;
// 0005: CREATE INDEX CONCURRENTLY outside BEGIN; 0006: constraint validation;
// 0007: contract/remove old columns AFTER old readers drained.
```

- **起動時自動任意DDL禁止**。専用migration jobを手動/管理者deployで実行。全workerは`schema_control`の`min_compatible_reader/writer`と自身のschema capabilitiesを起動・定期チェック。互換範囲外ならread/write停止しhealthに`INCOMPATIBLE_SCHEMA`を表示。
- 1つのdeploymentがmigration lock取得。`pg_advisory_lock`等の**セッションロックを専用接続で保持**、接続喪失時はlockを失うことを考慮しphase/checksumをDBで再検査。DB connection pooling経由で無条件にsession lockを使い回さない。
- Transactional migrationは1 transaction内`LOCK_TIMEOUT`設定→既存hash照合→upSQL→verify→schema_migrations insert→schema_control更新→commit。失敗時は丸ごとrollback。既適用版のchecksum相違や欠番はstop。
- Postcommit migration（`CREATE INDEX CONCURRENTLY`等）はDB transaction**外**。`schema_migration_steps`（後続migrationで定義）相当の実行台帳にphase/task/result/hashを管理し、失敗時は`pg_index.indisvalid`等を検査してから再構築。**IF NOT EXISTSだけではInvalid indexを修復できない**。実行前後`pg_get_indexdef`で定義一致を検証。
- Deploy順: 旧/新互換宣言を設計 → expand SQL → old+new dual-read/dual-write rollout → small-batch idempotent backfill（cursor/watermarkの永続化）→ compare/count/false-positive防止 → code switch → old workerが0であること確認 → constraint validation → contract。実Runが残る場合、旧payload/schema readerは必要期間維持する。
- Migration中もAPIにTaskを受け付けるにはそのversionで互換なread/writeが必要。不明ならWriteを停止して`503 maintenance`を返す。**逆向きdownSQLが存在するという理由だけで、破壊的migrationのrollbackが安全だとは判断しない**。
- SchemaもArtifactも一緒にrestoreできるよう、バックアップcheckpoint IDとschema version/Artifact manifestの整合を記録。RPO/RTOはCHAT_10-05で定義。

### 11.2 不変性・削除

Event、Evidence、Audit、Checkpointは原則append-only（訂正は新しい記録/状態change）。Runtimeに一般`UPDATE/DELETE`権限を無条件付与しない。保持期限やPrivacy削除はCHAT_09の契約、バックアップ保持は10-05に従う。参照中ArtifactのGCを禁止し、DB FKを跨いだStore GCは「mark→referenced check→grace→delete→reverify」の別Jobで管理する。Partitionは初期から強制せず、測定で導入を決める（Postgres partitioned tableのUNIQUEはpartition keyを含む必要あり [S17]）。

## 12. UI / HTTP / 他モジュールへの変更提案

- CHAT_08: 作成成功レスポンスはCommit後に返す。Idempotency-Key要求、request hash mismatch `409`、commit不明時の照会URL、Task/WatchのID・保存済みnext_due表示。OutboxはUIイベント配信用の正本に使うがSSEの再送ルールはCHAT_08。
- CHAT_01: Eventに`event_schema_version`、scope/Task/Run参照とTask stream序数、`occurred_at` と `persisted_at` の区別を提案。状態enum/enum migrationの互換性を確定する。
- CHAT_02: `run_checkpoints`と`runs`のrevision/recovery snapshotを使う前提のserialization contract、stale generation rejectを定義する。JS objectをそのままstoreしない。
- CHAT_03: `invocation_key`、`request_sha256`、`effect_key`、`outcome UNKNOWN`、approval identityとeffect reconciliationの正本契約を確定。外部操作のreplay semanticsはDBから推定不可。
- CHAT_04: BrowserSession storage artifactのTTL/owner/cookie secret分離。
- CHAT_07: `observation_key`と`notification_key`の再計算可能な導出規則、受信者スコープ、停止時動作、trigger結合を定義。DBは正しい重複Keyに対するuniqueを実装するのみ。
- CHAT_09: `tenant_id`とactor/user/tenant mapping、Row-level authorization/RLS方針、Secret Handle/Retentionを承認。
- CHAT_11: migration matrixと障害注入test fixtureを作る。番号/Schemaを変更する場合はCross-team proposal→再レビュー。

## 13. 実装フェーズとPR分割案（本仕様repoとは別の実装repo）

1. **PR-DB-A:** Postgres container/dev-test、migration runner、schema_control、tenant scope（CHAT_09 contract確定後）、権限最小化、CI schema test。外部Job/LLMを入れない。
2. **PR-DB-B:** Conversation/Task/Run/Event + revision/atomic outbox + repeatable idempotency + Crash fixtures。CHAT_01/02/08 contractと同時確認。
3. **PR-DB-C:** Tool Intent/Approval/Attempt/Side-effect Ledger + fencing + Artifact/Evidence references。CHAT_03/04 contractと照合。
4. **PR-DB-D:** Watch/Observation/Notification storage、fail≠zero、outbox relay integration。CHAT_07/10-02合同。
5. **PR-DB-E:** BrowserSession/secret refs/usage/audit、バックフィルとmigration expand/contract、EXPLAIN/Privacy/Fault test。CHAT_09/10-04/05/11合同。

この仕様PRはPhase実装を行ったものではない。契約が未決なら各実装PRをmerge不可にする。

## 14. テスト仕様（未実行。すべて設計のみ）

| TEST-ID | Level / Setup | Action | Expected |
|---|---|---|---|
| DB-T001 | Integration / fresh PG container | 0001適用、再適用 | schema+checksum一致、2回目no-op、dirtyなし |
| DB-T002 | Integration / 2 scope | 他scopeのtask_idをrun/evidenceへFK指定 | `23503`、横断参照0件 |
| DB-T003 | Concurrency / 2 connections | 同Taskへ100並列Event append | 序数は1..100一意、欠番/状態不一致なし（失敗transactionは全rollback） |
| DB-T004 | Fault / commit boundary | Task+Event+Outbox間でkill | 0件or3件同時、片方だけ残らない |
| DB-T005 | Contract / API | 同Idempotency-Key同payload再送 | 同じTask ID、追加作成0 |
| DB-T006 | Contract / API | 同Key異なるpayload | 409、旧結果不変 |
| DB-T007 | Fault / Tool | Intent commit前/後でkill | commit前0、後はIntentから再検出 |
| DB-T008 | Concurrency / Tool | attempt-1を停止させattempt-2成功後に旧attempt settle | 更新0行、attempt-2結果保持 |
| DB-T009 | Fault / external effect | mock provider success直後result commit前kill | UNKNOWN、再実行0、receipt照会へ |
| DB-T010 | Integration / Evidence | expired/failed sourceを保存 | observed_at NULL許容、失敗は0円にならない |
| DB-T011 | Integration / Watch | Watch登録後Worker/container restart | Watch同ID、同条件、next_due残存 |
| DB-T012 | Fault / Notification | fake Telegram ack前kill | Outbox残存、idempotency規則で照会/安全な再送 |
| DB-T013 | Security | 生Cookie/token/URL query credentialをsafe JSONへ投入 | scrub/validation reject、DB/Log/Traceに残らない |
| DB-T014 | Security | 任意tenant_id/偽scopeをAPI送信 | 本人のscope以外参照不可 |
| DB-T015 | Migration | 同時に2 migrator起動 | lockで一者のみ、version/hash一致 |
| DB-T016 | Migration | checksum改変済みmigration適用 | 即時abort、table/data変更なし |
| DB-T017 | Migration | CONCURRENT index途中kill | invalid index検知、検査なし成功扱いしない |
| DB-T018 | Migration | old/new app並走・checkpointあり | old/new両方read、既存Run保持、contract延期 |
| DB-T019 | Recovery | DB restore後Artifact Store整合を検査 | missing/hash mismatchをerror分類、誤成功0 |
| DB-T020 | Benchmark | seeded 10k Task/1m Event/1m Observation等、環境固定 | EXPLAIN+WAL+latency report、閾値の根拠を保存（数値合格は未決） |
| DB-T021 | Security / injection | Web HTML内の`DROP TABLE`指示を入力 | DDL呼出なし、権限変更なし、文字列SQL injection不成立 |
| DB-T022 | Load / limit | JSON size>設定上限、DB Pool枯渇、1000並列requests | size reject/artifact offload、bounded timeout、接続上限超過なし |
| DB-T023 | Consistency | 正本のRun/Tool/Approval各state transition fixture | 不正遷移がadapterで拒否、status_codeとrevision対応 |
| DB-T024 | Retention | Artifact参照中GC + backup cutoff | 参照生存中削除0、再開可能 |
| DB-T025 | DDL | PostgreSQL採用majorでSQLを`psql -v ON_ERROR_STOP=1`実行 | 型/FK/Indexが実際に全作成され、migration runnerと一致 |

**実際に実施した試験: 0件。** 既存OSS側のテストファイルを**読んだ**だけで、このprojectのDB実装/テストは存在しない。DB-T025未実行のため初期DDLは**検証前の提案**。

## 15. REQ → TEST → SC 追跡

| REQ | TEST | SC | 合格判定 |
|---|---|---|---|
| DB-001 | T001,T011,T019 | 02,07 | Restart後同一IDと永続値 |
| DB-002 | T002,T014 | 06,08 | scope横断0件 |
| DB-003 | T023,T025 | 01,02,07 | Canonical schemaをstorage adapterがvalidate |
| DB-004 | T003,T004 | 07 | Event/Outbox atomic, sequence unique |
| DB-005 | T007,T009 | 07,08 | Intentなし外部実行0 |
| DB-006 | T005,T006,T012 | 02,07 | 重複/衝突を検出 |
| DB-007 | T008,T009 | 07 | stale settle 0 |
| DB-008 | T010,T019 | 01〜05 | Provenance/失敗正確 |
| DB-009 | T013,T021 | 06,08 | Sensitive rawなし |
| DB-010 | T011,T023 | 02 | Watch restart consistency |
| DB-011 | T004,T012 | 02,07 | lost dispatch discoverable |
| DB-012 | T001,T015,T016,T025 | 07 | hash/lock/version |
| DB-013 | T017,T018,T024 | 07 | safe expand/contract |
| DB-014 | T020 | 09 | profile+WAL reportあり |
| DB-015 | T020,T022 | 09 | budget violation検出 |
| DB-016 | T013,T014,T021 | 08 | secrets absent/tenant safe |
| DB-017 | T009,T012 | 02,07 | UNKNOWN保持、盲目再送なし |
| DB-018 | T019,T024 | 07 | integrity audit可 |

`T###`は本節の`DB-T###`を指す。SC参照は`docs/00-foundation/acceptance-scenarios.md`に対応。

## 16. 未確定事項 / Cross-team proposals（Draft PRで要審議）

| ID / Owner | 優先度 | 提案/未決 | 決定条件 |
|---|---|---|---|
| DB-X01 CHAT_01 | **P0** | Canonical ID型、Event envelope/sequence scope、Task/Run status/version、Error型。`event_json`保存形の重複を解消する必要 | 契約MDのReviewed/Lockedとstorage mapping review |
| DB-X02 CHAT_09 | **P0** | tenant / tenant / user / actorの対応、row authorization、secret redaction、retention、auditアクセス | Cross-scope security testsとpolicy review |
| DB-X03 CHAT_03 | **P0** | Tool Intent/Approval/Effect/attemptとUNKNOWN・idempotencyのcanonical schema | side-effect ledger accepted + kill-window E2E |
| DB-X04 CHAT_02 | **P0** | Checkpoint形式、snapshot schema version、recovery/steer/cancel safe boundary | crash replay fixturesで既存Run可読 |
| DB-X05 CHAT_07 | **P1** | observation_key、trigger identity、Watch停止/周期/TZ、notification_keyの衝突規則 | reboot/double notification fixture合格 |
| DB-X06 CHAT_10-02 | **P1** | pg-boss採否、Outbox relay lease/fencing/ack/consumer group、job retention | at-least-once kill/duplicate test |
| DB-X07 CHAT_10-05 | **P1** | DB+Artifact+Secret snapshot整合、WAL retention、RPO/RTO | restore drill |
| DB-X08 CHAT_11 | **P1** | PG16/17/18の採用majorとtest matrix、query workload/閾値 | executable DDL+EXPLAIN+load |
| DB-X09 CHAT_00 | P1 | 個人利用起点で将来multi-user前提を物理モデルへ固定してよいか、Artifact store採用 | Architecture ADR |
| DB-X10 CHAT_01/09 | P1 | SQLのpayload retention/各JSON canonical schema/許容サイズ | Unit validation + privacy audit |
| DB-X11 CHAT_08 | P1 | Idempotency-Keyのnamespace, 409/202, SSE cursorとの対応 | API Contract + reconnect test |

**P0が残る間は本PRをDraftのまま維持。** 上記のProvisional列は任意にLocked扱いせず、Ownerの契約決定後にSQLとREQ/TESTを改訂する。

## 17. Sources（確認方法・範囲・Evidence grade）

すべて2026-10-30 JST調査。URLのbranch参照は**調査時点のGit commit**と組で追跡し、後日の上流更新を自動的に既検証とは扱わない。

- [S1] `https://github.com/timgit/pg-boss/blob/71ea068b144c75b014a1eb90327aaa4a92da1cf2/package.json` — `12.38.0`, Node >=22.12, MIT; **Code verified**（package内容）。
- [S2] `https://github.com/timgit/pg-boss/blob/71ea068b144c75b014a1eb90327aaa4a92da1cf2/src/plans.ts` — `create()`, job table/indices/functions, lines around 174-220; **Code verified**。
- [S3] `https://github.com/timgit/pg-boss/blob/71ea068b144c75b014a1eb90327aaa4a92da1cf2/src/migrationStore.ts` — `MigrationCommands`、`flatten()`, `rollback()`, inlineAsync; **Code verified**。
- [S4] `https://github.com/timgit/pg-boss/blob/71ea068b144c75b014a1eb90327aaa4a92da1cf2/test/attemptFenceTest.ts` — stale worker attempt takeover, complete/fail/heartbeat tests; `test/migrationTest.ts` migration assertions; **Code verified**。
- [S5] `https://github.com/timgit/pg-boss/blob/71ea068b144c75b014a1eb90327aaa4a92da1cf2/LICENSE` — MIT; **Docs verified**。
- [S6] `https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/checkpoint-postgres/pyproject.toml` — `3.2.0`, PostgreSQL psycopg dependencies; **Code verified**。
- [S7] `https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/checkpoint-postgres/langgraph/checkpoint/postgres/base.py` — `MIGRATIONS`、`UPSERT_CHECKPOINTS_SQL`、`UPSERT_CHECKPOINT_WRITES_SQL`; **Code verified**。
- [S8] `https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/checkpoint-postgres/langgraph/checkpoint/postgres/__init__.py` — `PostgresSaver.setup()` version iteration; **Code verified**。
- [S9] `https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/checkpoint-postgres/tests/test_sync.py` — migration nonnull/pending sends tests; **Code verified**。
- [S10] `https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/checkpoint-postgres/LICENSE` — MIT; **Docs verified**。
- [S11] `https://github.com/temporalio/temporal/blob/3eb5fb80523443d8583bcec463843275908f1bbc/schema/postgresql/v12/temporal/schema.sql` — workflow execution/current/history/task physical tables; **Code verified**。
- [S12] `https://github.com/temporalio/temporal/blob/3eb5fb80523443d8583bcec463843275908f1bbc/schema/postgresql/v12/temporal/versioned/v1.19/current_chasm_executions.sql` — physical PK and table addition; **Code verified**。
- [S13] `https://github.com/temporalio/temporal/blob/3eb5fb80523443d8583bcec463843275908f1bbc/schema/postgresql/v12/temporal/versioned/v1.19/manifest.json` — `CurrVersion=1.19`, `MinCompatibleVersion=1.0`; **Code verified**。
- [S14] `https://github.com/temporalio/temporal/blob/3eb5fb80523443d8583bcec463843275908f1bbc/LICENSE` — MIT; **Docs verified**。
- [S15] `https://www.postgresql.org/docs/18/indexes-unique.html` — Composite UNIQUE / NULLS DISTINCT; **Docs verified** (PG18)。
- [S16] `https://www.postgresql.org/docs/18/mvcc-intro.html` — MVCC and transaction isolation overview; **Docs verified** (PG18)。
- [S17] `https://www.postgresql.org/docs/18/ddl-partitioning.html` — partitioned UNIQUE key/CONCURRENT index limitation; **Docs verified** (PG18)。
- [S18] `https://www.postgresql.org/docs/18/sql-createindex.html` — CREATE INDEX CONCURRENTLY and constraints; **Docs verified** (PG18)。
- [S19] `https://github.com/choko510/aiagent-md/blob/92b5827ed16a7d4ae61e8146ce382795398293cb/docs/00-foundation/project-brief.md` + `architecture-rules.md` + `acceptance-scenarios.md` — product/owners/scenarios; **Docs verified**。
- [S20] `https://github.com/choko510/aiagent-md/blob/92b5827ed16a7d4ae61e8146ce382795398293cb/prompts/CHAT_10.md`, `prompts/COMMON.md`, `docs/00-foundation/review-gates.md` — deliverable/review protocol; **Docs verified**。

**Evidence boundary:** source/テストファイルは実際に閲覧したが、上流OSSのテストをこの環境で再実行したわけではない。提案SQLをPostgresで実行したわけでもない。DBの外部副作用 exactly-once、SLO、性能値、互換性は未検証。コード未確認の関数名を事実として追加しない。

## 18. Round 2 契約相違台帳・修正指示（2026-10-30）

**調査対象:** PR #1 `agent-loop.md`、#2 `core-domain-models.md`、#8 `rest-api.md`、#9 `authentication-and-user-isolation.md`、#10 本MD、#11 `scheduler-and-recurring-tasks.md`（全てDraft）。Logical ownerが未Lockedのため全項目は**[PROVISIONAL]**。

| ID/重大度 | 検証した相違 | Owner・採用提案 | 物理差分・対象テスト |
|---|---|---|---|
| CC10-01 P0 | PR #2は公開IDをserver-issued UUIDv7、旧PR #10はDB `gen_random_uuid()`（PG16/17でv4） | CHAT_01がIdentity正本。DB側UUID defaultを削除しtrusted serverがUUIDv7を生成/validate。DB `uuid`型だけではversion検証不可 | 全ID列、DB-T026: v7成功、v4 validator拒否 |
| CC10-02 P0 | PR #2 Task.conversationId nullable、PR #8 TaskView required、旧DDL NOT NULL | CHAT_01論理契約を採用、CHAT_08へnullable REST projection提案 | `tasks.conversation_id NULL`、同ConversationのみoriginThread可能。DB-T027 |
| CC10-03 P0 | PR #2 recordVersionは初期1、旧DDL revision=0 | CHAT_01 CAS正本。物理は`record_version bigint default 1`とsigned BIGINT上限。Task goalRevision、Watch definitionRevision、Run leaseEpochと分離 | `tasks/runs/conversations/tool_intents/watches`、DB-T028 |
| CC10-04 P0 | PR #2 Run.leaseEpoch number nonnegative、PR #1 runtime uses generation、旧DDL Run lease欠落 | CHAT_01/02正本。 `runs.lease_epoch bigint default 0`、leaseOwner/Expiry保存。同Run resume時 epoch++、current Task.activeRunの一致をCAS。DB bigint→JS numberはlossless bigint/string codec | `runs`+Checkpoint repository、DB-T029 |
| CC10-05 P0 | PR #2 Task.goalRevision, activeRunId, Run.inputGoalRevisionが旧DDLにない | CHAT_01/02。 `task_goal_revisions` immutable、Task goal_revision/active_run_id、Run input_goal_revision、TaskのactiveRun複合FK。Steer新goalとoutbox同一Tx | `tasks/runs/checkpoints`、DB-T030 |
| CC10-06 P0 | PR #2と#9はTenantIdとownerUserId、旧DDLはworkspaceのみ | CHAT_01 identity、CHAT_09 authorization。物理scopeは`tenant_id`へ統一し`tenant_memberships`とowner_user_id複合FK。**membershipだけでは別ownerにアクセス不可**。現行のworkspace独立概念は撤回 | 全tenant付き表、DB-T031 |
| CC10-07 P0 | PR #8 API idempotencyはtenant+principal+endpoint+key、旧DBはtenant+operationKeyのみ | CHAT_08 HTTP契約、CHAT_03 tool。 `api_idempotency`複合PKにactor/operation/keyとresponse status、request hash。Task+Event+Outbox+receipt同Tx | DB-T032 |
| CC10-08 P0 | PR #1/2はexternal effect UNKNOWNの再実行禁止。Tool attempt fenceとRun leaseは別 | CHAT_03 Tool Ledger正本。DB stale settlementは0行、外部実行が済んだかはreceipt照会なしに断言しない | `tool_intents/side_effect_records` DB-T033 |
| CC10-09 P1 | PR #11 scheduler definition revisionとSC-10..16追加 | CHAT_07 scheduler、CHAT_12..15新機能owner。 `watches.definition_revision`とTask/Record/Leaseを分離 | DB-T034、SC-10..16の回帰 |

### 18.1 Claim/CASの要求SQL（Transaction内のみ、DB上に論理状態enumを新設しない）

```sql
-- taskを先にFOR UPDATEして同taskのRun claimだけを直列化。
SELECT active_run_id,record_version,goal_revision FROM agent.tasks
 WHERE tenant_id=$1 AND id=$2 FOR UPDATE;
-- Resume (旧lease_epoch=$4、active_run_id=当該Run、状態検証済み)
UPDATE agent.runs SET lease_epoch=lease_epoch+1,
 lease_owner_ref=$3,lease_expires_at=$5,record_version=record_version+1
 WHERE tenant_id=$1 AND id=$2 AND lease_epoch=$4
   AND $4 BETWEEN 0 AND 9223372036854775806
 RETURNING lease_epoch;
-- 0行 => STALE_OWNER。checkpoint/ToolResult/new effectは絶対にcommitしない。
```

上記resumeの識別子は`$2=runId`、先行Task SELECTの`$2`はtaskIdとして**別SQL statementでbindし直す**。実装では同一変数名を流用しない。Claim時に同tenantかつTask.activeRunId=Run.idを検証。fresh RunならTask.activeRunId NULLを検証→新Run attemptNo→Task.activeRunIdを同Tx更新。checkpoint/side effect ledgerに入る前もRun lease+Task activeRun+最新GoalRevisionを再検証する。DB fencingは外部HTTPの取り消しを保証しない。

- AppのID validatorはUUIDv7を強制しDBの型/uniqueは最後の防御。 `record_version` と `lease_epoch` はPostgreSQL bigint、wireではJS 正規10進文字列（最大9223372036854775807、内部BigInt）に限定。pg driverのint8 stringを無検証Number化しない。必ず正規10進文字列として検証しBigIntで演算。
- `task_goal_revisions`をappend-onlyで保存。SteerでTask.goal_revisionを上げるTxは新revision行とEvent/Outboxを含める。既存Run.input_goal_revisionは変更しない。旧GoalRevision承認はCHAT_03 gatewayで不許可。
- `tenants`は認可の単位でもUserそのものではない。アプリAPIでprincipalからtenant/userを復元し、全SELECTとSSE等でowner照合。将来RLSはCHAT_09指示で防御層として追加。
- migration runnerは `:'migration_checksum_sha256'` を実ファイルのSHA256（psql変数）に置換。Driverのprepared SQLなら別statementへ切り出す。**SQL例はDB-T025実行前なので未検証**。循環hash回避のためrunner側でhashを記録する方式を最終採用する。
- 他Ownerに必要な変更は本PR本文にCross-team proposalとして提出し、採否が決まるまでLocked不可。

### 18.2 追加テスト（全件設計のみ／実行0）

| TEST | Setup / action / expected | SC |
|---|---|---|
| DB-T026 | trusted generator v7 / v4注入 → UUIDv7 validatorはv7のみ許可、全保存経路でdefault v4に戻らない | 07,08 |
| DB-T027 | Conversation NULLのWatch Task作成成功、originThreadだけ指定はvalidation/FKで拒否 | 02,07,11 |
| DB-T028 | 新規recordVersion=1、CAS +1、旧If-Match 412、signed BIGINT overflow拒否 | 07,08 |
| DB-T029 | 2 WorkersがRun resume、旧epochのCheckpoint/Result更新0、generation単調 | 07 |
| DB-T030 | Steerとcheckpoint並行、old goal適用なし、他TaskのactiveRun FK拒否 | 01,04,07 |
| DB-T031 | 同tenantの別ownerと別tenantの全resourceのread/list/stream拒否 | 06,08 |
| DB-T032 | API same key/same body replay→同Task、異body→409、他actorは独立receipt | 02,07 |
| DB-T033 | external action ACK lost→UNKNOWN保持、blind retry0、照会か承認待ち | 04,07 |
| DB-T034 | paused Watch旧definition revision occurrence→Tool開始0 | 02,11,16 |

**未確定P0:** Task/Run status enumとEvent envelope（CHAT_01）、Steer safe boundary（CHAT_02）、Tool Ledger（CHAT_03）、principal/owner SQL authorization（CHAT_09）。SC-10..16の新設物理表は当該Ownerの設計提案待ち。DB-T025 SQL実行は未実施。


## 19. Round 2 category integration (Job/Observability/Recovery)

This is the **CHAT_10-owned physical integration** with the new module specifications. Reviewer should treat all as provisional pending canonical contracts and DDL tests.

- **JQ-10-02 (PR #22):** Added `outbox_messages.relay_lease_epoch`, `relay_owner_ref`, `relay_lease_until`, `last_error_code`. The relay may mark a row dispatched only after a confirmed queue write and on successful lease epoch CAS. If publish response is lost it retries the *pointer*, never the external action. The extra lease is not Run.leaseEpoch. DB-T035: kill/claim with two relays, stale ack must affect 0 rows; queued logical action applies once.
- **OBS-10-04 (PR #36):** Changed `cost_usage_records.input_units/output_units` to nullable, no default 0: **unknown !== observed zero**. Added optional cache read/write, reasoning, usage kind, provider attempt/logical call, source hash, pricing version, currency/amount minor units and scale. `usage_key` is the stable dedup key, but canonical generation/charge/reconciliation is CHAT_02's decision. Backfill for existing rows with default 0 MUST NOT claim the amount is measured; migration should preserve `usage_kind=estimated` and unknownness, or quarantine ambiguous old records. DB-T036: unknown usage inputs remain NULL, received 0 is explicit 0, conflicting provider receipt not double-billed.
- **REC-10-05 (PR #45):** Backup and recovery controller requests physical `backup_restore_points` and `recovery_gates`; these are **operator-only future migration** because recovery incarnation and operator authority remain P0 with CHAT_01/02/09. Do not put restore control into AI Tool registry. A backup snapshot that rewinds Run leaseEpoch does not alone fence a pre-restore worker. DB-T037: restoration with still-live old Worker must remain quarantined.
- **Migration safety:** All modifications to a previously deployed base schema must be separately numbered additive migrations; the revised 0001 sketch here is only the clean-install target, NOT an instruction to mutate an already-applied migration hash. Existing production deployments require expand/dual-read/backfill/contract. No project SQL has yet been executed.
- **Review request:** CHAT_01 decides recordVersion/CAS, CHAT_02 budget usage, CHAT_03 side effects, CHAT_07 notifications, CHAT_09 owner and secrets, CHAT_11 DDL/restore fixture and benchmarks. P0 unresolved keeps Draft.

## 20. Round 3 contract unification — Draft / NOT TESTED

- P0 Event storage: `agent.domain_events`（Task非必須）、`event_aggregate_counters`、`tenant_event_counters`、outbox FK、Task/Watch回放インデックスを導入。契約OwnerはCHAT_01 PR #23とCHAT_08 PR #17。
- P1 Checkpoint: 同stepの複数保存を許すappend-only `checkpoint_revision` + `runs.current_checkpoint_id`を導入。Writerの`lease_epoch`と`record_version`をCASで確認し、古いownerは書込0。詳細の復旧はCHAT_02 PR #27に整合。
- スキーマ自体のSQL実行は未検証（本Repoは仕様書のみ）。循環FK・BindとRace・Migrationを採用予定PostgreSQLで実行検証すること。

| New test | Action / expected |
|---|---|
| DB-R3-T01 | Task + Watch等1000並列Event: 同tenant offset一意、per aggregate seq一意、Task NULL Watch Event可 |
| DB-R3-T02 | Domain/Counter/Event/Outboxの各TX killpoint: all-or-none、offset rollback |
| DB-R3-T03 | GET SnapshotとSSE開始間にappend: 認可可視Eventの欠落0、他User漏洩0 |
| DB-R3-T04 | 同Step MODEL/INTENT/EXECUTE等5相のCheckpoint: 5 revision保存、head最後、再実行0 |
| DB-R3-T05 | 2 Worker fence交代後に旧WorkerがCheckpoint書込: 0 row update, 最新head保持 |
| DB-R3-T06 | Retention cutoff/署名cursor期限切れ: 410再Snapshot、重複適用0 |

Cross-team proposals: CHAT_01が物理Eventの`schema_version`とactor/audience codecを確認、CHAT_02がRun checkpoint pointer CAS順序を確認、CHAT_08がsnapshot barrier、CHAT_09がTask外Event owner-scope、CHAT_11が以上のContract/Faultテストを追加。これらの承認まで**Lockedではない**。

### 20.1 Round 3 int64 contract finalization (pending owner review)

CHAT_01 `core-domain-models.md`のCanonical整数は、`recordVersion`, `goalRevision`, `leaseEpoch`ほかを**signed PostgreSQL BIGINT 0..9223372036854775807の正規十進文字列**として表現する。上記DDLの旧`Number.MAX_SAFE_INTEGER`上限をBIGINT full rangeへ訂正。内部は`bigint`、wireは`string`、PostgreSQLは`bigint`で、JavaScript`Number`へ変換しない。`0`許容はleaseEpoch等の明示された初期値に限定、recordVersion/goalRevision/checkpointRevision/aggregateSequence/deliveryOffsetは正の値。SQL incrementは必ずMAX未満を前置条件とする（またはoverflowをTX全体rollbackして明示のExhaustedエラー）。`uint64`やfloatを用いない。

`DB-R3-T07`（テスト設計のみ）：version/epochを2^53-1、2^53、BIGINT_MAX-1でwrite/read/ETag/CASし全く同じ文字列に復帰、JSON numberはreject、BIGINT_MAX+1はTX rollback、旧workerのCASは失敗。正式実装のUnit/DB Integration未実行。

### 20.2 Round 4 — Migration 0002 Proposal: External Events / Entity / Digest / Parser (2026-10-30)

**Separate forward-only migration proposal** for features CHAT_12–15. **Do not edit already applied 0001.** This SQL intentionally provides independent physical records, retaining the canonical business ownership at CHAT_01/06/07/13; it is a reviewable schema, NOT a claim of application-level enforcement or executed migration. Test this against 0001 on PostgreSQL 16 and add checksum/backfill/rolling read/write gate before implementation.

```sql
BEGIN;
-- One durable subscription bound to a stable actor identity and explicit owner.
CREATE TABLE agent.external_event_subscriptions (
 tenant_id uuid NOT NULL, id uuid NOT NULL, owner_user_id uuid NOT NULL,
 provider_code text NOT NULL, provider_actor_native_id text NOT NULL,
 connector_credential_ref text NOT NULL, transport_code text NOT NULL,
 generation bigint NOT NULL DEFAULT 1 CHECK(generation BETWEEN 1 AND 9223372036854775807),
 record_version bigint NOT NULL DEFAULT 1 CHECK(record_version BETWEEN 1 AND 9223372036854775807),
 status_code text NOT NULL CHECK(status_code IN ('draft','active','paused','blocked','revoked')),
 filters_schema_version text NOT NULL, filters_json jsonb NOT NULL CHECK(jsonb_typeof(filters_json)='object'),
 policy_json jsonb NOT NULL CHECK(jsonb_typeof(policy_json)='object'),
 consent_ref text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,id),
 UNIQUE(tenant_id,id,generation),
 FOREIGN KEY(tenant_id,owner_user_id) REFERENCES agent.tenant_memberships(tenant_id,user_id)
);
-- Immutable generation records keep old receipts valid when current subscription generation increments.
CREATE TABLE agent.external_subscription_generations (
 tenant_id uuid NOT NULL, subscription_id uuid NOT NULL,
 generation bigint NOT NULL CHECK(generation BETWEEN 1 AND 9223372036854775807),
 created_at timestamptz NOT NULL DEFAULT now(), transition_reason text NOT NULL,
 PRIMARY KEY(tenant_id,subscription_id,generation),
 FOREIGN KEY(tenant_id,subscription_id)
 REFERENCES agent.external_event_subscriptions(tenant_id,id)
);
CREATE TABLE agent.external_event_cursors (
 tenant_id uuid NOT NULL, subscription_id uuid NOT NULL,
 generation bigint NOT NULL,
 since_native_id text, next_token_ref text, coverage_code text NOT NULL,
 last_complete_poll_at timestamptz, gap_start_at timestamptz,
 row_version bigint NOT NULL DEFAULT 1 CHECK(row_version>0),
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,subscription_id,generation),
 FOREIGN KEY(tenant_id,subscription_id,generation)
 REFERENCES agent.external_subscription_generations(tenant_id,subscription_id,generation)
);
CREATE TABLE agent.external_event_receipts (
 tenant_id uuid NOT NULL, id uuid NOT NULL, subscription_id uuid NOT NULL,
 subscription_generation bigint NOT NULL,
 provider_code text NOT NULL, native_event_kind text NOT NULL,
 native_event_id text NOT NULL, actor_native_id text NOT NULL,
 source_created_at timestamptz, received_at timestamptz NOT NULL DEFAULT now(),
 source_sha256 char(64) NOT NULL CHECK(source_sha256 ~ '^[0-9a-f]{64}$'),
 payload_artifact_id uuid, disposition_code text NOT NULL CHECK
 (disposition_code IN ('accepted','duplicate','ignored','blocked','quarantined','unknown')),
 task_id uuid, domain_event_id uuid,
 PRIMARY KEY(tenant_id,id),
 UNIQUE(tenant_id,subscription_id,provider_code,native_event_kind,native_event_id),
 FOREIGN KEY(tenant_id,subscription_id,subscription_generation)
 REFERENCES agent.external_subscription_generations(tenant_id,subscription_id,generation),
 FOREIGN KEY(tenant_id,payload_artifact_id) REFERENCES agent.artifacts(tenant_id,id),
 FOREIGN KEY(tenant_id,task_id) REFERENCES agent.tasks(tenant_id,id),
 FOREIGN KEY(tenant_id,domain_event_id) REFERENCES agent.domain_events(tenant_id,id)
);
CREATE INDEX external_receipts_by_subscription ON agent.external_event_receipts
 (tenant_id,subscription_id,received_at DESC);
-- Storefront: physical seller/account/branch, distinct from corporate organization.
CREATE TABLE agent.canonical_entities (
 tenant_id uuid NOT NULL, id uuid NOT NULL, owner_user_id uuid NOT NULL,
 entity_kind text NOT NULL CHECK(entity_kind IN
 ('product','hotel_property','hotel_rate','organization','retailer_storefront','public_person')),
 mapping_revision bigint NOT NULL DEFAULT 1 CHECK(mapping_revision>0),
 status_code text NOT NULL CHECK(status_code IN ('active','review','split','retired')),
 attributes_json jsonb NOT NULL CHECK(jsonb_typeof(attributes_json)='object'),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id,owner_user_id) REFERENCES agent.tenant_memberships(tenant_id,user_id)
);
CREATE TABLE agent.entity_source_links (
 tenant_id uuid NOT NULL, id uuid NOT NULL, canonical_entity_id uuid NOT NULL,
 source_id text NOT NULL, source_listing_id text NOT NULL, extraction_revision text NOT NULL,
 relation_code text NOT NULL CHECK(relation_code IN ('accepted','rejected','review')),
 mapping_revision bigint NOT NULL CHECK(mapping_revision>0),
 evidence_id uuid, created_at timestamptz NOT NULL DEFAULT now(),
 invalidated_at timestamptz,
 PRIMARY KEY(tenant_id,id),
 UNIQUE(tenant_id,source_id,source_listing_id,extraction_revision,mapping_revision),
 FOREIGN KEY(tenant_id,canonical_entity_id) REFERENCES agent.canonical_entities(tenant_id,id),
 FOREIGN KEY(tenant_id,evidence_id) REFERENCES agent.evidence_records(tenant_id,id)
);
CREATE INDEX entity_links_active ON agent.entity_source_links
 (tenant_id,canonical_entity_id,mapping_revision) WHERE invalidated_at IS NULL;
-- Ranker never sends: only the canonical CHAT_07 delivery sender sends a digest.
CREATE TABLE agent.notification_digest_envelopes (
 tenant_id uuid NOT NULL, id uuid NOT NULL, owner_user_id uuid NOT NULL,
 destination_ref text NOT NULL, policy_revision text NOT NULL,
 generation bigint NOT NULL DEFAULT 1 CHECK(generation>0),
 send_state_code text NOT NULL CHECK(send_state_code IN
 ('draft','reserved','queued','sending','provider_accepted','unknown','failed','cancelled')),
 outbox_id uuid, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id,owner_user_id) REFERENCES agent.tenant_memberships(tenant_id,user_id),
 FOREIGN KEY(tenant_id,outbox_id) REFERENCES agent.outbox_messages(tenant_id,id)
);
CREATE TABLE agent.notification_digest_members (
 tenant_id uuid NOT NULL, digest_id uuid NOT NULL, notification_id uuid NOT NULL,
 member_state_code text NOT NULL CHECK(member_state_code IN
 ('reserved','covered_accepted','coverage_unknown','released','blocked')),
 rank_decision_ref text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,digest_id,notification_id),
 UNIQUE(tenant_id,notification_id),
 FOREIGN KEY(tenant_id,digest_id) REFERENCES agent.notification_digest_envelopes(tenant_id,id),
 FOREIGN KEY(tenant_id,notification_id) REFERENCES agent.notification_records(tenant_id,id)
);
-- Source bytes and derived artifacts must retain provenance and bounded parser budgets.
CREATE TABLE agent.parser_jobs (
 tenant_id uuid NOT NULL, id uuid NOT NULL, owner_user_id uuid NOT NULL,
 source_artifact_id uuid NOT NULL, derived_artifact_id uuid,
 parser_build_digest text NOT NULL, job_state_code text NOT NULL CHECK
 (job_state_code IN ('queued','running','partial','verified','quarantined','failed','cancelled')),
 lease_epoch bigint NOT NULL DEFAULT 0 CHECK(lease_epoch>=0),
 job_revision bigint NOT NULL DEFAULT 1 CHECK(job_revision>0),
 budget_json jsonb NOT NULL CHECK(jsonb_typeof(budget_json)='object'),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id,owner_user_id) REFERENCES agent.tenant_memberships(tenant_id,user_id),
 FOREIGN KEY(tenant_id,source_artifact_id) REFERENCES agent.artifacts(tenant_id,id),
 FOREIGN KEY(tenant_id,derived_artifact_id) REFERENCES agent.artifacts(tenant_id,id),
 CHECK(derived_artifact_id IS NULL OR derived_artifact_id <> source_artifact_id)
);
CREATE TABLE agent.artifact_derivations (
 tenant_id uuid NOT NULL, id uuid NOT NULL,
 source_artifact_id uuid NOT NULL, derived_artifact_id uuid NOT NULL,
 parser_job_id uuid NOT NULL, transform_version text NOT NULL,
 locator_schema_version text NOT NULL, locator_json jsonb NOT NULL CHECK(jsonb_typeof(locator_json)='object'),
 source_sha256 char(64) NOT NULL CHECK(source_sha256 ~ '^[0-9a-f]{64}$'),
 derived_sha256 char(64) NOT NULL CHECK(derived_sha256 ~ '^[0-9a-f]{64}$'),
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id,source_artifact_id) REFERENCES agent.artifacts(tenant_id,id),
 FOREIGN KEY(tenant_id,derived_artifact_id) REFERENCES agent.artifacts(tenant_id,id),
 FOREIGN KEY(tenant_id,parser_job_id) REFERENCES agent.parser_jobs(tenant_id,id),
 UNIQUE(tenant_id,parser_job_id,source_artifact_id,derived_artifact_id)
);
COMMIT;
```

**Mandatory application transaction contracts (not fulfilled by FK alone):**
1. X: lock Subscription row → check `active` & generation & consent → receipt `INSERT ... ON CONFLICT` for stable provider ID across stream/poll → if new and allowed, atomically create Task + DomainEvent + Outbox and mark receipt taskId; advance cursor only for complete upstream coverage, same TX. A disabled/revoked generation prevents late messages and queued dispatch. **Generation changes require explicit policy for old receipts; don't silently alter existing generation FK while active.**
2. Entity: `organization` is a chain/company; `retailer_storefront` is a specific branch/channel/merchant seller identity. No automatic parent-to-branch identity equivalence. `Feedback.class=retailer` is legacy alias resolved to exact storefront only after tenant/owner/mappingRevision check. If ambiguous → REVIEW with no exclusion. Splits/undo create new mapping revision and invalidate dependent Radar/Feedback caches atomically.
3. Digest: ranking only reserves member rows, sender is exclusively CHAT_07. Verify consent/quiet/identity and lock in deterministic order before send. Digest provider acceptance results `covered_accepted` (not confirmed delivered/read); ACK lost→`coverage_unknown`, original individual notifications remain blocked pending reconciliation. Cancellation may release only provably not-sent members; no automatic duplicate send on UNKNOWN.
4. Parser: content is untrusted. Raw artifact SHA, parser build digest, resource limits and owner permission must be validated before no-network parser execution. All derived data points retain source SHA + page/region/cell locator and parent link. Lease/cancel/retention/shredding verified before artifact commit; no macro/formula/script execution.

**Rollout:** `0002_expand` deployed before adapters; enable feature gates only after DB tests + migrations and full Tool/Browser/Parser egress tests; `0002_contract` later only after data backfill/reconciliation. Tests: `DB-R4-E01..05` subscription dedup/revoke/cursor; `DB-R4-ENT01..04` store identity/invalidated mapping; `DB-R4-D01..05` digest coverage and ACK loss; `DB-R4-P01..05` artifact hash/locator/quarantine/cancel. **NOT RUN against an implemented Agent Harness.**


### 20.3 Round 4 — Generation update conflict discovered during FK review
Initially the v2 proposal attached the cursor and receipt composite FK to `external_event_subscriptions(tenant_id,id,generation)`. **That is invalid for an actual subscription generation advance**: updating the parent current generation would be rejected if any old cursor/receipt existed. Revised physical model adds **immutable `external_subscription_generations`** and references it from receipts/cursors. New status generation requires inserting a row into the generation ledger and updating current subscription generation/status in one TX. Historical receipts remain resolvable. The new generation is not permission to dispatch unless the current subscription is ACTIVE, consent/credential valid, version and generation match. Old-generation late events remain audit-only. `DB-R4-E06` must execute 1→2 generation advance after committed old receipt and prove no FK failure, then reject stale generation dispatch in guarded application transaction. SQL FK alone does not implement dispatch authorization. This supersedes the preliminary v2 FK model. Tests not yet re-executed against the new version.
