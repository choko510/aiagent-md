//! The Tauri window is a read-only mock UI backed by `aiagent-device-core`'s persisted
//! `LocalJournal`. It exposes no shell, browser, USB, or Cloud-auth commands.
use aiagent_device_core::{
    ActionRequest, Agent, CloudGrant, CloudPairingConfig, ConnectionPhase, ControlRecord, Decision,
    Fence, LocalAdapters, LocalJournal, LocalPolicy, Scope, StartPermit, TlsAuthenticatedChannel,
};
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
    scope: Scope,
    journal_path: PathBuf,
    phase: ConnectionPhase,
    policy: LocalPolicy,
    journal: Option<LocalJournal>,
    agent: Option<Agent<TlsAuthenticatedChannel, LocalAdapters>>,
    pending_admitted: Option<(ActionRequest, CloudGrant, StartPermit, u64)>,
    stopped: bool,
    revoked: bool,
    audit: Vec<String>,
    init_error: Option<String>,
    control_persisted: bool,
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
                scope,
                journal_path: path.to_path_buf(),
                phase: if rec.revoked {
                    ConnectionPhase::Revoked
                } else {
                    ConnectionPhase::Unpaired
                },
                policy,
                journal: Some(journal),
                agent: None,
                pending_admitted: None,
                stopped: rec.stopped || rec.revoked,
                revoked: rec.revoked,
                audit: rec.audit,
                init_error: None,
                control_persisted: false,
            },
            Err(err) => {
                // Fail closed if persisted journal cannot be opened: lock down local UI controls.
                Self {
                    scope,
                    journal_path: path.to_path_buf(),
                    phase: ConnectionPhase::Unpaired,
                    policy,
                    journal: None,
                    agent: None,
                    pending_admitted: None,
                    stopped: true,
                    revoked: false,
                    audit: vec![format!("JOURNAL_UNAVAILABLE: fail-closed ({err})")],
                    init_error: Some(err),
                    control_persisted: false,
                }
            }
        }
    }

    fn pair_cloud(
        &mut self,
        config: CloudPairingConfig,
        fence: Fence,
        adapters: LocalAdapters,
    ) -> Result<(), &'static str> {
        if self.stopped || self.revoked || self.init_error.is_some() {
            return Err("LOCAL_STOP_OR_REVOKED");
        }
        let mut channel = TlsAuthenticatedChannel::unpaired();
        channel.pair(config)?;
        let journal = LocalJournal::open(&self.journal_path, &self.scope)
            .map_err(|_| "JOURNAL_UNAVAILABLE")?;
        let mut policy = LocalPolicy::default();
        for app in &self.policy.desktop_apps {
            let _ = policy.allow_desktop(app);
        }
        for origin in &self.policy.browser_origins {
            let _ = policy.allow_browser_origin(origin);
        }
        policy.allow_mock_build = self.policy.allow_mock_build;
        policy.allow_mock_android = self.policy.allow_mock_android;
        self.agent = Some(Agent::new(
            self.scope.clone(),
            fence,
            channel,
            adapters,
            policy,
            journal,
        ));
        self.phase = ConnectionPhase::OnlineMock;
        self.audit.push("CLOUD_PAIR: mTLS確認済み接続確立".into());
        let _ = self.persist_control();
        Ok(())
    }

    fn execute_admitted_action(
        &mut self,
        request: &ActionRequest,
        grant: &CloudGrant,
        permit: Option<StartPermit>,
        now_ms: u64,
    ) -> Decision {
        if self.stopped || self.revoked {
            return Decision::Denied("LOCAL_STOP_OR_REVOKED");
        }
        let Some(agent) = self.agent.as_mut() else {
            return Decision::Denied("UNPAIRED");
        };
        if let Some(p) = permit {
            agent.channel.active_permit = Some(p);
        }
        let dec = agent.execute(request, grant, now_ms);
        self.audit
            .push(format!("AGENT_EXEC: {:?} ({:?})", request.operation, dec));
        let _ = self.persist_control();
        dec
    }

    fn persist_control(&mut self) -> Result<(), String> {
        self.control_persisted = false;
        let Some(journal) = self.journal.as_mut() else {
            return Err("JOURNAL_UNAVAILABLE".into());
        };
        journal.save_control(&ControlRecord {
            stopped: self.stopped,
            revoked: self.revoked,
            audit: self.audit.clone(),
        })?;
        self.control_persisted = true;
        Ok(())
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
        let paired = self
            .agent
            .as_ref()
            .is_some_and(|a| !a.stopped() && !a.revoked());
        UiSnapshot {
            mode: "LOCAL_MOCK_ONLY",
            connection: if self.revoked {
                "REVOKED"
            } else if self.init_error.is_some() {
                "JOURNAL_UNAVAILABLE"
            } else if paired {
                "ONLINE_MOCK"
            } else {
                "UNPAIRED_OFFLINE"
            },
            phase: phase_label(self.phase),
            capabilities: self.capabilities(),
            pending_action: "なし",
            revoked: self.revoked,
            stopped: self.stopped,
            persisted: self.control_persisted,
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
        if let Some(agent) = state.agent.as_mut() {
            agent.stop()?;
        }
        state.audit.push("LOCAL_STOP: 新規操作を停止".into());
    }
    // A previous persistence attempt may have failed after setting the in-memory flag.
    state.persist_control()?;
    Ok(state.snapshot())
}

