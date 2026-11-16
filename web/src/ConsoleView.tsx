import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiClientError,
  createClientRequest,
  type ApprovalRow,
  type BrowserObservation,
  type BrowserSessionItem,
  type CampaignItem,
  type CapabilityItem,
  type ConsentItem,
  type ContextPolicyItem,
  type FullTaskView,
  type MemoryItem,
  type MemoryRevisionItem,
  type NotificationBinding,
  type NotificationIntentItem,
  type SessionInfo,
  type TaskSummary,
  type WatchItem,
} from "./api/client";
import AuthSessionBar from "./components/AuthSessionBar";
import AutomationAndNotifyPanel from "./components/AutomationAndNotifyPanel";
import BrowserTakeoverPanel from "./components/BrowserTakeoverPanel";
import CapabilitiesBanner from "./components/CapabilitiesBanner";
import MemoryAndPolicyPanel from "./components/MemoryAndPolicyPanel";
import TaskWorkspace from "./components/TaskWorkspace";

type SubTab = "tasks" | "memory" | "browser" | "automation";

export default function ConsoleView() {
  const [subTab, setSubTab] = useState<SubTab>("tasks");
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [taskDetail, setTaskDetail] = useState<FullTaskView | null>(null);
  const [eventsText, setEventsText] = useState("");

  const [sessions, setSessions] = useState<BrowserSessionItem[]>([]);
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [observation, setObservation] = useState<BrowserObservation | null>(null);
  const [takeoverResult, setTakeoverResult] = useState<{
    authStatus?: string;
    authenticated?: boolean;
    observation?: string;
  } | null>(null);

  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [revisionsByMemory, setRevisionsByMemory] = useState<Record<string, MemoryRevisionItem[]>>({});
  const [consents, setConsents] = useState<ConsentItem[]>([]);
  const [contextPolicy, setContextPolicy] = useState<ContextPolicyItem | null>(null);

  const [campaigns, setCampaigns] = useState<CampaignItem[]>([]);
  const [selectedCampaignId, setSelectedCampaignId] = useState<string | null>(null);
  const [campaignEventsText, setCampaignEventsText] = useState("");
  const [watches, setWatches] = useState<WatchItem[]>([]);
  const [bindings, setBindings] = useState<NotificationBinding[]>([]);
  const [notifications, setNotifications] = useState<NotificationIntentItem[]>([]);
  const [capabilities, setCapabilities] = useState<CapabilityItem[]>([]);

  const csrfRef = useRef<string | null>(null);
  const authEpochRef = useRef(0);
  const taskEpochRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const taskAbortRef = useRef<AbortController | null>(null);

  const clearAllState = useCallback(() => {
    authEpochRef.current += 1;
    taskEpochRef.current += 1;
    abortRef.current?.abort();
    taskAbortRef.current?.abort();
    csrfRef.current = null;
    setSession(null);
    setTasks([]);
    setSelectedTaskId(null);
    setTaskDetail(null);
    setEventsText("");
    setSessions([]);
    setSelectedSessionId(null);
    setObservation(null);
    setTakeoverResult(null);
    setMemories([]);
    setRevisionsByMemory({});
    setConsents([]);
    setContextPolicy(null);
    setCampaigns([]);
    setSelectedCampaignId(null);
    setCampaignEventsText("");
    setWatches([]);
    setBindings([]);
    setNotifications([]);
    setCapabilities([]);
  }, []);

  const apiFetch = useMemo(
    () => createClientRequest(() => csrfRef.current, clearAllState),
    [clearAllState],
  );

  const loadTaskMetadata = useCallback(
    async (taskId: string, signal?: AbortSignal) => {
      const [detail, memRes, polRes] = await Promise.all([
        apiFetch<FullTaskView>(`/v1/tasks/${taskId}`, { signal }),
        apiFetch<{ items: MemoryItem[] }>(`/v1/tasks/${taskId}/memory`, { signal }).catch(() => ({ items: [] })),
        apiFetch<{ policy: ContextPolicyItem | null }>(`/v1/tasks/${taskId}/context-policy`, { signal }).catch(() => ({
          policy: null,
        })),
      ]);
      return { detail, memories: memRes.items, policy: polRes.policy };
    },
    [apiFetch],
  );

  const refreshWorkspace = useCallback(async () => {
    const myEpoch = authEpochRef.current + 1;
    authEpochRef.current = myEpoch;
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const isStale = () => authEpochRef.current !== myEpoch || ctrl.signal.aborted;

    const settled = await Promise.allSettled([
      apiFetch<{ items: TaskSummary[] }>("/v1/tasks", { signal: ctrl.signal }),
      apiFetch<{ items: BrowserSessionItem[] }>("/v1/browser/sessions", { signal: ctrl.signal }),
      apiFetch<{ items: CampaignItem[] }>("/v1/campaigns", { signal: ctrl.signal }),
      apiFetch<{ items: WatchItem[] }>("/v1/watches", { signal: ctrl.signal }),
      apiFetch<{ items: ConsentItem[] }>("/v1/memory/consents", { signal: ctrl.signal }),
      apiFetch<{ items: NotificationBinding[] }>("/v1/notifications/bindings", { signal: ctrl.signal }),
      apiFetch<{ items: NotificationIntentItem[] }>("/v1/notifications", { signal: ctrl.signal }),
      apiFetch<{ capabilities: CapabilityItem[] }>("/v1/step4/capabilities", { signal: ctrl.signal }),
    ]);
    if (isStale()) return;

    const [tRes, bRes, cRes, wRes, conRes, bindRes, notRes, capRes] = settled;
    if (tRes.status === "fulfilled") setTasks(tRes.value.items);
    if (bRes.status === "fulfilled") setSessions(bRes.value.items);
    if (cRes.status === "fulfilled") setCampaigns(cRes.value.items);
    if (wRes.status === "fulfilled") setWatches(wRes.value.items);
    if (conRes.status === "fulfilled") setConsents(conRes.value.items);
    if (bindRes.status === "fulfilled") setBindings(bindRes.value.items);
    if (notRes.status === "fulfilled") setNotifications(notRes.value.items);
    if (capRes.status === "fulfilled") setCapabilities(capRes.value.capabilities);

    const partialFailures = settled
      .filter((r): r is PromiseRejectedResult => r.status === "rejected")
      .map((r) => (r.reason instanceof ApiClientError ? `${r.reason.status} (${r.reason.code})` : String(r.reason)));
    if (partialFailures.length > 0) {
      setErrorMsg(`一部データ取得に失敗: ${partialFailures.join(", ")}`);
    }
  }, [apiFetch]);

  const loadTaskDetail = useCallback(
    async (taskId: string) => {
      const myAuthEpoch = authEpochRef.current;
      const myTaskEpoch = taskEpochRef.current + 1;
      taskEpochRef.current = myTaskEpoch;
      taskAbortRef.current?.abort();
      const ctrl = new AbortController();
      taskAbortRef.current = ctrl;
      const isStale = () =>
        authEpochRef.current !== myAuthEpoch || taskEpochRef.current !== myTaskEpoch || ctrl.signal.aborted;

      try {
        const [{ detail, memories: mems, policy }, sseRes] = await Promise.all([
          loadTaskMetadata(taskId, ctrl.signal),
          fetch(`/v1/tasks/${taskId}/events/stream?limit=100`, {
            credentials: "include",
            signal: ctrl.signal,
          }).then((r) => (r.ok ? r.text() : "")),
        ]);
        if (isStale()) return;
        setTaskDetail(detail);
        setMemories(mems);
        setContextPolicy(policy);
        setEventsText(sseRes);

        if (!["SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED", "BUDGET_EXHAUSTED"].includes(detail.state)) {
          const lines = sseRes.split("\n").filter((ln) => ln.startsWith("id: "));
          const lastOffset = lines.length > 0 ? Number(lines[lines.length - 1].slice(4).trim()) || 0 : 0;
          void (async () => {
            try {
              const streamResp = await fetch(
                `/v1/tasks/${taskId}/events/stream?after_offset=${lastOffset}&follow=true&max_polls=20`,
                { credentials: "include", signal: ctrl.signal },
              );
              if (!streamResp.ok || !streamResp.body || isStale()) return;
              const reader = streamResp.body.getReader();
              const decoder = new TextDecoder();
              while (true) {
                const { value, done } = await reader.read();
                if (done || isStale()) break;
                const chunk = decoder.decode(value, { stream: true });
                if (chunk) {
                  setEventsText((prev) => `${prev}${chunk}`);
                }
              }
            } catch {
              // Abort or stream close on task switch / terminal state
            }
          })();
        }
      } catch (err) {
        if (isStale()) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        setErrorMsg(err instanceof Error ? err.message : "タスク詳細の取得に失敗しました");
      }
    },
    [loadTaskMetadata],
  );

  // Restore HttpOnly cookie session transparently across page reloads
  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const info = await apiFetch<SessionInfo>("/v1/auth/session");
        if (!active || !info.authenticated) return;
        csrfRef.current = info.csrfToken ?? null;
        setSession(info);
        await refreshWorkspace();
      } catch {
        // 401 when unauthenticated on initial load is expected
      }
    })();
    return () => {
      active = false;
      taskAbortRef.current?.abort();
    };
  }, [apiFetch, refreshWorkspace]);

  function selectTask(taskId: string) {
    setSelectedTaskId(taskId);
    setTaskDetail(null);
    setEventsText("");
    setMemories([]);
    setRevisionsByMemory({});
    setContextPolicy(null);
    void loadTaskDetail(taskId);
  }

  async function runAction(label: string, fn: () => Promise<void>) {
    setBusy(true);
    setErrorMsg(null);
    try {
      await fn();
      setStatusMsg(label);
    } catch (err) {
      setErrorMsg(err instanceof ApiClientError ? `${err.status} (${err.code})` : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function handleLogin(token: string) {
    await runAction("Cookie セッションを開始しました。", async () => {
      const info = await apiFetch<SessionInfo>("/v1/auth/session", {
        method: "POST",
        body: JSON.stringify({ token }),
      });
      csrfRef.current = info.csrfToken ?? null;
      setSession(info);
      await refreshWorkspace();
    });
  }

  async function handleLogout() {
    setBusy(true);
    setErrorMsg(null);
    try {
      await apiFetch("/v1/auth/session", { method: "DELETE" });
    } catch {
      // clear UI even if already expired
    } finally {
      clearAllState();
      setStatusMsg("ログアウトしました。");
      setBusy(false);
    }
  }

  const detailMatchesSelected = Boolean(selectedTaskId && taskDetail && taskDetail.id === selectedTaskId);

  return (
    <section className="console" aria-label="Agent Console">
      <AuthSessionBar
        session={session}
        busy={busy}
        onLogin={handleLogin}
        onLogout={handleLogout}
        onRefresh={() => runAction("最新状態を取得しました。", refreshWorkspace)}
      />

      {statusMsg && (
        <div className="banner" data-testid="console-status-banner">
          {statusMsg}
        </div>
      )}
      {errorMsg && (
        <div className="banner banner-error" role="alert" data-testid="console-error-banner">
          {errorMsg}
        </div>
      )}

      <div className="subtabs" role="tablist" aria-label="Console パネル切替">
        {(
          [
            ["tasks", "Tasks・承認"],
            ["memory", "Memory・Policy"],
            ["browser", "Browser Takeover"],
            ["automation", "Automation・通知"],
          ] as Array<[SubTab, string]>
        ).map(([id, text]) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={subTab === id}
            className={subTab === id ? "subtab subtab-active" : "subtab"}
            onClick={() => setSubTab(id)}
            data-testid={`tab-${id}`}
          >
            {text}
          </button>
        ))}
      </div>

      {subTab === "tasks" && (
        <TaskWorkspace
          authenticated={Boolean(session?.authenticated)}
          busy={busy}
          tasks={tasks}
          selectedTaskId={selectedTaskId}
          taskDetail={taskDetail}
          detailMatchesSelected={detailMatchesSelected}
          eventsText={eventsText}
          onCreateTask={(goal, profile) =>
            runAction("タスクを投入しました。", async () => {
              const res = await apiFetch<{ taskId: string }>("/v1/tasks", {
                method: "POST",
                body: JSON.stringify({ schemaVersion: "1.0", goal, profile }),
              });
              await refreshWorkspace();
              selectTask(res.taskId);
            })
          }
          onSelectTask={selectTask}
          onControlTask={(action) =>
            runAction(`Task ${action} を実行しました。`, async () => {
              if (!selectedTaskId) return;
              await apiFetch(`/v1/tasks/${selectedTaskId}/${action}`, { method: "POST" });
              await refreshWorkspace();
              await loadTaskDetail(selectedTaskId);
            })
          }
          onSteerTask={(newGoal) =>
            runAction("Steer を送信しました。", async () => {
              if (!taskDetail) return;
              await apiFetch(`/v1/tasks/${taskDetail.id}/steer`, {
                method: "POST",
                body: JSON.stringify({
                  schemaVersion: "1.0",
                  goal: newGoal,
                  expectedGoalRevision: taskDetail.goalRevision,
                }),
              });
              await refreshWorkspace();
              await loadTaskDetail(taskDetail.id);
            })
          }
          onDecideApproval={(approval: ApprovalRow, decision: "APPROVED" | "REJECTED") =>
            runAction(`承認 ${decision} を記録しました。`, async () => {
              try {
                await apiFetch(`/v1/approvals/${approval.id}/decision`, {
                  method: "POST",
                  body: JSON.stringify({
                    schemaVersion: "1.0",
                    decision,
                    requestSha256: approval.requestSha256,
                    expectedRecordVersion: approval.recordVersion,
                  }),
                });
              } finally {
                if (selectedTaskId) await loadTaskDetail(selectedTaskId);
              }
            })
          }
        />
      )}

      {subTab === "memory" && (
        <MemoryAndPolicyPanel
          authenticated={Boolean(session?.authenticated)}
          busy={busy}
          selectedTaskId={selectedTaskId}
          memories={memories}
          revisionsByMemory={revisionsByMemory}
          consents={consents}
          contextPolicy={contextPolicy}
          onRevokeMemory={(memoryId) =>
            runAction("Memory を削除・Tombstone 記録しました。", async () => {
              if (!selectedTaskId) return;
              await apiFetch(`/v1/tasks/${selectedTaskId}/memory/${memoryId}`, { method: "DELETE" });
              await loadTaskDetail(selectedTaskId);
            })
          }
          onLoadRevisions={(memoryId) =>
            runAction("Memory 履歴を取得しました。", async () => {
              if (!selectedTaskId) return;
              const res = await apiFetch<{ items: MemoryRevisionItem[] }>(
                `/v1/tasks/${selectedTaskId}/memory/${memoryId}/revisions`,
              );
              setRevisionsByMemory((prev) => ({ ...prev, [memoryId]: res.items }));
            })
          }
          onGrantConsent={(recipient) =>
            runAction(`${recipient} への Memory 送信同意を付与しました。`, async () => {
              await apiFetch(`/v1/memory/consents/${encodeURIComponent(recipient)}`, { method: "POST" });
              await refreshWorkspace();
            })
          }
          onRevokeConsent={(recipient) =>
            runAction(`${recipient} への Memory 送信同意を撤回しました。`, async () => {
              await apiFetch(`/v1/memory/consents/${encodeURIComponent(recipient)}`, { method: "DELETE" });
              await refreshWorkspace();
            })
          }
          onSaveContextPolicy={(payload) =>
            runAction("Context Policy を更新しました。", async () => {
              if (!selectedTaskId) return;
              await apiFetch(`/v1/tasks/${selectedTaskId}/context-policy`, {
                method: "PUT",
                body: JSON.stringify({ schemaVersion: "1.0", ...payload }),
              });
              await loadTaskDetail(selectedTaskId);
            })
          }
        />
      )}

      {subTab === "browser" && (
        <BrowserTakeoverPanel
          authenticated={Boolean(session?.authenticated)}
          busy={busy}
          sessions={sessions}
          selectedSessionId={selectedSessionId}
          observation={observation}
          takeoverResult={takeoverResult}
          onSelectSession={async (sid) => {
            setSelectedSessionId(sid);
            setTakeoverResult(null);
            await runAction("Browser 観測を取得しました。", async () => {
              const obs = await apiFetch<BrowserObservation>(`/v1/browser/sessions/${sid}/observation`);
              setObservation(obs);
            });
          }}
          onStartTakeover={(sid, reason) =>
            runAction("Human Takeover を開始しました。", async () => {
              await apiFetch(`/v1/browser/sessions/${sid}/takeover/start`, {
                method: "POST",
                body: JSON.stringify({ reason }),
              });
              await refreshWorkspace();
              const obs = await apiFetch<BrowserObservation>(`/v1/browser/sessions/${sid}/observation`);
              setObservation(obs);
            })
          }
          onAuthenticate={(sid, payload) =>
            runAction("認証リクエストを実行しました。", async () => {
              const res = await apiFetch<{ authStatus: string; authenticated: boolean; observation: string }>(
                `/v1/browser/sessions/${sid}/takeover/authenticate`,
                {
                  method: "POST",
                  body: JSON.stringify(payload),
                },
              );
              setTakeoverResult(res);
              await refreshWorkspace();
            })
          }
          onCompleteTakeover={(sid) =>
            runAction("Human Takeover を完了しました（AGENT へ制御返還）。", async () => {
              await apiFetch(`/v1/browser/sessions/${sid}/takeover/complete`, { method: "POST" });
              await refreshWorkspace();
            })
          }
          onAbortTakeover={(sid) =>
            runAction("Browser セッションを破棄しました。", async () => {
              await apiFetch(`/v1/browser/sessions/${sid}/takeover/abort`, { method: "POST" });
              setObservation(null);
              await refreshWorkspace();
            })
          }
        />
      )}

      {subTab === "automation" && (
        <AutomationAndNotifyPanel
          authenticated={Boolean(session?.authenticated)}
          busy={busy}
          watches={watches}
          campaigns={campaigns}
          campaignEventsText={campaignEventsText}
          selectedCampaignId={selectedCampaignId}
          bindings={bindings}
          notifications={notifications}
          onCreateWatch={(payload) =>
            runAction("Watch を登録しました。", async () => {
              await apiFetch("/v1/watches", { method: "POST", body: JSON.stringify(payload) });
              await refreshWorkspace();
            })
          }
          onTriggerWatch={(watchId) =>
            runAction("Watch スロットを確定し通知 Outbox へ投入しました。", async () => {
              await apiFetch(`/v1/watches/${encodeURIComponent(watchId)}/trigger`, {
                method: "POST",
                body: JSON.stringify({ dispatch: true }),
              });
              await refreshWorkspace();
            })
          }
          onDeactivateWatch={(watchId) =>
            runAction("Watch を停止しました。", async () => {
              await apiFetch(`/v1/watches/${encodeURIComponent(watchId)}`, { method: "DELETE" });
              await refreshWorkspace();
            })
          }
          onCreateCampaign={(payload) =>
            runAction("Campaign を開始しました。", async () => {
              const res = await apiFetch<{ campaignId: string }>("/v1/campaigns", {
                method: "POST",
                body: JSON.stringify(payload),
              });
              await refreshWorkspace();
              setSelectedCampaignId(res.campaignId);
              const evRes = await fetch(`/v1/campaigns/${encodeURIComponent(res.campaignId)}/events/stream`, {
                credentials: "include",
              });
              setCampaignEventsText(evRes.ok ? await evRes.text() : "");
            })
          }
          onSelectCampaign={(cid) => {
            setSelectedCampaignId(cid);
            void fetch(`/v1/campaigns/${encodeURIComponent(cid)}/events/stream`, { credentials: "include" })
              .then((r) => (r.ok ? r.text() : ""))
              .then((t) => setCampaignEventsText(t))
              .catch(() => setCampaignEventsText(""));
          }}
          onControlCampaign={(cid, action, expectedCv) =>
            runAction(`Campaign ${action} を実行しました。`, async () => {
              await apiFetch(`/v1/campaigns/${encodeURIComponent(cid)}/${action}`, {
                method: "POST",
                body: JSON.stringify({ expectedControlVersion: expectedCv }),
              });
              await refreshWorkspace();
              const evRes = await fetch(`/v1/campaigns/${encodeURIComponent(cid)}/events/stream`, {
                credentials: "include",
              });
              setCampaignEventsText(evRes.ok ? await evRes.text() : "");
            })
          }
          onSteerCampaign={(cid, expectedCv, newGoalRev, newGoalText) =>
            runAction("Campaign Steer を実行しました。", async () => {
              await apiFetch(`/v1/campaigns/${encodeURIComponent(cid)}/steer`, {
                method: "POST",
                body: JSON.stringify({
                  expectedControlVersion: expectedCv,
                  newGoalRevision: newGoalRev,
                  newGoalText: newGoalText || undefined,
                }),
              });
              await refreshWorkspace();
            })
          }
          onCreateBinding={(payload) =>
            runAction("通知 Binding を暗号化保存しました。", async () => {
              await apiFetch("/v1/notifications/bindings", { method: "POST", body: JSON.stringify(payload) });
              await refreshWorkspace();
            })
          }
          onToggleBindingConsent={(bindingId, granted) =>
            runAction(granted ? "通知同意を付与しました。" : "通知同意を撤回しました。", async () => {
              await apiFetch(`/v1/notifications/bindings/${bindingId}/consent`, {
                method: "POST",
                body: JSON.stringify({ granted }),
              });
              await refreshWorkspace();
            })
          }
          onOptOutBinding={(bindingId) =>
            runAction("通知 Opt-Out を記録しました。", async () => {
              await apiFetch(`/v1/notifications/bindings/${bindingId}/opt-out`, { method: "POST" });
              await refreshWorkspace();
            })
          }
          onRevokeBinding={(bindingId) =>
            runAction("通知 Binding を失効させました。", async () => {
              await apiFetch(`/v1/notifications/bindings/${bindingId}`, { method: "DELETE" });
              await refreshWorkspace();
            })
          }
          onSendManualNotification={(payload) =>
            runAction("通知 Outbox 登録と配送を実行しました。", async () => {
              await apiFetch("/v1/notifications", { method: "POST", body: JSON.stringify(payload) });
              await refreshWorkspace();
            })
          }
          onDispatchNotifications={() =>
            runAction("保留中の通知 Outbox を配送しました。", async () => {
              await apiFetch("/v1/notifications/dispatch", { method: "POST" });
              await refreshWorkspace();
            })
          }
          onMarkRead={(notificationId) =>
            runAction("通知を既読 (READ) にしました。", async () => {
              await apiFetch(`/v1/notifications/${notificationId}/read`, { method: "POST" });
              await refreshWorkspace();
            })
          }
        />
      )}

      <CapabilitiesBanner capabilities={capabilities} />
    </section>
  );
}
