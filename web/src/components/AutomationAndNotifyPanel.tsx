import { FormEvent, useState } from "react";
import type {
  CampaignItem,
  DryRunPreview,
  NotificationBinding,
  NotificationIntentItem,
  WatchItem,
} from "../api/client";

const NOTIFY_STATUS_LABELS: Record<string, string> = {
  QUEUED: "送信待機中 (QUEUED)",
  DEFERRED_QUIET_HOURS: "おやすみ時間待機 (DEFERRED_QUIET_HOURS)",
  SENDING: "送信中 (SENDING)",
  PROVIDER_ACCEPTED: "配信先サーバー受理済み（ユーザー未確認）",
  READ: "ユーザー確認済み (READ)",
  DELIVERY_UNKNOWN: "到達不明・自動再送なし（要手動確認）",
  FAILED: "送信失敗 (FAILED)",
  SUPPRESSED: "同意無効・失効により抑止 (SUPPRESSED)",
};

type WatchTarget = { bindingId?: string; allConsentedBindings?: boolean };

type Props = {
  authenticated: boolean;
  busy: boolean;
  watches: WatchItem[];
  campaigns: CampaignItem[];
  campaignEventsText: string;
  selectedCampaignId: string | null;
  bindings: NotificationBinding[];
  notifications: NotificationIntentItem[];
  watchPreview: DryRunPreview | null;
  manualPreview: DryRunPreview | null;
  onCreateWatch: (payload: {
    watchId?: string;
    timezoneId: string;
    localHour: number;
    localMinute: number;
    maxOccurrences: number;
  }) => Promise<boolean>;
  onDryRunWatch: (watchId: string, target: WatchTarget) => Promise<boolean>;
  onTriggerWatch: (watchId: string, target: WatchTarget) => Promise<boolean>;
  onDeactivateWatch: (watchId: string) => Promise<boolean>;
  onCreateCampaign: (payload: {
    goalText: string;
    profile: "default" | "browser" | "sandbox" | "anthropic";
    maxCycles: number;
    tokensPerCycle: number;
    toolsPerCycle: number;
  }) => Promise<boolean>;
  onSelectCampaign: (campaignId: string) => void;
  onControlCampaign: (campaignId: string, action: "pause" | "resume" | "stop", expectedCv: number) => Promise<boolean>;
  onSteerCampaign: (campaignId: string, expectedCv: number, newGoalRev: string, newGoalText: string) => Promise<boolean>;
  onCreateBinding: (payload: {
    channel: "webhook" | "telegram";
    label: string;
    destination: Record<string, string>;
    consentGranted: boolean;
    timezoneId: string;
    quietStartHour?: number;
    quietEndHour?: number;
  }) => Promise<boolean>;
  onToggleBindingConsent: (bindingId: string, granted: boolean) => Promise<boolean>;
  onOptOutBinding: (bindingId: string) => Promise<boolean>;
  onRevokeBinding: (bindingId: string) => Promise<boolean>;
  onDryRunNotification: (payload: {
    bindingId: string;
    title: string;
    summaryText: string;
    priority: "critical" | "standard" | "low";
  }) => Promise<boolean>;
  onSendManualNotification: (payload: {
    bindingId: string;
    title: string;
    summaryText: string;
    priority: "critical" | "standard" | "low";
    dispatch: boolean;
  }) => Promise<boolean>;
  onDispatchNotifications: () => Promise<boolean>;
  onMarkRead: (notificationId: string) => Promise<boolean>;
};

