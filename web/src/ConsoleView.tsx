import { KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
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
  type DryRunPreview,
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
import ConfirmDialog, { type ConfirmRequest } from "./components/ConfirmDialog";
import MemoryAndPolicyPanel from "./components/MemoryAndPolicyPanel";
import TaskWorkspace from "./components/TaskWorkspace";

type SubTab = "tasks" | "memory" | "browser" | "automation";

const SUB_TABS: Array<[SubTab, string]> = [
  ["tasks", "Tasks・承認"],
  ["memory", "Memory・Policy"],
  ["browser", "Browser Takeover"],
  ["automation", "Automation・通知"],
];

const TERMINAL_TASK_STATES = new Set([
  "SUCCEEDED",
  "FAILED",
  "CANCELLED",
  "EXPIRED",
  "BUDGET_EXHAUSTED",
]);

function consumeSseFrames(
  chunk: string,
  state: { buf: string; lastOffset: number },
  seenOffsets: Set<number>,
  linesOut: string[],
  flushTrailing = false,
): number {
  state.buf += chunk;
  const parts = state.buf.split(/\r?\n\r?\n/);
  if (flushTrailing) {
    state.buf = "";
  } else {
    state.buf = parts.pop() ?? "";
  }
  let added = 0;
  for (const frame of parts) {
    const trimmed = frame.trim();
    if (!trimmed) continue;
    let idStr = "";
    let eventKind = "";
    const dataLines: string[] = [];
    for (const ln of trimmed.split(/\r?\n/)) {
      if (ln.startsWith("id:")) {
        idStr = ln.slice(3).trim();
      } else if (ln.startsWith("event:")) {
        eventKind = ln.slice(6).trim();
      } else if (ln.startsWith("data:")) {
        dataLines.push(ln.slice(5).trim());
      }
    }
    const rawData = dataLines.join("\n");
    let offset = Number(idStr);
    let displayLine = trimmed;
    if (rawData) {
      try {
        const parsed = JSON.parse(rawData) as {
          offset?: number;
          kind?: string;
          occurredAt?: string;
          payload?: unknown;
        };
        if (typeof parsed.offset === "number" && Number.isFinite(parsed.offset)) {
          offset = parsed.offset;
        }
        const kindLabel = parsed.kind || eventKind || "event";
        const tsLabel = parsed.occurredAt ? ` (${parsed.occurredAt})` : "";
        displayLine = `#${Number.isFinite(offset) ? offset : "?"} [${kindLabel}]${tsLabel} ${JSON.stringify(parsed.payload ?? parsed)}`;
      } catch {
        displayLine = eventKind ? `[${eventKind}] ${rawData}` : rawData;
      }
    }
    if (Number.isFinite(offset) && offset > 0) {
      if (seenOffsets.has(offset)) continue;
      seenOffsets.add(offset);
      state.lastOffset = Math.max(state.lastOffset, offset);
    }
    linesOut.push(displayLine);
    added += 1;
  }
  return added;
}

export default function ConsoleView() {
  const [subTab, setSubTab] = useState<SubTab>("tasks");
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [confirmReq, setConfirmReq] = useState<ConfirmRequest | null>(null);

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
  const [watchPreview, setWatchPreview] = useState<DryRunPreview | null>(null);
  const [manualPreview, setManualPreview] = useState<DryRunPreview | null>(null);
  const [capabilities, setCapabilities] = useState<CapabilityItem[]>([]);

  const csrfRef = useRef<string | null>(null);
  const authEpochRef = useRef(0);
  const workspaceEpochRef = useRef(0);
  const taskEpochRef = useRef(0);
  const browserEpochRef = useRef(0);
  const campaignEpochRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const taskAbortRef = useRef<AbortController | null>(null);
  const browserAbortRef = useRef<AbortController | null>(null);
  const campaignAbortRef = useRef<AbortController | null>(null);
  const consoleBodyRef = useRef<HTMLDivElement | null>(null);
  const tabRefs = useRef<Record<SubTab, HTMLButtonElement | null>>({
    tasks: null,
    memory: null,
    browser: null,
    automation: null,
  });

  const clearAllState = useCallback(() => {
    authEpochRef.current += 1;
    workspaceEpochRef.current += 1;
    taskEpochRef.current += 1;
    browserEpochRef.current += 1;
    campaignEpochRef.current += 1;
    abortRef.current?.abort();
    taskAbortRef.current?.abort();
    browserAbortRef.current?.abort();
    campaignAbortRef.current?.abort();
    csrfRef.current = null;
    setConfirmReq(null);
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
    setWatchPreview(null);
    setManualPreview(null);
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
    const myAuthEpoch = authEpochRef.current;
    const myWorkspaceEpoch = workspaceEpochRef.current + 1;
    workspaceEpochRef.current = myWorkspaceEpoch;
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const isStale = () =>
      authEpochRef.current !== myAuthEpoch ||
      workspaceEpochRef.current !== myWorkspaceEpoch ||
      ctrl.signal.aborted;

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
        const [{ detail, memories: mems, policy }, initialSse] = await Promise.all([
          loadTaskMetadata(taskId, ctrl.signal),
          fetch(`/v1/tasks/${taskId}/events/stream?limit=100`, {
            credentials: "include",
            signal: ctrl.signal,
          }).then((r) => (r.ok ? r.text() : "")),
        ]);
        if (isStale()) return;

        const seenOffsets = new Set<number>();
        const formattedLines: string[] = [];
        const sseState = { buf: "", lastOffset: 0 };
        consumeSseFrames(initialSse, sseState, seenOffsets, formattedLines, true);

        setTaskDetail(detail);
        setMemories(mems);
        setContextPolicy(policy);
        setEventsText(formattedLines.join("\n"));

        let currentState: string = detail.state;
        if (!TERMINAL_TASK_STATES.has(currentState)) {
          void (async () => {
            let debounceTimer: ReturnType<typeof setTimeout> | null = null;
            const refreshLive = async () => {
              if (isStale()) return;
              try {
                const [meta, listRes] = await Promise.all([
                  loadTaskMetadata(taskId, ctrl.signal),
                  apiFetch<{ items: TaskSummary[] }>("/v1/tasks", { signal: ctrl.signal }).catch(() => null),
                ]);
                if (isStale()) return;
                currentState = meta.detail.state;
                setTaskDetail(meta.detail);
                setMemories(meta.memories);
                setContextPolicy(meta.policy);
                if (listRes) setTasks(listRes.items);
              } catch {
                // Ignore transient poll errors unless persistent
              }
            };
            const scheduleRefresh = () => {
              if (debounceTimer) return;
              debounceTimer = setTimeout(() => {
                debounceTimer = null;
                void refreshLive();
              }, 200);
            };
            try {
              let consecutiveFailures = 0;
              while (!isStale() && !TERMINAL_TASK_STATES.has(currentState)) {
                try {
                  const streamResp = await fetch(
                    `/v1/tasks/${taskId}/events/stream?after_offset=${sseState.lastOffset}&follow=true&max_polls=20`,
                    { credentials: "include", signal: ctrl.signal },
                  );
                  if (isStale()) break;
                  if (!streamResp.ok || !streamResp.body) {
                    consecutiveFailures += 1;
                    if (consecutiveFailures >= 5) break;
                    await new Promise((resolve) => setTimeout(resolve, 400));
                    continue;
                  }
                  consecutiveFailures = 0;
                  const reader = streamResp.body.getReader();
                  const decoder = new TextDecoder();
                  while (true) {
                    const { value, done } = await reader.read();
                    if (done || isStale()) break;
                    const chunk = decoder.decode(value, { stream: true });
                    if (chunk) {
                      const added = consumeSseFrames(chunk, sseState, seenOffsets, formattedLines);
                      if (added > 0) {
                        setEventsText(formattedLines.join("\n"));
                        scheduleRefresh();
                      }
                    }
                  }
                  const tailAdded = consumeSseFrames(decoder.decode(), sseState, seenOffsets, formattedLines, true);
                  if (tailAdded > 0 && !isStale()) {
                    setEventsText(formattedLines.join("\n"));
                  }
                  if (debounceTimer) {
                    clearTimeout(debounceTimer);
                    debounceTimer = null;
                  }
                  await refreshLive();
                  if (!isStale() && !TERMINAL_TASK_STATES.has(currentState)) {
                    await new Promise((resolve) => setTimeout(resolve, 250));
                  }
                } catch (streamErr) {
                  if (isStale() || (streamErr instanceof DOMException && streamErr.name === "AbortError")) break;
                  consecutiveFailures += 1;
                  if (consecutiveFailures >= 5) break;
                  await new Promise((resolve) => setTimeout(resolve, 400));
                }
              }
            } finally {
              if (debounceTimer) clearTimeout(debounceTimer);
            }
          })();
        }
      } catch (err) {
        if (isStale()) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        setErrorMsg(err instanceof Error ? err.message : "タスク詳細の取得に失敗しました");
      }
    },
    [apiFetch, loadTaskMetadata],
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
      browserAbortRef.current?.abort();
      campaignAbortRef.current?.abort();
    };
  }, [apiFetch, refreshWorkspace]);

  useEffect(() => {
    const el = consoleBodyRef.current;
    if (!el) return;
    if (confirmReq) {
      el.setAttribute("inert", "");
    } else {
      el.removeAttribute("inert");
    }
  }, [confirmReq]);

  const loadCampaignEvents = useCallback(async (cid: string) => {
    const myAuthEpoch = authEpochRef.current;
    const myCampaignEpoch = campaignEpochRef.current + 1;
    campaignEpochRef.current = myCampaignEpoch;
    campaignAbortRef.current?.abort();
    const ctrl = new AbortController();
    campaignAbortRef.current = ctrl;
    const isStale = () =>
      authEpochRef.current !== myAuthEpoch ||
      campaignEpochRef.current !== myCampaignEpoch ||
      ctrl.signal.aborted;

    setSelectedCampaignId(cid);
    setCampaignEventsText("");
    try {
      const evRes = await fetch(`/v1/campaigns/${encodeURIComponent(cid)}/events/stream`, {
        credentials: "include",
        signal: ctrl.signal,
      });
      if (isStale()) return;
      const text = evRes.ok ? await evRes.text() : "";
      if (isStale()) return;
      setCampaignEventsText(text);
    } catch {
      if (!isStale()) setCampaignEventsText("");
    }
  }, []);

  function selectTask(taskId: string) {
    setSelectedTaskId(taskId);
    setTaskDetail(null);
    setEventsText("");
    setMemories([]);
    setRevisionsByMemory({});
    setContextPolicy(null);
    void loadTaskDetail(taskId);
  }

  async function runAction(label: string, fn: () => Promise<void>): Promise<boolean> {
    setBusy(true);
    setErrorMsg(null);
    setStatusMsg(null);
    try {
      await fn();
      setStatusMsg(label);
      return true;
    } catch (err) {
      setStatusMsg(null);
      setErrorMsg(err instanceof ApiClientError ? `${err.status} (${err.code})` : String(err));
      return false;
    } finally {
      setBusy(false);
    }
  }

  function askConfirmation(
    spec: Omit<ConfirmRequest, "onConfirm">,
    onConfirmedAction: () => Promise<boolean>,
  ): Promise<boolean> {
    return new Promise((resolve) => {
      let settled = false;
      setConfirmReq({
        ...spec,
        cancelLabel: spec.cancelLabel ?? "キャンセル",
        onConfirm: async () => {
          if (settled) return;
          settled = true;
          setConfirmReq(null);
          const ok = await onConfirmedAction();
          resolve(ok);
        },
      });
      // Replace close handler behavior via wrapper when closing modal
      confirmResolveRef.current = () => {
        if (settled) return;
        settled = true;
        setConfirmReq(null);
        resolve(false);
      };
    });
  }

  const confirmResolveRef = useRef<() => void>(() => setConfirmReq(null));

  async function handleLogin(token: string): Promise<boolean> {
    return runAction("Cookie セッションを開始しました。", async () => {
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
    setStatusMsg(null);
    try {
      await apiFetch("/v1/auth/session", { method: "DELETE" });
      clearAllState();
      setStatusMsg("ログアウトしました。");
    } catch (err) {
      if (err instanceof ApiClientError && err.status === 401) {
        clearAllState();
        setStatusMsg("セッションは既に失効していました。ログアウトしました。");
      } else {
        setErrorMsg(
          `サーバー側のセッション失効に失敗しました（状態は維持されています）: ${
            err instanceof ApiClientError ? `${err.status} (${err.code})` : String(err)
          }`,
        );
      }
    } finally {
      setBusy(false);
    }
  }

  function handleTabKeyDown(e: KeyboardEvent<HTMLButtonElement>, idx: number) {
    let nextIdx = idx;
    if (e.key === "ArrowRight") nextIdx = (idx + 1) % SUB_TABS.length;
    else if (e.key === "ArrowLeft") nextIdx = (idx - 1 + SUB_TABS.length) % SUB_TABS.length;
    else if (e.key === "Home") nextIdx = 0;
    else if (e.key === "End") nextIdx = SUB_TABS.length - 1;
    else return;
    e.preventDefault();
    const [nextTab] = SUB_TABS[nextIdx];
    setSubTab(nextTab);
    tabRefs.current[nextTab]?.focus();
  }

  const detailMatchesSelected = Boolean(selectedTaskId && taskDetail && taskDetail.id === selectedTaskId);

  return (
    <section className="console" aria-label="Agent Console">
      <div className="console" ref={consoleBodyRef}>
        <AuthSessionBar
        session={session}
        busy={busy}
        onLogin={handleLogin}
        onLogout={handleLogout}
        onRefresh={() => runAction("最新状態を取得しました。", refreshWorkspace)}
      />

      {statusMsg && (
        <div className="banner" role="status" aria-live="polite" data-testid="console-status-banner">
          {statusMsg}
        </div>
      )}
      {errorMsg && (
        <div className="banner banner-error" role="alert" data-testid="console-error-banner">
          {errorMsg}
        </div>
      )}

      <div className="subtabs" role="tablist" aria-label="Console パネル切替">
        {SUB_TABS.map(([id, text], idx) => (
          <button
            key={id}
            ref={(el) => {
              tabRefs.current[id] = el;
            }}
            id={`tab-${id}`}
            type="button"
            role="tab"
            aria-selected={subTab === id}
            aria-controls={`tabpanel-${id}`}
            tabIndex={subTab === id ? 0 : -1}
            className={subTab === id ? "subtab subtab-active" : "subtab"}
            onClick={() => setSubTab(id)}
            onKeyDown={(e) => handleTabKeyDown(e, idx)}
            data-testid={`tab-${id}`}
          >
            {text}
          </button>
        ))}
      </div>

      <div role="tabpanel" id={`tabpanel-${subTab}`} aria-labelledby={`tab-${subTab}`} tabIndex={0}>
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
            onControlTask={(action) => {
              if (!selectedTaskId) return Promise.resolve(false);
              const exec = () =>
                runAction(`Task ${action} を実行しました。`, async () => {
                  await apiFetch(`/v1/tasks/${selectedTaskId}/${action}`, { method: "POST" });
                  await refreshWorkspace();
                  await loadTaskDetail(selectedTaskId);
                });
              if (action === "cancel") {
                return askConfirmation(
                  {
                    title: "Task キャンセルの確認",
                    summary: `Task ${selectedTaskId.slice(0, 8)} をキャンセルします。実行中処理の再開はできません。`,
                    confirmLabel: "キャンセルを実行",
                    danger: true,
                  },
                  exec,
                );
              }
              return exec();
            }}
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
              askConfirmation(
                {
                  title: decision === "APPROVED" ? "外部作用・実行承認 (APPROVE)" : "承認リクエスト拒否 (REJECT)",
                  summary:
                    decision === "APPROVED"
                      ? "以下の外部作用または高権限ツール実行を承認します。"
                      : "以下のリクエストを拒否します。",
                  details: [
                    { label: "ツール", value: approval.tool },
                    { label: "内容要約", value: approval.summary },
                    { label: "SHA-256", value: approval.requestSha256 },
                    { label: "Goal / Ver", value: `rev=${approval.goalRevision} / ver=${approval.recordVersion}` },
                  ],
                  warning:
                    decision === "APPROVED"
                      ? "承認後の外部送信・決済・確定操作は原則として自動取り消しができません。"
                      : undefined,
                  confirmLabel: decision === "APPROVED" ? "承認を確定" : "拒否を確定",
                  danger: decision === "REJECTED",
                },
                () =>
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
                  }),
              )
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
              askConfirmation(
                {
                  title: "Memory 削除（復元時再適用防止）",
                  summary: "選択した記憶項目を削除し、バックアップ復元時も再適用されないよう削除履歴を記録します。",
                  details: [{ label: "Memory ID", value: memoryId }],
                  warning: "削除履歴の記録後は同じ内容を復元しても利用できなくなります。",
                  confirmLabel: "削除を確定",
                  danger: true,
                },
                () =>
                  runAction("記憶を削除し復元時再適用を防止しました。", async () => {
                    if (!selectedTaskId) return;
                    await apiFetch(`/v1/tasks/${selectedTaskId}/memory/${memoryId}`, { method: "DELETE" });
                    await loadTaskDetail(selectedTaskId);
                  }),
              )
            }
            onLoadRevisions={(memoryId) =>
              runAction("記憶履歴を取得しました。", async () => {
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
              const myAuthEpoch = authEpochRef.current;
              const myBrowserEpoch = browserEpochRef.current + 1;
              browserEpochRef.current = myBrowserEpoch;
              browserAbortRef.current?.abort();
              const ctrl = new AbortController();
              browserAbortRef.current = ctrl;
              const isBrowserStale = () =>
                authEpochRef.current !== myAuthEpoch ||
                browserEpochRef.current !== myBrowserEpoch ||
                ctrl.signal.aborted;

              setSelectedSessionId(sid);
              setObservation(null);
              setTakeoverResult(null);
              setBusy(true);
              setErrorMsg(null);
              setStatusMsg(null);
              try {
                const obs = await apiFetch<BrowserObservation>(`/v1/browser/sessions/${sid}/observation`, {
                  signal: ctrl.signal,
                });
                if (isBrowserStale()) return false;
                setObservation(obs);
                setStatusMsg("Browser 観測を取得しました。");
                return true;
              } catch (err) {
                if (isBrowserStale()) return false;
                if (err instanceof DOMException && err.name === "AbortError") return false;
                setErrorMsg(err instanceof ApiClientError ? `${err.status} (${err.code})` : String(err));
                return false;
              } finally {
                if (browserEpochRef.current === myBrowserEpoch) {
                  setBusy(false);
                }
              }
            }}
            onStartTakeover={(sid, reason) => {
              const myAuthEpoch = authEpochRef.current;
              const myBrowserEpoch = browserEpochRef.current;
              return runAction("Human Takeover を開始しました。", async () => {
                await apiFetch(`/v1/browser/sessions/${sid}/takeover/start`, {
                  method: "POST",
                  body: JSON.stringify({ reason }),
                });
                await refreshWorkspace();
                const obs = await apiFetch<BrowserObservation>(`/v1/browser/sessions/${sid}/observation`);
                if (authEpochRef.current === myAuthEpoch && browserEpochRef.current === myBrowserEpoch) {
                  setObservation(obs);
                }
              });
            }}
            onAuthenticate={(sid, payload) => {
              const myAuthEpoch = authEpochRef.current;
              const myBrowserEpoch = browserEpochRef.current;
              return runAction("認証リクエストを実行しました。", async () => {
                const res = await apiFetch<{ authStatus: string; authenticated: boolean; observation: string }>(
                  `/v1/browser/sessions/${sid}/takeover/authenticate`,
                  {
                    method: "POST",
                    body: JSON.stringify(payload),
                  },
                );
                if (authEpochRef.current === myAuthEpoch && browserEpochRef.current === myBrowserEpoch) {
                  setTakeoverResult(res);
                }
                await refreshWorkspace();
              });
            }}
            onCompleteTakeover={(sid) =>
              runAction("Human Takeover を完了しました（AGENT へ制御返還）。", async () => {
                await apiFetch(`/v1/browser/sessions/${sid}/takeover/complete`, { method: "POST" });
                await refreshWorkspace();
              })
            }
            onAbortTakeover={(sid) =>
              askConfirmation(
                {
                  title: "Browser セッション破棄 (Abort)",
                  summary: `Browser Session ${sid.slice(0, 8)} を破棄し、関連する Takeover 状態を終了します。`,
                  details: [{ label: "Session ID", value: sid }],
                  warning: "破棄した Browser セッションは再開できません。",
                  confirmLabel: "セッション破棄を確定",
                  danger: true,
                },
                () =>
                  runAction("Browser セッションを破棄しました。", async () => {
                    await apiFetch(`/v1/browser/sessions/${sid}/takeover/abort`, { method: "POST" });
                    setObservation(null);
                    setTakeoverResult(null);
                    await refreshWorkspace();
                  }),
              )
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
            watchPreview={watchPreview}
            manualPreview={manualPreview}
            onCreateWatch={(payload) =>
              runAction("Watch を登録しました。", async () => {
                await apiFetch("/v1/watches", { method: "POST", body: JSON.stringify(payload) });
                await refreshWorkspace();
              })
            }
            onDryRunWatch={(watchId, target) =>
              runAction("Watch Dry-Run プレビューを取得しました（DB・外部送信なし）。", async () => {
                const res = await apiFetch<{ preview: DryRunPreview }>(
                  `/v1/watches/${encodeURIComponent(watchId)}/trigger`,
                  {
                    method: "POST",
                    body: JSON.stringify({ ...target, dryRun: true }),
                  },
                );
                setWatchPreview(res.preview);
              })
            }
            onTriggerWatch={(watchId, target) => {
              const targetDesc = target.allConsentedBindings
                ? "同意済み全 Binding"
                : (() => {
                    const found = bindings.find((b) => b.id === target.bindingId);
                    return found ? `${found.label} (${found.redactedTarget})` : (target.bindingId ?? "-");
                  })();
              return askConfirmation(
                {
                  title: "Watch 通知の実送信確認",
                  summary: "Watch スロットを消費して外部通知 Outbox へ登録し、実送信を実行します。",
                  details: [
                    { label: "Watch ID", value: watchId },
                    { label: "送信先", value: targetDesc },
                  ],
                  warning: "実送信後は外部サーバーへの通知取り消しができません。",
                  confirmLabel: "実際に送信する",
                },
                () =>
                  runAction("Watch スロットを確定し通知 Outbox へ投入しました。", async () => {
                    await apiFetch(`/v1/watches/${encodeURIComponent(watchId)}/trigger`, {
                      method: "POST",
                      body: JSON.stringify({ ...target, dryRun: false, dispatch: true }),
                    });
                    setWatchPreview(null);
                    await refreshWorkspace();
                  }),
              );
            }}
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
                await loadCampaignEvents(res.campaignId);
              })
            }
            onSelectCampaign={(cid) => {
              void loadCampaignEvents(cid);
            }}
            onControlCampaign={(cid, action, expectedCv) =>
              runAction(`Campaign ${action} を実行しました。`, async () => {
                await apiFetch(`/v1/campaigns/${encodeURIComponent(cid)}/${action}`, {
                  method: "POST",
                  body: JSON.stringify({ expectedControlVersion: expectedCv }),
                });
                await refreshWorkspace();
                await loadCampaignEvents(cid);
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
            onOptOutBinding={(bindingId) => {
              const b = bindings.find((x) => x.id === bindingId);
              return askConfirmation(
                {
                  title: "通知 Binding Opt-Out 確認",
                  summary: "この通知 Binding を停止し、未配送の Outbox 通知をすべて抑止 (SUPPRESSED) します。",
                  details: [
                    { label: "Binding", value: b ? `${b.label} [${b.channel}]` : bindingId },
                    { label: "宛先", value: b?.redactedTarget ?? "-" },
                  ],
                  confirmLabel: "Opt-Out を確定",
                  danger: true,
                },
                () =>
                  runAction("通知 Opt-Out を記録しました。", async () => {
                    await apiFetch(`/v1/notifications/bindings/${bindingId}/opt-out`, { method: "POST" });
                    await refreshWorkspace();
                  }),
              );
            }}
            onRevokeBinding={(bindingId) => {
              const b = bindings.find((x) => x.id === bindingId);
              return askConfirmation(
                {
                  title: "通知 Binding 完全失効確認",
                  summary: "この通知 Binding を永久に失効させ、未配送の通知を抑止します。",
                  details: [
                    { label: "Binding", value: b ? `${b.label} [${b.channel}]` : bindingId },
                    { label: "宛先", value: b?.redactedTarget ?? "-" },
                  ],
                  warning: "失効 (Revoke) した Binding は再有効化できません。",
                  confirmLabel: "失効を確定",
                  danger: true,
                },
                () =>
                  runAction("通知 Binding を失効させました。", async () => {
                    await apiFetch(`/v1/notifications/bindings/${bindingId}`, { method: "DELETE" });
                    await refreshWorkspace();
                  }),
              );
            }}
            onDryRunNotification={(payload) =>
              runAction("通知 Dry-Run プレビューを取得しました（DB・外部送信なし）。", async () => {
                const res = await apiFetch<{ preview: DryRunPreview }>("/v1/notifications", {
                  method: "POST",
                  body: JSON.stringify({ ...payload, dryRun: true }),
                });
                setManualPreview(res.preview);
              })
            }
            onSendManualNotification={(payload) => {
              const b = bindings.find((x) => x.id === payload.bindingId);
              return askConfirmation(
                {
                  title: "外部通知の実送信確認",
                  summary: "以下の設定で外部通知 Outbox に登録し、直ちに送信を実行します。",
                  details: [
                    { label: "送信先", value: b ? `${b.label} [${b.channel}] (${b.redactedTarget})` : payload.bindingId },
                    { label: "タイトル", value: payload.title },
                    { label: "優先度", value: payload.priority },
                  ],
                  warning: "外部チャネルへ実送信されます。内容と宛先に誤りがないか確認してください。",
                  confirmLabel: "実際に送信する",
                },
                () =>
                  runAction("通知 Outbox 登録と配送を実行しました。", async () => {
                    await apiFetch("/v1/notifications", {
                      method: "POST",
                      body: JSON.stringify({ ...payload, dryRun: false }),
                    });
                    setManualPreview(null);
                    await refreshWorkspace();
                  }),
              );
            }}
            onDispatchNotifications={async () => {
              setBusy(true);
              setErrorMsg(null);
              setStatusMsg(null);
              let preview: {
                pendingCount: number;
                items: Array<{
                  intentId: string;
                  label: string;
                  channel: string;
                  redactedTarget: string;
                  title: string;
                  priority: string;
                }>;
              };
              try {
                const res = await apiFetch<{ preview: typeof preview }>("/v1/notifications/dispatch", {
                  method: "POST",
                  body: JSON.stringify({ dryRun: true }),
                });
                preview = res.preview;
              } catch (err) {
                setErrorMsg(err instanceof ApiClientError ? `${err.status} (${err.code})` : String(err));
                return false;
              } finally {
                setBusy(false);
              }
              if (preview.pendingCount === 0) {
                setStatusMsg("現在、配送待ちの保留 Outbox 通知はありません (0 件)。");
                return true;
              }
              const detailRows: Array<{ label: string; value: string }> = [
                { label: "対象件数", value: `${preview.pendingCount} 件` },
                ...preview.items.slice(0, 8).map((it, idx) => ({
                  label: `宛先 ${idx + 1}`,
                  value: `${it.label} [${it.channel}] (${it.redactedTarget}) — ${it.title} (${it.priority})`,
                })),
              ];
              if (preview.items.length > 8) {
                detailRows.push({ label: "ほか", value: `${preview.items.length - 8} 件` });
              }
              return askConfirmation(
                {
                  title: "保留 Outbox 一括配送の確認",
                  summary: `保留中の外部通知 ${preview.pendingCount} 件を以下の宛先へ一括送信します。`,
                  details: detailRows,
                  warning: "実送信後は外部サーバーへの通知取り消しができません。対象と宛先を確認してください。",
                  confirmLabel: `${preview.pendingCount} 件を実際に送信する`,
                },
                () =>
                  runAction("保留中の通知 Outbox を配送しました。", async () => {
                    await apiFetch("/v1/notifications/dispatch", {
                      method: "POST",
                      body: JSON.stringify({ dryRun: false }),
                    });
                    await refreshWorkspace();
                  }),
              );
            }}
            onMarkRead={(notificationId) =>
              runAction("通知を既読 (READ) にしました。", async () => {
                await apiFetch(`/v1/notifications/${notificationId}/read`, { method: "POST" });
                await refreshWorkspace();
              })
            }
          />
        )}
        </div>

        <CapabilitiesBanner capabilities={capabilities} />
      </div>
      <ConfirmDialog request={confirmReq} busy={busy} onClose={() => confirmResolveRef.current()} />
    </section>
  );
}
