import type { ApprovalRow } from "../api/client";

type Props = {
  approvals: ApprovalRow[];
  disabled: boolean;
  onDecide: (approval: ApprovalRow, decision: "APPROVED" | "REJECTED") => Promise<boolean>;
};

export default function ApprovalPanel({ approvals, disabled, onDecide }: Props) {
  return (
    <div data-testid="approval-queue-panel">
      <h3>承認リクエスト ({approvals.length})</h3>
      {approvals.length === 0 && <p className="muted">承認待ちのリクエストはありません。</p>}
      {approvals.map((a) => (
        <div key={a.id} className="approval-card" data-testid={`approval-${a.id}`}>
          <div>
            <strong>{a.tool}</strong> — {a.summary}
          </div>
          <div className="meta-row">
            <span>判定: <strong>{a.decision}</strong></span>
            <span>goalRev: {a.goalRevision}</span>
            <span>ver: {a.recordVersion}</span>
            <span title={a.requestSha256}>sha256: {a.requestSha256.slice(0, 12)}…</span>
          </div>
          {a.decision === "PENDING" && (
            <div className="btn-row">
              <button type="button" disabled={disabled} onClick={() => void onDecide(a, "APPROVED")}>
                承認 (APPROVE)
              </button>
              <button
                type="button"
                className="danger-btn"
                disabled={disabled}
                onClick={() => void onDecide(a, "REJECTED")}
              >
                拒否 (REJECT)
              </button>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