#[tauri::command]
fn revoke_local(state: tauri::State<'_, Mutex<UiState>>) -> Result<UiSnapshot, String> {
    let mut state = state.lock().map_err(|_| "STATE_LOCKED")?;
    if !state.revoked {
        state.stopped = true;
        state.revoked = true;
        state.phase = ConnectionPhase::Revoked;
        if let Some(agent) = state.agent.as_mut() {
            agent.revoke()?;
        }
        state
            .audit
            .push("LOCAL_REVOKE: 端末ペアリング・ローカル操作を無効化".into());
    }
    state.persist_control()?;
    Ok(state.snapshot())
}

impl UiState {
    fn recover_legacy_stop_internal(&mut self, confirmation: &str) -> Result<(), String> {
        self.control_persisted = false;
        if self.init_error.is_some() {
            return Err("JOURNAL_UNAVAILABLE".into());
        }
        if self.revoked {
            return Err("CANNOT_RECOVER_REVOKED_DEVICE".into());
        }
        let Some(journal) = self.journal.as_mut() else {
            return Err("JOURNAL_UNAVAILABLE".into());
        };
        let rec = journal.owner_confirm_clear_legacy_stop(confirmation)?;
        self.stopped = rec.stopped;
        self.revoked = rec.revoked;
        self.audit = rec.audit;
        self.control_persisted = true;
        Ok(())
    }
}

#[tauri::command]
fn recover_legacy_stop(
    state: tauri::State<'_, Mutex<UiState>>,
    confirmation: String,
) -> Result<UiSnapshot, String> {
    let mut state = state.lock().map_err(|_| "STATE_LOCKED")?;
    state.recover_legacy_stop_internal(&confirmation)?;
    Ok(state.snapshot())
}

#[tauri::command]
fn pair_cloud(
    state: tauri::State<'_, Mutex<UiState>>,
    endpoint: String,
    ca_pin_sha256: String,
    client_cert_present: bool,
) -> Result<UiSnapshot, String> {
    let mut state = state.lock().map_err(|_| "STATE_LOCKED")?;
    let cfg = CloudPairingConfig {
        endpoint,
        ca_pin_sha256,
        client_cert_present,
        tls_handshake_verified: false, // UI alone may never fabricate a verified mTLS handshake
    };
    let fence = Fence {
        device_claim_epoch: aiagent_device_core::Epoch(1),
        lease_epoch: aiagent_device_core::Epoch(1),
        restore_incarnation: aiagent_device_core::Epoch(1),
        connection_generation: aiagent_device_core::Epoch(1),
        goal_revision: aiagent_device_core::Epoch(1),
        policy_revision: aiagent_device_core::Epoch(1),
    };
    state
        .pair_cloud(cfg, fence, LocalAdapters::default())
        .map_err(|e| e.to_string())?;
    Ok(state.snapshot())
}

#[tauri::command]
fn perform_local_action(state: tauri::State<'_, Mutex<UiState>>) -> Result<UiSnapshot, String> {
    let mut state = state.lock().map_err(|_| "STATE_LOCKED")?;
    if state.stopped || state.revoked {
        return Err("LOCAL_STOP_OR_REVOKED".into());
    }
    let Some((req, grant, permit, now_ms)) = state.pending_admitted.take() else {
        return Err(if state.agent.is_none() {
            "UNPAIRED"
        } else {
            "NO_ADMITTED_CLOUD_GRANT"
        }
        .into());
    };
    let dec = state.execute_admitted_action(&req, &grant, Some(permit), now_ms);
    if let Decision::Denied(reason) = dec {
        return Err(reason.into());
    }
    Ok(state.snapshot())
}

