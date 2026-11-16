import { FormEvent, useEffect, useState } from "react";
import type { ConsentItem, ContextPolicyItem, MemoryItem, MemoryRevisionItem } from "../api/client";

type Props = {
  authenticated: boolean;
  busy: boolean;
  selectedTaskId: string | null;
  memories: MemoryItem[];
  revisionsByMemory: Record<string, MemoryRevisionItem[]>;
  consents: ConsentItem[];
  contextPolicy: ContextPolicyItem | null;
  onRevokeMemory: (memoryId: string) => Promise<boolean>;
  onLoadRevisions: (memoryId: string) => Promise<boolean>;
  onGrantConsent: (recipient: string) => Promise<boolean>;
  onRevokeConsent: (recipient: string) => Promise<boolean>;
  onSaveContextPolicy: (payload: {
    recipient: string;
    model: string;
    enabled: boolean;
    semanticVerifierMode: "HEURISTIC" | "LLM_GATED";
    expectedRecordVersion: string | null;
  }) => Promise<boolean>;
};

export default function MemoryAndPolicyPanel({
  authenticated,
  busy,
  selectedTaskId,
  memories,
  revisionsByMemory,
  consents,
  contextPolicy,
  onRevokeMemory,
  onLoadRevisions,
  onGrantConsent,
  onRevokeConsent,
  onSaveContextPolicy,
}: Props) {
  const [recipientInput, setRecipientInput] = useState("anthropic");
  const [policyRecipient, setPolicyRecipient] = useState(contextPolicy?.recipient ?? "anthropic");
  const [policyModel, setPolicyModel] = useState(contextPolicy?.model ?? "claude-sonnet-5-5");
  const [policyEnabled, setPolicyEnabled] = useState(contextPolicy?.enabled ?? true);
  const [verifierMode, setVerifierMode] = useState<"HEURISTIC" | "LLM_GATED">(
    contextPolicy?.semanticVerifierMode ?? "HEURISTIC",
  );

  useEffect(() => {
    setPolicyRecipient(contextPolicy?.recipient ?? "anthropic");
    setPolicyModel(contextPolicy?.model ?? "claude-sonnet-5-5");
    setPolicyEnabled(contextPolicy?.enabled ?? true);
    setVerifierMode(contextPolicy?.semanticVerifierMode ?? "HEURISTIC");
  }, [selectedTaskId, contextPolicy]);

  async function submitConsent(e: FormEvent) {
    e.preventDefault();
    if (!recipientInput.trim()) return;
    await onGrantConsent(recipientInput.trim());
  }

  async function submitPolicy(e: FormEvent) {
    e.preventDefault();
    if (!selectedTaskId || !policyRecipient.trim() || !policyModel.trim()) return;
    await onSaveContextPolicy({
      recipient: policyRecipient.trim(),
      model: policyModel.trim(),
      enabled: policyEnabled,
      semanticVerifierMode: verifierMode,
      expectedRecordVersion: contextPolicy ? String(contextPolicy.recordVersion) : "0",
    });
  }

  return (
    <div className="console-grid" data-testid="memory-policy-panel">
      <div className="panel">
        <h2>外部モデル 記憶・利用同意管理</h2>
        <p className="muted">
          平文の記憶は API に保持されません。同意を撤回すると、削除履歴（復元時の再適用防止）によりモデル事業者への送信が直ちに遮断されます。
        </p>
        <form onSubmit={(e) => void submitConsent(e)} className="inline-form">
          <input
            type="text"
            value={recipientInput}
            onChange={(e) => setRecipientInput(e.target.value)}
            placeholder="送信先モデル事業者 (例: anthropic, openai)"
            aria-label="Memory同意送信先"
          />
          <button type="submit" disabled={!authenticated || busy || !recipientInput.trim()}>
            同意付与
          </button>
        </form>

        <ul className="compact-list">
          {consents.map((c) => (
            <li key={c.recipient} className="row-between" data-testid={`consent-${c.recipient}`}>
              <span>
                <strong>{c.recipient}</strong> · {c.granted ? "GRANTED" : "REVOKED"} (v{c.recordVersion})
              </span>
              {c.granted ? (
                <button
                  type="button"
                  className="danger-btn"
                  disabled={!authenticated || busy}
                  onClick={() => void onRevokeConsent(c.recipient)}
                >
                  同意撤回
                </button>
              ) : (
                <button
                  type="button"
                  disabled={!authenticated || busy}
                  onClick={() => void onGrantConsent(c.recipient)}
                >
                  再同意
                </button>
              )}
            </li>
          ))}
        </ul>

        <h3>タスク別 Context Policy {selectedTaskId ? `(${selectedTaskId.slice(0, 8)})` : ""}</h3>
        {!selectedTaskId ? (
          <p className="muted">Tasks タブでタスクを選択すると Context Policy を編集できます。</p>
        ) : (
          <form onSubmit={(e) => void submitPolicy(e)} className="stack-form">
            {contextPolicy && (
              <p className="muted">
                現行: {contextPolicy.recipient} / {contextPolicy.model} / enabled={String(contextPolicy.enabled)} / mode=
                {contextPolicy.semanticVerifierMode} (v{contextPolicy.recordVersion})
              </p>
            )}
            <input
              type="text"
              value={policyRecipient}
              onChange={(e) => setPolicyRecipient(e.target.value)}
              placeholder="送信先モデル事業者"
              aria-label="Policy送信先"
            />
            <input
              type="text"
              value={policyModel}
              onChange={(e) => setPolicyModel(e.target.value)}
              placeholder="利用モデル名"
              aria-label="Policyモデル"
            />
            <select
              value={verifierMode}
              onChange={(e) => setVerifierMode(e.target.value as "HEURISTIC" | "LLM_GATED")}
              aria-label="Semantic Verifierモード"
            >
              <option value="HEURISTIC">自動判定 (HEURISTIC)</option>
              <option value="LLM_GATED">LLM 二重検証 (LLM_GATED)</option>
            </select>
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={policyEnabled}
                onChange={(e) => setPolicyEnabled(e.target.checked)}
              />
              Context 圧縮送信を有効化
            </label>
            <button type="submit" disabled={!authenticated || busy}>
              Context Policy 保存
            </button>
          </form>
        )}
      </div>

      <div className="panel">
        <h2>Task Memory メタデータ ({memories.length})</h2>
        {!selectedTaskId ? (
          <p className="muted">左の一覧または Tasks タブで対象タスクを選択してください。</p>
        ) : memories.length === 0 ? (
          <p className="muted">このタスクに登録された Memory はありません。</p>
        ) : (
          <ul className="compact-list">
            {memories.map((m) => (
              <li key={m.id} className="card-subtle" data-testid={`memory-${m.id}`}>
                <div>
                  <strong>{m.recipient}</strong> · rev {m.revision} · {m.revoked ? "REVOKED" : "ACTIVE"}
                </div>
                <div className="muted">
                  source={m.sourceArtifactRef.slice(0, 8)} · expires={m.expiresAt}
                </div>
                <div className="btn-row">
                  <button
                    type="button"
                    disabled={!authenticated || busy}
                    onClick={() => void onLoadRevisions(m.id)}
                  >
                    履歴表示
                  </button>
                  {!m.revoked && (
                    <button
                      type="button"
                      className="danger-btn"
                      disabled={!authenticated || busy}
                      onClick={() => void onRevokeMemory(m.id)}
                      data-testid={`revoke-memory-${m.id}`}
                    >
                      削除（復元時再適用を防止）
                    </button>
                  )}
                </div>
                {revisionsByMemory[m.id] && (
                  <ul className="compact-list">
                    {revisionsByMemory[m.id].map((rev) => (
                      <li key={rev.revision}>
                        r{rev.revision}: {rev.recipient} (edited {rev.editedAt})
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
