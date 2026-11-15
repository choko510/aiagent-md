//! The Tauri window is a read-only mock UI backed by `aiagent-device-core`'s persisted
//! `LocalJournal`. It exposes no shell, browser, USB, or Cloud-auth commands.
use aiagent_device_core::{ConnectionPhase, ControlRecord, LocalJournal, LocalPolicy, Scope};
use serde::Serialize;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

#[derive(Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct UiSnapshot {
    mode: &'static str,
    connection: &'static str,
    phase: &'static str,
    capabilities: Vec<&'static str>,
    pending_action: &'static str,
    revoked: bool,
    stopped: bool,
    persisted: bool,
    audit: Vec<String>,
    cloud_authorized: bool,
    real_device_io_enabled: bool,
}

struct UiState {
    phase: ConnectionPhase,
    policy: LocalPolicy,
    journal: Option<LocalJournal>,
    stopped: bool,
    revoked: bool,
    audit: Vec<String>,
    init_error: Option<String>,
}

fn default_audit() -> Vec<String> {
    vec!["SIMULATOR: 実デバイス未登録 / 外部操作は無効".into()]
}

fn simulator_scope() -> Scope {
    let os_user = std::env::var("USERNAME")
        .or_else(|_| std::env::var("USER"))
        .ok()
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| "local-user".into());
    Scope {
        os_user,
        tenant_id: "local-mock-tenant".into(),
        owner_user_id: "local-mock-owner".into(),
        device_identity_id: "unpaired-device-identity".into(),
        device_id: "unpaired-device".into(),
    }
}

fn default_journal_path() -> PathBuf {
    let base = std::env::var_os("LOCALAPPDATA")
        .or_else(|| std::env::var_os("XDG_DATA_HOME"))
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".local/share")))
        .unwrap_or_else(std::env::temp_dir);
    base.join("aiagent-device-mock").join("journal.db")
}

fn phase_label(phase: ConnectionPhase) -> &'static str {
    match phase {
        ConnectionPhase::Unpaired => "UNPAIRED",
        ConnectionPhase::PairedOffline => "PAIRED_OFFLINE",
        ConnectionPhase::OnlineMock => "ONLINE_MOCK",
        ConnectionPhase::Stale => "STALE",
        ConnectionPhase::Revoked => "REVOKED",
    }
}

impl UiState {
    fn open_default() -> Self {
        Self::open_in(&default_journal_path())
    }

    fn open_in(path: &Path) -> Self {
        let scope = simulator_scope();
        let policy = LocalPolicy::default();
        match LocalJournal::open(path, &scope)
            .and_then(|j| j.load_control(default_audit()).map(|rec| (j, rec)))
        {
            Ok((journal, rec)) => Self {
                phase: if rec.revoked {
                    ConnectionPhase::Revoked
                } else {
                    ConnectionPhase::Unpaired
                },
                policy,
                journal: Some(journal),
                stopped: rec.stopped || rec.revoked,
                revoked: rec.revoked,
                audit: rec.audit,
                init_error: None,
            },
            Err(err) => {
                // Fail closed if persisted journal cannot be opened: lock down local UI controls.
                Self {
                    phase: ConnectionPhase::Unpaired,
                    policy,
                    journal: None,
                    stopped: true,
                    revoked: false,
                    audit: vec![format!("JOURNAL_UNAVAILABLE: fail-closed ({err})")],
                    init_error: Some(err),
                }
            }
        }
    }

    fn persist_control(&mut self) -> Result<(), String> {
        let Some(journal) = self.journal.as_mut() else {
            return Err("JOURNAL_UNAVAILABLE".into());
        };
        journal.save_control(&ControlRecord {
            stopped: self.stopped,
            revoked: self.revoked,
            audit: self.audit.clone(),
        })
    }

    fn capabilities(&self) -> Vec<&'static str> {
        let mut caps = vec!["Mock Journal", "Mock Policy", "Local Stop"];
        if self.policy.allow_mock_build {
            caps.push("Mock Build");
        }
        if self.policy.allow_mock_android {
            caps.push("Mock Android");
        }
        caps
    }

    fn snapshot(&self) -> UiSnapshot {
        UiSnapshot {
            mode: "LOCAL_MOCK_ONLY",
            connection: if self.revoked {
                "REVOKED"
            } else if self.init_error.is_some() {
                "JOURNAL_UNAVAILABLE"
            } else {
                "UNPAIRED_OFFLINE"
            },
            phase: phase_label(self.phase),
            capabilities: self.capabilities(),
            pending_action: "なし",
            revoked: self.revoked,
            stopped: self.stopped,
            persisted: self.journal.is_some(),
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
        state.persist_control()?;
    }
    Ok(state.snapshot())
}

#[tauri::command]
fn revoke_local(state: tauri::State<'_, Mutex<UiState>>) -> Result<UiSnapshot, String> {
    let mut state = state.lock().map_err(|_| "STATE_LOCKED")?;
    if !state.revoked {
        state.stopped = true;
        state.revoked = true;
        state.phase = ConnectionPhase::Revoked;
        state
            .audit
            .push("LOCAL_REVOKE: Mock端末のみ無効化（Cloud失効ではない）".into());
        state.persist_control()?;
    }
    Ok(state.snapshot())
}

pub fn run() {
    tauri::Builder::default()
        .manage(Mutex::new(UiState::open_default()))
        .invoke_handler(tauri::generate_handler![snapshot, stop_local, revoke_local])
        .run(tauri::generate_context!())
        .expect("failed to start mock desktop UI");
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    struct TempDir(PathBuf);
    impl TempDir {
        fn new() -> Self {
            static COUNTER: AtomicU64 = AtomicU64::new(0);
            let id = COUNTER.fetch_add(1, Ordering::Relaxed);
            let dir = std::env::temp_dir().join(format!(
                "aiagent-device-tauri-test-{}-{id}",
                std::process::id()
            ));
            std::fs::create_dir_all(&dir).unwrap();
            Self(dir)
        }
        fn path(&self) -> &Path {
            &self.0
        }
    }
    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn unpaired_never_authorizes_device_actions() {
        let dir = TempDir::new();
        let s = UiState::open_in(&dir.path().join("journal.db")).snapshot();
        assert!(!s.cloud_authorized);
        assert!(!s.real_device_io_enabled);
        assert_eq!(s.connection, "UNPAIRED_OFFLINE");
        assert_eq!(s.phase, "UNPAIRED");
        assert!(s.persisted);
    }

    #[test]
    fn stop_and_revoke_persist_across_reopen() {
        let dir = TempDir::new();
        let path = dir.path().join("journal.db");
        {
            let mut state = UiState::open_in(&path);
            state.stopped = true;
            state.revoked = true;
            state.phase = ConnectionPhase::Revoked;
            state.audit.push("LOCAL_REVOKE".into());
            state.persist_control().unwrap();
        }
        let reloaded = UiState::open_in(&path).snapshot();
        assert!(reloaded.stopped);
        assert!(reloaded.revoked);
        assert_eq!(reloaded.phase, "REVOKED");
        assert_eq!(reloaded.connection, "REVOKED");
        assert!(reloaded.audit.iter().any(|entry| entry == "LOCAL_REVOKE"));
    }

    #[test]
    fn corrupted_journal_path_fails_closed() {
        let dir = TempDir::new();
        let state = UiState::open_in(dir.path());
        let snap = state.snapshot();
        assert!(snap.stopped);
        assert!(!snap.persisted);
        assert!(!snap.cloud_authorized);
        assert!(!snap.real_device_io_enabled);
        assert_eq!(snap.connection, "JOURNAL_UNAVAILABLE");
    }
}