export default function AutomationAndNotifyPanel({
  authenticated,
  busy,
  watches,
  campaigns,
  campaignEventsText,
  selectedCampaignId,
  bindings,
  notifications,
  watchPreview,
  manualPreview,
  onCreateWatch,
  onDryRunWatch,
  onTriggerWatch,
  onDeactivateWatch,
  onCreateCampaign,
  onSelectCampaign,
  onControlCampaign,
  onSteerCampaign,
  onCreateBinding,
  onToggleBindingConsent,
  onOptOutBinding,
  onRevokeBinding,
  onDryRunNotification,
  onSendManualNotification,
  onDispatchNotifications,
  onMarkRead,
}: Props) {
  // Watch state
  const [watchTz, setWatchTz] = useState("Asia/Tokyo");
  const [watchHour, setWatchHour] = useState(9);
  const [watchMin, setWatchMin] = useState(0);
  const [watchMax, setWatchMax] = useState(5);
  const [watchBindingSel, setWatchBindingSel] = useState("");

  // Campaign state
  const [campGoal, setCampGoal] = useState("");
  const [campProfile, setCampProfile] = useState<"default" | "browser" | "sandbox" | "anthropic">("default");
  const [campCycles, setCampCycles] = useState(3);
  const [campTools, setCampTools] = useState(8);
  const [steerRev, setSteerRev] = useState("2");
  const [steerText, setSteerText] = useState("");

  // Binding state
  const [channel, setChannel] = useState<"webhook" | "telegram">("webhook");
  const [label, setLabel] = useState("");
  const [webhookUrl, setWebhookUrl] = useState("");
  const [tgToken, setTgToken] = useState("");
  const [tgChat, setTgChat] = useState("");
  const [tgApiBase, setTgApiBase] = useState("https://api.telegram.org");
  const [consentInit, setConsentInit] = useState(true);
  const [notifyTz, setNotifyTz] = useState("UTC");

  // Manual notification state (NO implicit fallback; explicit selection required)
  const [notifyBindingId, setNotifyBindingId] = useState("");
  const [notifyTitle, setNotifyTitle] = useState("");
  const [notifyBody, setNotifyBody] = useState("");
  const [notifyPriority, setNotifyPriority] = useState<"critical" | "standard" | "low">("standard");

  const selectedCamp = campaigns.find((c) => c.id === selectedCampaignId) ?? null;
  const activeBindings = bindings.filter((b) => b.consentGranted && !b.optedOutAt && !b.revokedAt);
  const selectedManualBinding = activeBindings.find((b) => b.id === notifyBindingId) ?? null;
  const watchTarget: WatchTarget | null =
    watchBindingSel === "__ALL__"
      ? activeBindings.length > 0
        ? { allConsentedBindings: true }
        : null
      : activeBindings.some((b) => b.id === watchBindingSel)
        ? { bindingId: watchBindingSel }
        : null;

  async function handleWatchSubmit(e: FormEvent) {
    e.preventDefault();
    await onCreateWatch({
      timezoneId: watchTz.trim() || "UTC",
      localHour: Number(watchHour),
      localMinute: Number(watchMin),
      maxOccurrences: Number(watchMax),
    });
  }

  async function handleCampaignSubmit(e: FormEvent) {
    e.preventDefault();
    if (!campGoal.trim()) return;
    if (
      await onCreateCampaign({
        goalText: campGoal.trim(),
        profile: campProfile,
        maxCycles: Number(campCycles),
        tokensPerCycle: 4096,
        toolsPerCycle: Number(campTools),
      })
    ) {
      setCampGoal("");
    }
  }

  async function handleBindingSubmit(e: FormEvent) {
    e.preventDefault();
    if (!label.trim()) return;
    const destination: Record<string, string> =
      channel === "webhook"
        ? { url: webhookUrl.trim() }
        : { botToken: tgToken.trim(), chatId: tgChat.trim(), apiBaseUrl: tgApiBase.trim() };
    if (
      await onCreateBinding({
        channel,
        label: label.trim(),
        destination,
        consentGranted: consentInit,
        timezoneId: notifyTz.trim() || "UTC",
      })
    ) {
      setLabel("");
      setWebhookUrl("");
      setTgToken("");
      setTgChat("");
    }
  }

  async function handleNotifyDryRun() {
    if (!selectedManualBinding || !notifyTitle.trim() || !notifyBody.trim()) return;
    await onDryRunNotification({
      bindingId: selectedManualBinding.id,
      title: notifyTitle.trim(),
      summaryText: notifyBody.trim(),
      priority: notifyPriority,
    });
  }

  async function handleNotifySubmit(e: FormEvent) {
    e.preventDefault();
    if (!selectedManualBinding || !notifyTitle.trim() || !notifyBody.trim()) return;
    if (
      await onSendManualNotification({
        bindingId: selectedManualBinding.id,
        title: notifyTitle.trim(),
        summaryText: notifyBody.trim(),
        priority: notifyPriority,
        dispatch: true,
      })
    ) {
      setNotifyTitle("");
      setNotifyBody("");
    }
  }

  return (
    <div className="console" data-testid="automation-notify-panel">
      <div className="console-grid">
        <div className="panel">
          <h2>Watches（定期スケジュール / 通知トリガー）</h2>
          <p className="muted">
            時刻スケジュール枠と通知トリガーを管理します（Web ページ差分の内容監視条件は未設定です）。
          </p>
          <form onSubmit={(e) => void handleWatchSubmit(e)} className="stack-form">
            <div className="inline-form">
              <input
                type="text"
                value={watchTz}
                onChange={(e) => setWatchTz(e.target.value)}
                placeholder="Timezone (Asia/Tokyo)"
                aria-label="Watchタイムゾーン"
              />
              <input
                type="number"
                min={0}
                max={23}
                value={watchHour}
                onChange={(e) => setWatchHour(Number(e.target.value))}
                aria-label="Watch時"
              />
              <input
                type="number"
                min={0}
                max={59}
                value={watchMin}
                onChange={(e) => setWatchMin(Number(e.target.value))}
                aria-label="Watch分"
              />
              <input
                type="number"
                min={1}
                max={1000}
                value={watchMax}
                onChange={(e) => setWatchMax(Number(e.target.value))}
                aria-label="Watch最大回数"
              />
            </div>
            <button type="submit" disabled={!authenticated || busy}>
              Watch 作成
            </button>
          </form>

          <div className="inline-form">
            <select
              value={watchBindingSel}
              onChange={(e) => setWatchBindingSel(e.target.value)}
              aria-label="Watch通知送信先Binding"
            >
              <option value="">-- Watch 通知先 Binding を選択してください --</option>
              {activeBindings.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.label} ({b.redactedTarget})
                </option>
              ))}
              {activeBindings.length > 0 && (
                <option value="__ALL__">同意済み全 Binding に明示送信 ({activeBindings.length}件)</option>
              )}
            </select>
          </div>

          {watchPreview && (
            <div className="preview-box" data-testid="watch-dryrun-preview">
              <strong>Watch Dry-Run プレビュー（DB 書込み・外部送信なし）:</strong> 送信可能 {watchPreview.sendableCount} /{" "}
              {watchPreview.recipientCount} 件
              <ul className="compact-list">
                {watchPreview.recipients.map((r) => (
                  <li key={r.bindingId}>
                    {r.label} [{r.channel}] ({r.redactedTarget}) —{" "}
                    {r.eligible ? "送信対象" : `抑止: ${r.blockedReason}`}
                    {r.wouldDeferUntil ? ` / 待機予定: ${r.wouldDeferUntil}` : ""}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <ul className="compact-list">
            {watches.map((w) => (
              <li key={w.watchId} className="row-between" data-testid={`watch-${w.watchId}`}>
                <span>
                  {w.watchId.slice(0, 8)} · {w.timezoneId} {String(w.localHour).padStart(2, "0")}:
                  {String(w.localMinute).padStart(2, "0")} · max={w.maxOccurrences} · active={String(w.active)}
                </span>
                {w.active && (
                  <div className="btn-row">
                    <button
                      type="button"
                      className="secondary-btn"
                      disabled={!authenticated || busy || !watchTarget}
                      onClick={() => watchTarget && void onDryRunWatch(w.watchId, watchTarget)}
                    >
                      プレビュー (Dry-Run)
                    </button>
                    <button
                      type="button"
                      disabled={!authenticated || busy || !watchTarget}
                      onClick={() => watchTarget && void onTriggerWatch(w.watchId, watchTarget)}
                    >
                      実際に送信する
                    </button>
                    <button
                      type="button"
                      className="danger-btn"
                      disabled={!authenticated || busy}
                      onClick={() => void onDeactivateWatch(w.watchId)}
                    >
                      停止
                    </button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>

        <div className="panel">
          <h2>Campaigns (有界反復タスク)</h2>
          <form onSubmit={(e) => void handleCampaignSubmit(e)} className="stack-form">
            <input
              type="text"
              value={campGoal}
              onChange={(e) => setCampGoal(e.target.value)}
              placeholder="Campaign 目標..."
              aria-label="Campaign目標"
            />
            <div className="inline-form">
              <select
                value={campProfile}
                onChange={(e) => setCampProfile(e.target.value as typeof campProfile)}
                aria-label="Campaignプロファイル"
              >
                <option value="default">default</option>
                <option value="browser">browser</option>
                <option value="sandbox">sandbox</option>
                <option value="anthropic">anthropic</option>
              </select>
              <input
                type="number"
                min={1}
                max={100}
                value={campCycles}
                onChange={(e) => setCampCycles(Number(e.target.value))}
                aria-label="最大サイクル数"
              />
              <input
                type="number"
                min={1}
                max={100}
                value={campTools}
                onChange={(e) => setCampTools(Number(e.target.value))}
                aria-label="サイクルあたりツール上限"
              />
            </div>
            <button type="submit" disabled={!authenticated || busy || !campGoal.trim()}>
              Campaign 開始
            </button>
          </form>

          <ul className="compact-list">
            {campaigns.map((c) => (
              <li key={c.id}>
                <button
                  type="button"
                  className={selectedCampaignId === c.id ? "chip chip-active" : "chip"}
                  onClick={() => onSelectCampaign(c.id)}
                >
                  [{c.state}] v{c.controlVersion} · goalRev {c.goalRevision} · {c.id.slice(0, 8)}
                </button>
              </li>
            ))}
          </ul>

          {selectedCamp && (
            <div className="detail-stack">
              <div className="btn-row">
                <button
                  type="button"
                  disabled={!authenticated || busy}
                  onClick={() => void onControlCampaign(selectedCamp.id, "pause", selectedCamp.controlVersion)}
                >
                  Pause
                </button>
                <button
                  type="button"
                  disabled={!authenticated || busy}
                  onClick={() => void onControlCampaign(selectedCamp.id, "resume", selectedCamp.controlVersion)}
                >
                  Resume
                </button>
                <button
                  type="button"
                  className="danger-btn"
                  disabled={!authenticated || busy}
                  onClick={() => void onControlCampaign(selectedCamp.id, "stop", selectedCamp.controlVersion)}
                >
                  Stop
                </button>
              </div>
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  void onSteerCampaign(selectedCamp.id, selectedCamp.controlVersion, steerRev, steerText);
                }}
                className="inline-form"
              >
                <input
                  type="text"
                  value={steerRev}
                  onChange={(e) => setSteerRev(e.target.value)}
                  placeholder="goalRev"
                  aria-label="Campaign GoalRevision"
                />
                <input
                  type="text"
                  value={steerText}
                  onChange={(e) => setSteerText(e.target.value)}
                  placeholder="新しい Campaign 目標"
                  aria-label="Campaign Steer目標"
                />
                <button type="submit" disabled={!authenticated || busy || !steerRev.trim()}>
                  Steer
                </button>
              </form>
              <pre className="event-log">{campaignEventsText || "(Campaign イベントなし)"}</pre>
            </div>
          )}
        </div>
      </div>

      <div className="console-grid">
        <div className="panel">
          <h2>通知先 Binding（AES-GCM 暗号化保存 / 同意管理）</h2>
          <form onSubmit={(e) => void handleBindingSubmit(e)} className="stack-form">
            <div className="inline-form">
              <select
                value={channel}
                onChange={(e) => setChannel(e.target.value as "webhook" | "telegram")}
                aria-label="通知チャネル"
              >
                <option value="webhook">Webhook</option>
                <option value="telegram">Telegram</option>
              </select>
              <input
                type="text"
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                placeholder="表示名 (例: My Webhook)"
                aria-label="通知ラベル"
              />
            </div>
            {channel === "webhook" ? (
              <input
                type="url"
                value={webhookUrl}
                onChange={(e) => setWebhookUrl(e.target.value)}
                placeholder="https://hooks.example.com/..."
                aria-label="Webhook URL"
              />
            ) : (
              <>
                <input
                  type="password"
                  value={tgToken}
                  onChange={(e) => setTgToken(e.target.value)}
                  placeholder="Telegram Bot Token"
                  aria-label="Telegram Bot Token"
                />
                <input
                  type="text"
                  value={tgChat}
                  onChange={(e) => setTgChat(e.target.value)}
                  placeholder="Telegram Chat ID"
                  aria-label="Telegram Chat ID"
                />
                <input
                  type="url"
                  value={tgApiBase}
                  onChange={(e) => setTgApiBase(e.target.value)}
                  placeholder="https://api.telegram.org"
                  aria-label="Telegram API Base"
                />
              </>
            )}
            <div className="inline-form">
              <input
                type="text"
                value={notifyTz}
                onChange={(e) => setNotifyTz(e.target.value)}
                placeholder="Timezone (UTC)"
                aria-label="通知タイムゾーン"
              />
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={consentInit}
                  onChange={(e) => setConsentInit(e.target.checked)}
                />
                初期同意を付与
              </label>
            </div>
            <button type="submit" disabled={!authenticated || busy || !label.trim()}>
              Binding 登録
            </button>
          </form>

          <ul className="compact-list">
            {bindings.map((b) => (
              <li key={b.id} className="card-subtle" data-testid={`binding-${b.id}`}>
                <div>
                  <strong>{b.label}</strong> [{b.channel}] · {b.redactedTarget}
                </div>
                <div className="muted">
                  consent={String(b.consentGranted)} (v{b.consentVersion}) · revoked={String(Boolean(b.revokedAt))}
                </div>
                {!b.revokedAt && (
                  <div className="btn-row">
                    <button
                      type="button"
                      disabled={!authenticated || busy}
                      onClick={() => void onToggleBindingConsent(b.id, !b.consentGranted)}
                    >
                      {b.consentGranted ? "同意撤回" : "同意付与"}
                    </button>
                    <button
                      type="button"
                      disabled={!authenticated || busy}
                      onClick={() => void onOptOutBinding(b.id)}
                    >
                      Opt-Out
                    </button>
                    <button
                      type="button"
                      className="danger-btn"
                      disabled={!authenticated || busy}
                      onClick={() => void onRevokeBinding(b.id)}
                    >
                      失効
                    </button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>

        <div className="panel">
          <h2>Notification Outbox & 配送履歴 ({notifications.length})</h2>
          <form onSubmit={(e) => void handleNotifySubmit(e)} className="stack-form">
            <select
              value={notifyBindingId}
              onChange={(e) => setNotifyBindingId(e.target.value)}
              aria-label="送信先Binding"
            >
              <option value="">-- 送信先 Binding を選択してください --</option>
              {activeBindings.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.label} ({b.redactedTarget})
                </option>
              ))}
            </select>
            <input
              type="text"
              value={notifyTitle}
              onChange={(e) => setNotifyTitle(e.target.value)}
              placeholder="通知タイトル"
              aria-label="通知タイトル"
            />
            <input
              type="text"
              value={notifyBody}
              onChange={(e) => setNotifyBody(e.target.value)}
              placeholder="通知本文"
              aria-label="通知本文"
            />
            <select
              value={notifyPriority}
              onChange={(e) => setNotifyPriority(e.target.value as typeof notifyPriority)}
              aria-label="通知優先度"
            >
              <option value="standard">standard</option>
              <option value="critical">critical</option>
              <option value="low">low</option>
            </select>
            <div className="btn-row">
              <button
                type="button"
                className="secondary-btn"
                disabled={
                  !authenticated || busy || !selectedManualBinding || !notifyTitle.trim() || !notifyBody.trim()
                }
                onClick={() => void handleNotifyDryRun()}
              >
                プレビュー (Dry-Run)
              </button>
              <button
                type="submit"
                disabled={
                  !authenticated || busy || !selectedManualBinding || !notifyTitle.trim() || !notifyBody.trim()
                }
              >
                実際に送信する
              </button>
              <button
                type="button"
                disabled={!authenticated || busy}
                onClick={() => void onDispatchNotifications()}
              >
                保留 Outbox 配送
              </button>
            </div>
          </form>

          {manualPreview && (
            <div className="preview-box" data-testid="manual-dryrun-preview">
              <strong>手動通知 Dry-Run プレビュー（DB 書込み・外部送信なし）:</strong> 送信可能{" "}
              {manualPreview.sendableCount} / {manualPreview.recipientCount} 件
              <ul className="compact-list">
                {manualPreview.recipients.map((r) => (
                  <li key={r.bindingId}>
                    {r.label} [{r.channel}] ({r.redactedTarget}) —{" "}
                    {r.eligible ? "送信対象" : `抑止: ${r.blockedReason}`}
                    {r.wouldDeferUntil ? ` / 待機予定: ${r.wouldDeferUntil}` : ""}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <ul className="compact-list">
            {notifications.map((n) => (
              <li key={n.id} className="card-subtle" data-testid={`notification-${n.id}`} data-status={n.status}>
                <div className="row-between">
                  <strong>{n.title}</strong>
                  <span
                    className={
                      n.status === "DELIVERY_UNKNOWN" || n.status === "FAILED"
                        ? "badge badge-warn"
                        : n.status === "READ"
                          ? "badge badge-ok"
                          : "badge badge-info"
                    }
                  >
                    {NOTIFY_STATUS_LABELS[n.status] ?? n.status}
                  </span>
                </div>
                <div>{n.summaryText}</div>
                <div className="muted">
                  source={n.sourceKind}:{n.sourceRef} · observed={n.observedAt}
                  {n.lastErrorCode ? ` · err=${n.lastErrorCode}` : ""}
                </div>
                {n.evidenceUrls.length > 0 && (
                  <div className="muted">出典: {n.evidenceUrls.join(", ")}</div>
                )}
                {n.status === "DELIVERY_UNKNOWN" && (
                  <div className="banner banner-error">
                    配送 ACK 欠落のため未確定（UNKNOWN）です。二重送信を防ぐため自動再送されません。
                  </div>
                )}
                {n.status === "PROVIDER_ACCEPTED" && (
                  <div className="btn-row">
                    <button
                      type="button"
                      disabled={!authenticated || busy}
                      onClick={() => void onMarkRead(n.id)}
                    >
                      既読にする (READ)
                    </button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}
