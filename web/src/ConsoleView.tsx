import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import type { TaskState, TaskV1 } from "./api/generated/domain";

type TaskSummary = {
  id: string;
  state: TaskState;
  profile: string;
  goalRevision: string;
  activeRunId: string | null;
  runState: string | null;
  createdAt: string;
  updatedAt: string;
};

type BrowserSessionItem = {
  id: string;
  taskId: string;
  runId: string;
  state: string;
  mode: string;
  generation: number;
  goalRevision: string;
  leaseEpoch: string;
  updatedAt: string;
};

type CampaignItem = {
  id: string;
  state: string;
  controlVersion: number;
  goalRevision: string;
  updatedAt: string;
};

type WatchItem = {
  watchId: string;
  timezoneId: string;
  maxOccurrences: number;
  revision: number;
  active: boolean;
  createdAt: string;
};

type CapabilityItem = {
  component: string;
  integrationState: string;
  memoryEgressPath: string;
  externalActionsEnabled: boolean;
  ownerConsentRequired: boolean;
  hostVerified: boolean;
  productionReady: boolean;
};

type ApprovalRow = {
  id: string;
  tool: string;
  summary: string;
  decision: string;
  requestSha256: string;
  recordVersion: string;
  goalRevision: string;
};

type FullTaskView = {
  id: TaskV1["id"];
  state: TaskV1["state"];
  goalRevision: TaskV1["goalRevision"];
  recordVersion: TaskV1["recordVersion"];
  run: {
    id: string;
    state: string;
    attemptNo: number;
    leaseEpoch: string;
    headRevision: string;
    result: string | null;
    error: string | null;
  };
  approvals: ApprovalRow[];
  effects: Array<{ tool: string; status: string }>;
};

