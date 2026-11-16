import { FormEvent, useState } from "react";
import type { SessionInfo } from "../api/client";

type Props = {
  session: SessionInfo | null;
  busy: boolean;
  onLogin: (token: string) => Promise<void>;
  onLogout: () => Promise<void>;
  onRefresh: () => Promise<void>;
};

export default function AuthSessionBar({ session, busy, onLogin, onLogout, onRefresh }: Props) {
  const [tokenInput, setTokenInput] = useState("");

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const trimmed = tokenInput.trim();
    if (!trimmed) return;
    await onLogin(trimmed);
    // Clear credential from React state and DOM immediately after Cookie session exchange.
    setTokenInput("");
  }

  return (
    <div className="panel" data-testid="auth-session-panel">
      <h2>認証セッション（HttpOnly Cookie + CSRF 保護）</h2>
      {session?.authenticated ? (
        <div className="session-active-bar">
          <div className="session-meta">
            <span className="badge badge-ok">AUTHENTICATED</span>
            <span>Tenant: {session.tenantId?.slice(0, 8)}</span>
            <span>Workspace: {session.workspaceId?.slice(0, 8)}</span>
            {session.expiresAt && <span>期限: {new Date(session.expiresAt).toLocaleString()}</span>}
          </div>
          <div className="btn-row">
            <button type="button" disabled={busy} onClick={() => void onRefresh()}>
              状態更新
            </button>
            <button type="button" className="secondary-btn" disabled={busy} onClick={() => void onLogout()}>
              ログアウト
            </button>
          </div>
        </div>
      ) : (
        <form className="console-auth" onSubmit={(e) => void handleSubmit(e)}>
          <label>
            API Bearer トークン（セッション交換後は画面へ保存しません）
            <input
              type="password"
              value={tokenInput}
              onChange={(e) => setTokenInput(e.target.value)}
              placeholder="aiag_..."
              autoComplete="off"
              aria-label="APIトークン"
            />
          </label>
          <button type="submit" disabled={busy || !tokenInput.trim()}>
            セッション開始
          </button>
        </form>
      )}
    </div>
  );
}
