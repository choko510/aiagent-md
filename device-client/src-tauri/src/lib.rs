//! The Tauri window is a read-only mock UI. It exposes no shell, browser, USB or
//! Cloud-auth commands. The real Agent IPC and pairing require separate security review.
use serde::Serialize;
use std::sync::Mutex;

#[derive(Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct UiSnapshot {
    mode: &'static str,
    connection: &'static str,
    capabilities: Vec<&'static str>,
    pending_action: &'static str,
    revoked: bool,
    stopped: bool,
    audit: Vec<String>,
    cloud_authorized: bool,
    real_device_io_enabled: bool,
}
struct UiState {
    stopped: bool,
    revoked: bool,
    audit: Vec<String>,
}
impl Default for UiState {
    fn default() -> Self {
        Self {
            stopped: false,
            revoked: false,
            audit: vec!["SIMULATOR: 実デバイス未登録 / 外部操作は無効".into()],
        }
    }
}
impl UiState {
    fn snapshot(&self) -> UiSnapshot {
        UiSnapshot {
            mode: "LOCAL_MOCK_ONLY",
            connection: "UNPAIRED_OFFLINE",
            capabilities: vec!["Mock Journal", "Mock Policy", "Local Stop"],
            pending_action: "なし",
            revoked: self.revoked,
            stopped: self.stopped,
            audit: self.audit.clone(),
            cloud_authorized: false,
            real_device_io_enabled: false,
        }
    }
}
#[tauri::command]
fn snapshot(state: tauri::State<'_, Mutex<UiState>>) -> Result<UiSnapshot, String> {
    Ok(state.lock().map_err(|_| "STATE_LOCKED")?.snapshot())
}
#[tauri::command]
fn stop_local(state: tauri::State<'_, Mutex<UiState>>) -> Result<UiSnapshot, String> {
    let mut state = state.lock().map_err(|_| "STATE_LOCKED")?;
    if !state.stopped {
        state.stopped = true;
        state.audit.push("LOCAL_STOP: 新規Mock操作は停止".into());
    }
    Ok(state.snapshot())
}
#[tauri::command]
fn revoke_local(state: tauri::State<'_, Mutex<UiState>>) -> Result<UiSnapshot, String> {
    let mut state = state.lock().map_err(|_| "STATE_LOCKED")?;
    if !state.revoked {
        state.stopped = true;
        state.revoked = true;
        state
            .audit
            .push("LOCAL_REVOKE: Mock端末のみ無効化（Cloud失効ではない）".into());
    }
    Ok(state.snapshot())
}
pub fn run() {
    tauri::Builder::default()
        .manage(Mutex::new(UiState::default()))
        .invoke_handler(tauri::generate_handler![snapshot, stop_local, revoke_local])
        .run(tauri::generate_context!())
        .expect("failed to start mock desktop UI");
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn unpaired_never_authorizes_device_actions() {
        let s = UiState::default().snapshot();
        assert!(!s.cloud_authorized);
        assert!(!s.real_device_io_enabled);
        assert_eq!(s.connection, "UNPAIRED_OFFLINE");
    }
}