export default function ConsoleView() {
  const [token, setToken] = useState("");
  const [goal, setGoal] = useState("");
  const [profile, setProfile] = useState<"default" | "browser" | "sandbox" | "anthropic">("default");
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [taskDetail, setTaskDetail] = useState<FullTaskView | null>(null);
  const [eventsText, setEventsText] = useState<string>("");
  const [steerGoal, setSteerGoal] = useState("");
  const [sessions, setSessions] = useState<BrowserSessionItem[]>([]);
  const [campaigns, setCampaigns] = useState<CampaignItem[]>([]);
  const [watches, setWatches] = useState<WatchItem[]>([]);
  const [capabilities, setCapabilities] = useState<CapabilityItem[]>([]);
  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const authEpochRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const taskEpochRef = useRef(0);
  const taskAbortRef = useRef<AbortController | null>(null);

  const clearOwnerState = useCallback(() => {
    taskEpochRef.current += 1;
    taskAbortRef.current?.abort();
    taskAbortRef.current = null;
    setTasks([]);
    setSelectedTaskId(null);
    setTaskDetail(null);
    setEventsText("");
    setSessions([]);
    setCampaigns([]);
    setWatches([]);
    setCapabilities([]);
    setStatusMsg(null);
  }, []);

  const selectTask = useCallback((nextId: string | null) => {
    taskEpochRef.current += 1;
    taskAbortRef.current?.abort();
    taskAbortRef.current = null;
    setSelectedTaskId(nextId);
    setTaskDetail(null);
    setEventsText("");
  }, []);

  const handleTokenChange = useCallback(
    (nextToken: string) => {
      authEpochRef.current += 1;
      abortRef.current?.abort();
      abortRef.current = null;
      setToken(nextToken);
      setErrorMsg(null);
      clearOwnerState();
    },
    [clearOwnerState],
  );

  const authHeaders = useCallback((): Record<string, string> => {
    const t = token.trim();
    return t ? { Authorization: `Bearer ${t}`, "Content-Type": "application/json" } : { "Content-Type": "application/json" };
  }, [token]);

  const loadOverview = useCallback(async () => {
    if (!token.trim()) {
      clearOwnerState();
      return;
    }
    const epoch = authEpochRef.current;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setErrorMsg(null);
    try {
      const headers = authHeaders();
      const [tRes, bRes, cRes, wRes, capRes] = await Promise.all([
        fetch("/v1/tasks", { headers, signal: controller.signal }),
        fetch("/v1/browser/sessions", { headers, signal: controller.signal }),
        fetch("/v1/campaigns", { headers, signal: controller.signal }),
        fetch("/v1/watches", { headers, signal: controller.signal }),
        fetch("/v1/step4/capabilities", { headers, signal: controller.signal }),
      ]);
      if (epoch !== authEpochRef.current) return;
      if (!tRes.ok) {
        clearOwnerState();
        setErrorMsg(`認証エラーまたはAPI未接続 (HTTP ${tRes.status})`);
        return;
      }
      const [tJson, bJson, cJson, wJson, capJson] = await Promise.all([
        tRes.json(),
        bRes.ok ? bRes.json() : Promise.resolve({ items: [] }),
        cRes.ok ? cRes.json() : Promise.resolve({ items: [] }),
        wRes.ok ? wRes.json() : Promise.resolve({ items: [] }),
        capRes.ok ? capRes.json() : Promise.resolve({ capabilities: [] }),
      ]);
      if (epoch !== authEpochRef.current) return;
      setTasks(tJson.items ?? []);
      setSessions(bJson.items ?? []);
      setCampaigns(cJson.items ?? []);
      setWatches(wJson.items ?? []);
      setCapabilities(capJson.capabilities ?? []);
    } catch (err) {
      if ((err as { name?: string })?.name === "AbortError" || epoch !== authEpochRef.current) return;
      clearOwnerState();
      setErrorMsg("Control Plane API (/v1) に接続できませんでした。");
    }
  }, [token, authHeaders, clearOwnerState]);

  const loadTaskDetail = useCallback(
    async (id: string) => {
      if (!token.trim()) return;
      const authEpoch = authEpochRef.current;
      const taskEpoch = ++taskEpochRef.current;
      taskAbortRef.current?.abort();
      const controller = new AbortController();
      taskAbortRef.current = controller;
      const headers = authHeaders();
      try {
        const [dRes, eRes] = await Promise.all([
          fetch(`/v1/tasks/${encodeURIComponent(id)}`, { headers, signal: controller.signal }),
          fetch(`/v1/tasks/${encodeURIComponent(id)}/events/stream?follow=true&max_polls=4`, {
            headers,
            signal: controller.signal,
          }),
        ]);
        if (authEpoch !== authEpochRef.current || taskEpoch !== taskEpochRef.current) return;
        if (!dRes.ok) {
          if (dRes.status === 401 || dRes.status === 403 || dRes.status === 404) {
            setTaskDetail(null);
            setEventsText("");
          }
          return;
        }
        const detail = (await dRes.json()) as FullTaskView;
        const evText = eRes.ok ? await eRes.text() : "";
        if (authEpoch !== authEpochRef.current || taskEpoch !== taskEpochRef.current) return;
        if (detail.id !== id) return;
        setTaskDetail(detail);
        if (eRes.ok) setEventsText(evText);
      } catch (err) {
        if ((err as { name?: string })?.name === "AbortError") return;
        // Transient read failure is ignored if owner or task selection switched.
      }
    },
    [token, authHeaders],
  );

  useEffect(() => {
    if (!selectedTaskId) {
      taskAbortRef.current?.abort();
      taskAbortRef.current = null;
      return;
    }
    void loadTaskDetail(selectedTaskId);
    return () => {
      taskAbortRef.current?.abort();
    };
  }, [selectedTaskId, loadTaskDetail]);

  useEffect(() => {
    if (!selectedTaskId || !token.trim()) return;
    if (taskDetail && ["SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"].includes(taskDetail.state)) return;
    const timer = window.setInterval(() => {
      void loadTaskDetail(selectedTaskId);
    }, 3000);
    return () => window.clearInterval(timer);
  }, [selectedTaskId, token, taskDetail, loadTaskDetail]);

  async function submitNewTask(event: FormEvent) {
    event.preventDefault();
    if (!token.trim() || !goal.trim()) return;
    setErrorMsg(null);
    setStatusMsg(null);
    const res = await fetch("/v1/tasks", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ schemaVersion: "1.0", goal: goal.trim(), profile }),
    });
    if (!res.ok) {
      setErrorMsg(`タスク投入に失敗しました (HTTP ${res.status})`);
      return;
    }
    const created = (await res.json()) as { taskId: string };
    setGoal("");
    setStatusMsg(`タスクを投入しました: ${created.taskId}`);
    selectTask(created.taskId);
    await loadOverview();
  }

  async function decideApproval(approval: ApprovalRow, decision: "APPROVED" | "REJECTED") {
    const targetId = selectedTaskId;
    if (!targetId || !taskDetail || taskDetail.id !== targetId) return;
    const res = await fetch(`/v1/approvals/${encodeURIComponent(approval.id)}/decision`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({
        schemaVersion: "1.0",
        decision,
        requestSha256: approval.requestSha256,
        expectedRecordVersion: approval.recordVersion,
      }),
    });
    if (!res.ok) {
      setErrorMsg(`承認操作に失敗しました (HTTP ${res.status})`);
      return;
    }
    setStatusMsg(`承認操作を確定しました: ${approval.id} -> ${decision}`);
    await loadTaskDetail(targetId);
  }

  async function controlTask(action: "pause" | "resume" | "cancel") {
    const targetId = selectedTaskId;
    if (!targetId || !taskDetail || taskDetail.id !== targetId) return;
    const res = await fetch(`/v1/tasks/${encodeURIComponent(targetId)}/${action}`, {
      method: "POST",
      headers: authHeaders(),
    });
    if (!res.ok) {
      setErrorMsg(`Run 制御 (${action}) に失敗しました (HTTP ${res.status})`);
      return;
    }
    setStatusMsg(`Run 制御を実行しました: ${action}`);
    await loadOverview();
    await loadTaskDetail(targetId);
  }

  async function sendSteer(event: FormEvent) {
    event.preventDefault();
    const targetId = selectedTaskId;
    if (!targetId || !taskDetail || taskDetail.id !== targetId || !steerGoal.trim()) return;
    const res = await fetch(`/v1/tasks/${encodeURIComponent(targetId)}/steer`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({
        schemaVersion: "1.0",
        goal: steerGoal.trim(),
        expectedGoalRevision: taskDetail.goalRevision,
      }),
    });
    if (!res.ok) {
      setErrorMsg(`Steer に失敗しました (HTTP ${res.status})`);
      return;
    }
    setSteerGoal("");
    setStatusMsg("目標 (Steer) を更新しました");
    await loadOverview();
    await loadTaskDetail(targetId);
  }

  const detailMatchesSelected = Boolean(taskDetail && selectedTaskId && taskDetail.id === selectedTaskId);

  return (
    <section className="console" aria-label="Control Plane Console">
      <div className="panel console-auth">
        <label>
          <span>Owner API Token（メモリ保持のみ・保存しません）</span>
          <input
            type="password"
            value={token}
            onChange={(e) => handleTokenChange(e.target.value)}
            placeholder="aiag_... を入力"
          />
        </label>
        <button type="button" onClick={() => void loadOverview()} disabled={!token.trim()}>
          状態を読み込む
        </button>
      </div>

      {errorMsg && <div className="banner banner-error" role="alert">{errorMsg}</div>}
      {statusMsg && <div className="banner">{statusMsg}</div>}

      <div className="console-grid">
        <div className="panel">
          <h2>新規タスク投入</h2>
          <form onSubmit={(e) => void submitNewTask(e)} className="stack-form">
            <input
              type="text"
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              placeholder="実行目標を入力"
              aria-label="タスク目標"
            />
            <select
              value={profile}
              onChange={(e) => setProfile(e.target.value as typeof profile)}
              aria-label="プロファイル"
            >
              <option value="default">default</option>
              <option value="browser">browser (offline fixture + approval)</option>
              <option value="sandbox">sandbox (execution cell)</option>
              <option value="anthropic">anthropic (provider route)</option>
            </select>
            <button type="submit" disabled={!token.trim() || !goal.trim()}>
              タスク投入
            </button>
          </form>

          <h3>タスク一覧 ({tasks.length})</h3>
          <ul className="compact-list">
            {tasks.map((t) => (
              <li key={t.id}>
                <button
                  type="button"
                  className={selectedTaskId === t.id ? "chip chip-active" : "chip"}
                  onClick={() => selectTask(t.id)}
                >
                  [{t.state}] {t.profile} · {t.id.slice(0, 8)}
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
            <div className="detail-stack">
              <p>
                <strong>Task:</strong> {taskDetail.id} / <strong>State:</strong> {taskDetail.state} (goalRev{" "}
                {taskDetail.goalRevision})
              </p>
              <p>
                <strong>Run:</strong> {taskDetail.run.id} / {taskDetail.run.state} (leaseEpoch{" "}
                {taskDetail.run.leaseEpoch}, HEAD {taskDetail.run.headRevision})
              </p>
              <div className="btn-row">
                <button type="button" disabled={!detailMatchesSelected} onClick={() => void controlTask("pause")}>
                  Pause / Takeover
                </button>
                <button type="button" disabled={!detailMatchesSelected} onClick={() => void controlTask("resume")}>
                  Resume
                </button>
                <button type="button" disabled={!detailMatchesSelected} onClick={() => void controlTask("cancel")}>
                  Cancel
                </button>
              </div>
              <form onSubmit={(e) => void sendSteer(e)} className="inline-form">
                <input
                  type="text"
                  value={steerGoal}
                  onChange={(e) => setSteerGoal(e.target.value)}
                  placeholder="Steer 新しい目標"
                  aria-label="Steer目標"
                />
                <button type="submit" disabled={!detailMatchesSelected || !steerGoal.trim()}>
                  Steer 送信
                </button>
              </form>

              <h3>承認リクエスト ({taskDetail.approvals.length})</h3>
              {taskDetail.approvals.map((a) => (
                <div key={a.id} className="approval-card">
                  <div>
                    <strong>{a.tool}</strong> — {a.summary} ({a.decision})
                  </div>
                  {a.decision === "PENDING" && (
                    <div className="btn-row">
                      <button
                        type="button"
                        disabled={!detailMatchesSelected}
                        onClick={() => void decideApproval(a, "APPROVED")}
                      >
                        承認 (APPROVE)
                      </button>
                      <button
                        type="button"
                        disabled={!detailMatchesSelected}
                        onClick={() => void decideApproval(a, "REJECTED")}
                      >
                        拒否 (REJECT)
                      </button>
                    </div>
                  )}
                </div>
              ))}

              <h3>効果台帳 / Intents</h3>
              <ul className="compact-list">
                {taskDetail.effects.map((e, i) => (
                  <li key={`${e.tool}-${i}`}>{e.tool}: {e.status}</li>
                ))}
              </ul>

              <h3>SSE ドメインイベント</h3>
              <pre className="event-log">{eventsText || "(イベントなし)"}</pre>
            </div>
          )}
        </div>
      </div>

      <div className="console-grid">
        <div className="panel">
          <h2>Browser セッション & Takeover ({sessions.length})</h2>
          <ul className="compact-list">
            {sessions.map((b) => (
              <li key={b.id}>
                {b.id.slice(0, 8)} · {b.state} · mode={b.mode} · gen={b.generation} · lease={b.leaseEpoch}
              </li>
            ))}
          </ul>

          <h2>Automation Campaign / Watch</h2>
          <p className="muted">Campaigns: {campaigns.length} / Watches: {watches.length}</p>
          <ul className="compact-list">
            {campaigns.map((c) => (
              <li key={c.id}>
                Campaign {c.id}: {c.state} (v{c.controlVersion}, goalRev {c.goalRevision})
              </li>
            ))}
            {watches.map((w) => (
              <li key={w.watchId}>
                Watch {w.watchId} ({w.timezoneId}, max={w.maxOccurrences}, active={String(w.active)})
              </li>
            ))}
          </ul>
        </div>

        <div className="panel">
          <h2>System Capabilities (誠実ステータス)</h2>
          <ul className="compact-list">
            {capabilities.map((cap) => (
              <li key={cap.component}>
                <strong>{cap.component}</strong>: {cap.integrationState} · externalActions={String(cap.externalActionsEnabled)}{" "}
                · hostVerified={String(cap.hostVerified)} · prodReady={String(cap.productionReady)}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </section>
  );
}
