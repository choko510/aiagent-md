"use strict";
const byId = id => document.getElementById(id);
const getInvoke = () => window.__TAURI_INTERNALS__?.invoke ?? window.__TAURI__?.core?.invoke;
const showText = (el, text) => { el.textContent = String(text); };
function failClosed(message) {
  showText(byId("connection"), message);
  byId("pair").disabled = true;
  byId("perform").disabled = true;
  byId("stop").disabled = true;
  byId("revoke").disabled = true;
}
function update(s) {
  const statusMap = {
    UNPAIRED_OFFLINE: "● 未接続 · 未ペアリング",
    REVOKED: "● 無効化済み · ローカルMock端末停止",
    JOURNAL_UNAVAILABLE: "● Journal利用不可 · 安全停止中",
  };
  showText(byId("connection"), statusMap[s.connection] ?? s.connection);
  const caps = byId("caps"); caps.replaceChildren();
  for (const name of s.capabilities) {
    const li = document.createElement("li"); li.textContent = name; caps.appendChild(li);
  }
  showText(byId("pending"), s.pendingAction);
  const audit = byId("audit"); audit.replaceChildren();
  for (const entry of s.audit) {
    const li = document.createElement("li"); li.textContent = entry; audit.appendChild(li);
  }
  // Buttons may never start real device actions in this version.
  byId("pair").disabled = true;
  byId("perform").disabled = true;
  byId("stop").disabled = s.stopped || s.revoked;
  byId("revoke").disabled = s.revoked || !s.persisted;
}
async function call(command) {
  const invoke = getInvoke();
  if (!invoke) { failClosed("● ブラウザプレビューのみ / Tauri未接続"); return; }
  try { update(await invoke(command)); }
  catch { failClosed("● 状態取得エラー · 操作は禁止"); }
}
byId("stop").addEventListener("click", () => call("stop_local"));
byId("revoke").addEventListener("click", () => call("revoke_local"));
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => call("snapshot"));
} else {
  call("snapshot");
}
