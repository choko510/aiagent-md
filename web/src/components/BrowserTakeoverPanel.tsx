import { FormEvent, useEffect, useState } from "react";
import type { BrowserObservation, BrowserSessionItem } from "../api/client";

type Props = {
  authenticated: boolean;
  busy: boolean;
  sessions: BrowserSessionItem[];
  selectedSessionId: string | null;
  observation: BrowserObservation | null;
  takeoverResult: { authStatus?: string; authenticated?: boolean; observation?: string } | null;
  onSelectSession: (sessionId: string) => Promise<void>;
  onStartTakeover: (sessionId: string, reason: string) => Promise<void>;
  onAuthenticate: (
    sessionId: string,
    payload: {
      url: string;
      method: "POST" | "GET";
      formData: Record<string, string>;
      verifyUrl?: string;
      verifyContains?: string;
    },
  ) => Promise<void>;
  onCompleteTakeover: (sessionId: string) => Promise<void>;
  onAbortTakeover: (sessionId: string) => Promise<void>;
};

export default function BrowserTakeoverPanel({
  authenticated,
  busy,
  sessions,
  selectedSessionId,
  observation,
  takeoverResult,
  onSelectSession,
  onStartTakeover,
  onAuthenticate,
  onCompleteTakeover,
  onAbortTakeover,
}: Props) {
  const [reason, setReason] = useState("HUMAN_LOGIN");
  const [loginUrl, setLoginUrl] = useState("");
  const [method, setMethod] = useState<"POST" | "GET">("POST");
  const [formKey, setFormKey] = useState("username");
  const [formVal, setFormVal] = useState("");
  const [secretKey, setSecretKey] = useState("password");
  const [secretVal, setSecretVal] = useState("");
  const [verifyUrl, setVerifyUrl] = useState("");
  const [verifyContains, setVerifyContains] = useState("");

  useEffect(() => {
    setSecretVal("");
    setVerifyUrl("");
    setVerifyContains("");
  }, [selectedSessionId]);

  const activeSession = sessions.find((s) => s.id === selectedSessionId) ?? null;

  async function handleAuthSubmit(e: FormEvent) {
    e.preventDefault();
    if (!selectedSessionId || !loginUrl.trim()) return;
    const formData: Record<string, string> = {};
    if (formKey.trim()) formData[formKey.trim()] = formVal;
    if (secretKey.trim() && secretVal) formData[secretKey.trim()] = secretVal;
    await onAuthenticate(selectedSessionId, {
      url: loginUrl.trim(),
      method,
      formData,
      verifyUrl: verifyUrl.trim() || undefined,
      verifyContains: verifyContains.trim() || undefined,
    });
    // Clear password/input field immediately after submission.
    setSecretVal("");
  }

  return (
    <div className="console-grid" data-testid="browser-takeover-panel">
      <div className="panel">
        <h2>Browser セッション一覧 ({sessions.length})</h2>
        {sessions.length === 0 ? (
          <p className="muted">アクティブまたは最近の Browser セッションはありません。</p>
        ) : (
          <ul className="compact-list">
            {sessions.map((b) => (
              <li key={b.id}>
                <button
                  type="button"
                  className={selectedSessionId === b.id ? "chip chip-active" : "chip"}
                  onClick={() => void onSelectSession(b.id)}
                  data-testid={`browser-session-${b.id}`}
                >
                  {b.id.slice(0, 8)} · {b.state} · mode={b.mode} · gen={b.generation} · storage=
                  {b.hasStorageState ? "encrypted" : "none"}
                </button>
              </li>
            ))}
          </ul>
        )}

        {observation && (
          <div className="detail-stack" data-testid="browser-observation-box">
            <h3>
              Observe-only ビュー <span className="badge badge-info">READ-ONLY</span>
            </h3>
            <p className="muted">
              現在URL: {observation.currentUrl ?? "(none)"} / 理由: {observation.takeoverReason ?? "-"}
            </p>
            <pre className="event-log">{observation.observation || "(観測テキストなし)"}</pre>
          </div>
        )}
      </div>

      <div className="panel">
        <h2>Human Takeover 操作境界</h2>
        {!activeSession ? (
          <p className="muted">左の一覧から Browser セッションを選択してください。</p>
        ) : (
          <div className="detail-stack">
            <p>
              <strong>Session:</strong> {activeSession.id} · <strong>State:</strong> {activeSession.state} ·{" "}
              <strong>Mode:</strong> {activeSession.mode}
            </p>
            <div className="inline-form">
              <input
                type="text"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                aria-label="Takeover理由"
                placeholder="TAKEOVER_REASON"
              />
              <button
                type="button"
                disabled={!authenticated || busy || activeSession.state === "CLOSED"}
                onClick={() => void onStartTakeover(activeSession.id, reason)}
              >
                Takeover 開始
              </button>
            </div>

            <form onSubmit={(e) => void handleAuthSubmit(e)} className="stack-form">
              <h3>認証送信（Trusted Egress 経由 / storageState AES-GCM 保存）</h3>
              <input
                type="url"
                value={loginUrl}
                onChange={(e) => setLoginUrl(e.target.value)}
                placeholder="https://example.test/login"
                aria-label="ログインURL"
              />
              <select value={method} onChange={(e) => setMethod(e.target.value as "POST" | "GET")}>
                <option value="POST">POST</option>
                <option value="GET">GET</option>
              </select>
              <div className="inline-form">
                <input
                  type="text"
                  value={formKey}
                  onChange={(e) => setFormKey(e.target.value)}
                  placeholder="フィールド名1"
                  aria-label="フィールド名1"
                />
                <input
                  type="text"
                  value={formVal}
                  onChange={(e) => setFormVal(e.target.value)}
                  placeholder="値1"
                  aria-label="値1"
                />
              </div>
              <div className="inline-form">
                <input
                  type="text"
                  value={secretKey}
                  onChange={(e) => setSecretKey(e.target.value)}
                  placeholder="パスワードフィールド名"
                  aria-label="パスワードフィールド名"
                />
                <input
                  type="password"
                  value={secretVal}
                  onChange={(e) => setSecretVal(e.target.value)}
                  placeholder="パスワード値"
                  aria-label="パスワード値"
                />
              </div>
              <input
                type="url"
                value={verifyUrl}
                onChange={(e) => setVerifyUrl(e.target.value)}
                placeholder="verifyUrl (任意: 同一オリジン確認先)"
                aria-label="検証URL"
              />
              <input
                type="text"
                value={verifyContains}
                onChange={(e) => setVerifyContains(e.target.value)}
                placeholder="verifyContains (任意: 認証成功マーカー)"
                aria-label="検証文字列"
              />
              <button type="submit" disabled={!authenticated || busy || !loginUrl.trim()}>
                認証リクエスト実行
              </button>
            </form>

            {takeoverResult && (
              <div className="banner" data-testid="takeover-auth-result">
                認証結果: authStatus=<strong>{takeoverResult.authStatus ?? "-"}</strong> / authenticated=
                {String(takeoverResult.authenticated ?? false)}
              </div>
            )}

            <div className="btn-row">
              <button
                type="button"
                disabled={!authenticated || busy}
                onClick={() => void onCompleteTakeover(activeSession.id)}
              >
                Takeover 完了 (AGENT へ返還)
              </button>
              <button
                type="button"
                className="danger-btn"
                disabled={!authenticated || busy}
                onClick={() => void onAbortTakeover(activeSession.id)}
              >
                セッション破棄 (Abort)
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