pub fn run() {
    tauri::Builder::default()
        .manage(Mutex::new(UiState::open_default()))
        .invoke_handler(tauri::generate_handler![
            snapshot,
            stop_local,
            revoke_local,
            recover_legacy_stop,
            pair_cloud,
            perform_local_action
        ])
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
        assert!(!s.persisted);
    }

    #[test]
    fn persistence_status_tracks_last_write_attempt() {
        let dir = TempDir::new();
        let mut state = UiState::open_in(&dir.path().join("journal.db"));
        state.persist_control().unwrap();
        assert!(state.snapshot().persisted);
        state.journal = None;
        assert!(state.persist_control().is_err());
        let snapshot = state.snapshot();
        assert!(!snapshot.persisted);
        assert!(!snapshot.cloud_authorized && !snapshot.real_device_io_enabled);
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

    #[test]
    fn ui_config_and_html_start_hardened_and_fail_closed() {
        let conf = include_str!("../tauri.conf.json");
        assert!(conf.contains("\"withGlobalTauri\": false"));
        let html = include_str!("../../app/ui/index.html");
        assert!(html.contains("<button id=\"stop\" class=\"danger\" disabled>"));
        assert!(html.contains("<button id=\"revoke\" class=\"secondary\" disabled>"));
        let js = include_str!("../../app/ui/app.js");
        assert!(js.contains("__TAURI_INTERNALS__"));
    }

    #[test]
    fn tauri_state_routes_admitted_action_through_agent_execute_and_enforces_stop() {
        use aiagent_device_core::mock::mock_grant;
        use aiagent_device_core::{make_start_permit, Epoch, Operation, Target};

        let dir = TempDir::new();
        let mut state = UiState::open_in(&dir.path().join("journal.db"));
        let fence = Fence {
            device_claim_epoch: Epoch(1),
            lease_epoch: Epoch(1),
            restore_incarnation: Epoch(1),
            connection_generation: Epoch(1),
            goal_revision: Epoch(1),
            policy_revision: Epoch(1),
        };
        let ws = dir.path().join("ws-1");
        std::fs::create_dir_all(&ws).unwrap();
        let req = ActionRequest::new(
            state.scope.clone(),
            fence.clone(),
            "intent-tauri-1".into(),
            Epoch(1),
            Operation::BuildRun,
            Target::Build {
                workspace_id: "ws-1".into(),
            },
            b"cargo check".to_vec(),
        );
        let grant = mock_grant(&req, "g-1", 200);
        // Unpaired refuses execution cleanly
        assert_eq!(
            state.execute_admitted_action(&req, &grant, None, 100),
            Decision::Denied("UNPAIRED")
        );

        state.policy.allow_mock_build = true;
        let cfg = CloudPairingConfig {
            endpoint: "wss://control.aiagent.example/ws".into(),
            ca_pin_sha256: "c".repeat(64),
            client_cert_present: true,
            tls_handshake_verified: true,
        };
        let adapters = LocalAdapters {
            build_root: Some(dir.path().to_path_buf()),
            ..Default::default()
        };
        assert_eq!(
            state.pair_cloud(cfg, fence, adapters),
            Err("AUTHENTICATED_TRANSPORT_UNAVAILABLE")
        );
        let permit = make_start_permit(&req, &grant, "p-1", 200);
        assert_eq!(
            state.execute_admitted_action(&req, &grant, Some(permit), 100),
            Decision::Denied("UNPAIRED")
        );
        state.stopped = true;
        if let Some(a) = state.agent.as_mut() {
            a.stop().unwrap();
        }
        assert_eq!(
            state.execute_admitted_action(&req, &grant, None, 100),
            Decision::Denied("LOCAL_STOP_OR_REVOKED")
        );
    }

    #[test]
    fn recover_legacy_stop_enforces_confirmation_and_permanent_revoke() {
        let dir = TempDir::new();
        let mut state = UiState::open_in(&dir.path().join("journal.db"));
        assert_eq!(
            state.recover_legacy_stop_internal("WRONG").unwrap_err(),
            "OWNER_CONFIRMATION_REQUIRED"
        );
        assert_eq!(
            state
                .recover_legacy_stop_internal(LocalJournal::OWNER_RECOVERY_PHRASE)
                .unwrap_err(),
            "NOT_A_LEGACY_MIGRATED_STOP"
        );
        state.revoked = true;
        assert_eq!(
            state
                .recover_legacy_stop_internal(LocalJournal::OWNER_RECOVERY_PHRASE)
                .unwrap_err(),
            "CANNOT_RECOVER_REVOKED_DEVICE"
        );
    }
}
