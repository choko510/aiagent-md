import { FormEvent, useState } from "react";
import type { ApprovalRow, FullTaskView, TaskSummary } from "../api/client";
import ApprovalPanel from "./ApprovalPanel";

type Props = {
  authenticated: boolean;
  busy: boolean;
  tasks: TaskSummary[];
  selectedTaskId: string | null;
  taskDetail: FullTaskView | null;
  detailMatchesSelected: boolean;
  eventsText: string;
  onCreateTask: (goal: string, profile: "default" | "browser" | "sandbox" | "anthropic") => Promise<boolean>;
  onSelectTask: (taskId: string) => void;
  onControlTask: (action: "pause" | "resume" | "cancel") => Promise<boolean>;
  onSteerTask: (newGoal: string) => Promise<boolean>;
  onDecideApproval: (approval: ApprovalRow, decision: "APPROVED" | "REJECTED") => Promise<boolean>;
};

export default function TaskWorkspace({
  authenticated,
  busy,
  tasks,
  selectedTaskId,
  taskDetail,
  detailMatchesSelected,
  eventsText,
  onCreateTask,
  onSelectTask,
  onControlTask,
  onSteerTask,
  onDecideApproval,
}: Props) {
  const [goal, setGoal] = useState("");
  const [profile, setProfile] = useState<"default" | "browser" | "sandbox" | "anthropic">("default");
  const [steerGoal, setSteerGoal] = useState("");

  async function submitGoal(e: FormEvent) {
    e.preventDefault();
    if (!goal.trim()) return;
    if (await onCreateTask(goal.trim(), profile)) {
      setGoal("");
    }
  }

  async function submitSteer(e: FormEvent) {
    e.preventDefault();
    if (!steerGoal.trim()) return;
    if (await onSteerTask(steerGoal.trim())) {
      setSteerGoal("");
    }
  }

  const hasUnknownEffect = Boolean(
    detailMatchesSelected && taskDetail?.effects.some((ef) => ef.status === "UNKNOWN"),
  );

  return (
    <div className="console-grid" data-testid="task-workspace">
      <div className="panel">
        <h2>新しい Task 投入</h2>
        <form onSubmit={(e) => void submitGoal(e)} className="stack-form">
          <input
            type="text"
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            placeholder="達成したい目標を入力..."
            aria-label="新しいTask目標"
          />
          <select
            value={profile}
            onChange={(e) => setProfile(e.target.value as typeof profile)}
            aria-label="実行プロファイル"
          >
            <option value="default">default</option>
            <option value="browser">browser</option>
            <option value="sandbox">sandbox</option>
            <option value="anthropic">anthropic</option>
          </select>
          <button type="submit" disabled={!authenticated || busy || !goal.trim()}>
            Task 送信
          </button>
        </form>

        <h3>Task 一覧 ({tasks.length})</h3>
        <ul className="compact-list">
          {tasks.map((t) => (
            <li key={t.id}>
              <button
                type="button"
                className={selectedTaskId === t.id ? "chip chip-active" : "chip"}
                onClick={() => onSelectTask(t.id)}
                data-testid={`task-item-${t.id}`}
              >
                <div>
                  <strong>[{t.state}]</strong> {t.goalSummary ? t.goalSummary : `Task ${t.id.slice(0, 8)}`}
                </div>
                <div className="meta-row">
                  <span>{t.profile}</span>
                  <span>{t.id.slice(0, 8)}</span>
                  <span>更新: {new Date(t.updatedAt).toLocaleString()}</span>
                </div>
              </button>
            </li>
          ))}
        </ul>
      </div>

      <div className="panel">
        <h2>Run 詳細・承認・Steer</h2>
        {!detailMatchesSelected || !taskDetail ? (
          <p className="muted">
            {selectedTaskId ? "タスク詳細を読み込み中です..." : "左の一覧からタスクを選択してください。"}
          </p>
        ) : (
          <div className="detail-stack" data-testid="task-detail-panel">
            <p>
              <strong>Task:</strong> {taskDetail.id} / <strong>State:</strong> {taskDetail.state} (goalRev{" "}
              {taskDetail.goalRevision})
            </p>
            <p>
              <strong>Run:</strong> {taskDetail.run.id ?? "-"} / {taskDetail.run.state ?? "-"} (leaseEpoch{" "}
              {taskDetail.run.leaseEpoch}, HEAD {taskDetail.run.headRevision})
            </p>
            {taskDetail.run.result && (
              <div className="result-box">
                <strong>Result:</strong> {taskDetail.run.result}
              </div>
            )}
            {taskDetail.run.errorCode && (
              <div className="banner banner-error">Error: {taskDetail.run.errorCode}</div>
            )}
            <div className="btn-row">
              <button type="button" disabled={busy} onClick={() => void onControlTask("pause")}>
                Pause
              </button>
              <button type="button" disabled={busy} onClick={() => void onControlTask("resume")}>
                Resume
              </button>
              <button type="button" className="danger-btn" disabled={busy} onClick={() => void onControlTask("cancel")}>
                Cancel
              </button>
            </div>
            <form onSubmit={(e) => void submitSteer(e)} className="inline-form">
              <input
                type="text"
                value={steerGoal}
                onChange={(e) => setSteerGoal(e.target.value)}
                placeholder="Steer 新しい目標"
                aria-label="Steer目標"
              />
              <button type="submit" disabled={busy || !steerGoal.trim()}>
                Steer 送信
              </button>
            </form>

            {hasUnknownEffect && (
              <div className="banner banner-error" role="alert" data-testid="unknown-effect-warning">
                結果未確定（UNKNOWN）の外部作用が存在します。二重送信を防ぐため自動再実行は抑止されています。相手先を確認してリコンサイルしてください。
              </div>
            )}

            <ApprovalPanel approvals={taskDetail.approvals} disabled={busy} onDecide={onDecideApproval} />

            <h3>効果台帳 / Intents</h3>
            <ul className="compact-list">
              {taskDetail.effects.map((e, i) => (
                <li key={`${e.tool}-${i}`}>
                  {e.tool}: <strong>{e.status}</strong>
                </li>
              ))}
            </ul>

            <h3>SSE ドメインイベント</h3>
            <pre className="event-log" data-testid="task-sse-log">
              {eventsText || "(イベントなし)"}
            </pre>
          </div>
        )}
      </div>
    </div>
  );
}
