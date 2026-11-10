"use strict";
const byId = id => document.getElementById(id);
const invoke = window.__TAURI__?.core?.invoke;
const showText = (el, text) => { el.textContent = String(text); };
function update(s) {
  showText(byId("connection"), s.connection === "UNPAIRED_OFFLINE" ? "● 未接続 · 未ペアリング" : s.connection);
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
  byId("revoke").disabled = s.revoked;
}
async function call(command) {
  if (!invoke) { showText(byId("connection"), "● ブラウザプレビューのみ / Tauri未接続"); return; }
  try { update(await invoke(command)); }
  catch { showText(byId("connection"), "● 状態取得エラー · 操作は禁止"); }
}
byId("stop").addEventListener("click", () => call("stop_local"));
byId("revoke").addEventListener("click", () => call("revoke_local"));
call("snapshot");
