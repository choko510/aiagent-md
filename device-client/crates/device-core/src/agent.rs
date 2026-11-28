use crate::journal::{BeginResult, EffectState, LocalJournal};
use crate::protocol::{
    valid_browser_origin, valid_field, ActionRequest, CloudGrant, Fence, Operation, Scope,
    StartPermit, Target,
};
use std::collections::HashSet;

/// These traits are capability boundaries, NOT a licence to forward raw MCP/CDP/shell.
pub trait AuthenticatedChannelPort {
    fn online(&self) -> bool;
    /// Production adapter must validate an authenticated server START_PERMIT and CAS state.
    fn authorize_start(
        &mut self,
        request: &ActionRequest,
        grant: &CloudGrant,
        now_ms: u64,
    ) -> Option<StartPermit>;
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Delivery {
    LocalOnly,
    Failed,
    PossiblyApplied,
}
pub trait CuaAdapterPort {
    fn desktop(&mut self, request: &ActionRequest) -> Delivery;
}
pub trait ChromeAdapterPort {
    fn chrome(&mut self, request: &ActionRequest) -> Delivery;
}
pub trait BuildAdapterPort {
    fn build(&mut self, request: &ActionRequest) -> Delivery;
}
pub trait AndroidAdapterPort {
    fn android(&mut self, request: &ActionRequest) -> Delivery;
}
pub trait AdapterPorts:
    CuaAdapterPort + ChromeAdapterPort + BuildAdapterPort + AndroidAdapterPort
{
}
impl<T: CuaAdapterPort + ChromeAdapterPort + BuildAdapterPort + AndroidAdapterPort> AdapterPorts
    for T
{
}

/// Configured locally by the user; Cloud permission is always separately required.
#[derive(Default)]
pub struct LocalPolicy {
    pub desktop_apps: HashSet<String>,
    pub browser_origins: HashSet<String>,
    pub allow_mock_build: bool,
    pub allow_mock_android: bool,
}
impl LocalPolicy {
    pub fn allow_desktop(&mut self, app_id: &str) -> Result<(), &'static str> {
        if !valid_field(app_id, 256) {
            return Err("INVALID_POLICY_ENTRY");
        }
        self.desktop_apps.insert(app_id.to_owned());
        Ok(())
    }
    pub fn allow_browser_origin(&mut self, origin: &str) -> Result<(), &'static str> {
        if !valid_browser_origin(origin) {
            return Err("INVALID_POLICY_ENTRY");
        }
        self.browser_origins.insert(origin.to_owned());
        Ok(())
    }
    fn allows(&self, request: &ActionRequest) -> bool {
        if !request.target.valid() {
            return false;
        }
        match (&request.operation, &request.target) {
            (
                Operation::DesktopInspect | Operation::DesktopClick,
                Target::Desktop {
                    app_id,
                    window_token,
                    process_generation,
                    browser_like,
                },
            ) => {
                // Positive allowlist prevents desktop-input browser-origin bypass.
                !browser_like
                    && valid_field(window_token, 256)
                    && valid_field(process_generation, 256)
                    && valid_field(app_id, 256)
                    && self.desktop_apps.contains(app_id)
            }
            (
                Operation::BrowserInspect | Operation::BrowserClick,
                Target::Browser {
                    origin,
                    profile_id,
                    tab_id,
                    document_generation,
                },
            ) => {
                valid_browser_origin(origin)
                    && valid_field(profile_id, 256)
                    && valid_field(tab_id, 256)
                    && valid_field(document_generation, 256)
                    && self.browser_origins.contains(origin)
            }
            (Operation::BuildRun, Target::Build { workspace_id }) => {
                self.allow_mock_build && valid_field(workspace_id, 256)
            }
            (Operation::AndroidInstall, Target::Android { serial, package_id }) => {
                self.allow_mock_android && valid_field(serial, 256) && valid_field(package_id, 256)
            }
            _ => false,
        }
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ConnectionPhase {
    Unpaired,
    PairedOffline,
    OnlineMock,
    Stale,
    Revoked,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Decision {
    Denied(&'static str),
    LocalCompleted,
    AlreadyCompletedLocal,
    Failed,
    Unknown,
    IntentConflict,
    GrantReused,
    PermitReused,
    StorageError,
}

/// Simulator-only state machine; no registered transport or device side effects.
pub struct Agent<C: AuthenticatedChannelPort, A: AdapterPorts> {
    scope: Scope,
    fence: Fence,
    pub channel: C,
    pub adapters: A,
    pub policy: LocalPolicy,
    journal: LocalJournal,
    phase: ConnectionPhase,
    stopped: bool,
    revoked: bool,
}
impl<C: AuthenticatedChannelPort, A: AdapterPorts> Agent<C, A> {
    pub fn new(
        scope: Scope,
        fence: Fence,
        channel: C,
        adapters: A,
        policy: LocalPolicy,
        journal: LocalJournal,
    ) -> Self {
        // A reopened simulator must honor durable Stop/Revoke; corrupted control
        // can never be interpreted as permission to start.
        let control = journal.load_control(vec![]);
        let stopped = control
            .as_ref()
            .map_or(true, |record| record.stopped || record.revoked);
        let revoked = control.as_ref().is_ok_and(|record| record.revoked);
        let phase = if revoked {
            ConnectionPhase::Revoked
        } else if channel.online() {
            ConnectionPhase::OnlineMock
        } else {
            ConnectionPhase::PairedOffline
        };
        Self {
            scope,
            fence,
            channel,
            adapters,
            policy,
            journal,
            phase,
            stopped,
            revoked,
        }
    }
    pub fn stop(&mut self) -> Result<(), String> {
        self.stopped = true;
        let mut control = self.journal.load_control(vec![])?;
        control.stopped = true;
        self.journal.save_control(&control)
    }
    pub fn revoke(&mut self) -> Result<(), String> {
        self.revoked = true;
        self.stopped = true;
        self.phase = ConnectionPhase::Revoked;
        let mut control = self.journal.load_control(vec![])?;
        control.stopped = true;
        control.revoked = true;
        self.journal.save_control(&control)
    }
    pub fn phase(&self) -> ConnectionPhase {
        self.phase
    }
    pub fn disconnect(&mut self) {
        if !self.revoked {
            self.phase = ConnectionPhase::PairedOffline;
        }
    }
    pub fn unpair(&mut self) -> Result<(), String> {
        if !self.revoked {
            self.stopped = true;
            self.phase = ConnectionPhase::Unpaired;
            let mut control = self.journal.load_control(vec![])?;
            control.stopped = true;
            self.journal.save_control(&control)?;
        }
        Ok(())
    }
    pub fn mark_stale(&mut self) {
        if !self.revoked && self.phase != ConnectionPhase::Unpaired {
            self.phase = ConnectionPhase::Stale;
        }
    }
    /// Simulator-only reconnect. An old connection generation or unpaired client can never be revived.
    pub fn reconnect_mock(&mut self, next_fence: Fence) -> Result<(), &'static str> {
        if self.phase == ConnectionPhase::Unpaired
            || self.stopped
            || self.revoked
            || !next_fence.valid()
            || next_fence.connection_generation <= self.fence.connection_generation
            // This mock has no authenticated authority for changing the other fences.
            || next_fence.device_claim_epoch != self.fence.device_claim_epoch
            || next_fence.lease_epoch != self.fence.lease_epoch
            || next_fence.restore_incarnation != self.fence.restore_incarnation
            || next_fence.goal_revision != self.fence.goal_revision
            || next_fence.policy_revision != self.fence.policy_revision
            || !self.channel.online()
        {
            return Err("RECONNECT_DENIED");
        }
        self.fence = next_fence;
        self.phase = ConnectionPhase::OnlineMock;
        Ok(())
    }
    pub fn stopped(&self) -> bool {
        self.stopped
    }
    pub fn revoked(&self) -> bool {
        self.revoked
    }
    pub fn effect(&self, intent_id: &str) -> Result<Option<EffectState>, String> {
        self.journal.state(intent_id)
    }
    pub fn execute(
        &mut self,
        request: &ActionRequest,
        grant: &CloudGrant,
        now_ms: u64,
    ) -> Decision {
        // No offline starts, including "already approved" grants.
        if self.stopped || self.revoked {
            return Decision::Denied("LOCAL_STOP_OR_REVOKED");
        }
        if self.phase != ConnectionPhase::OnlineMock || !self.channel.online() {
            return Decision::Denied("OFFLINE");
        }
        if request.scope != self.scope || request.fence != self.fence {
            return Decision::Denied("STALE_SCOPE_OR_FENCE");
        }
        if !request.has_valid_shape() || !grant.matches(request, now_ms) {
            return Decision::Denied("INVALID_GRANT_OR_ARGUMENTS");
        }
        if !self.policy.allows(request) {
            return Decision::Denied("LOCAL_POLICY");
        }
        let fingerprint = request.journal_fingerprint();
        let attempt_fence = request.attempt_fence.to_string();
        match self.journal.precheck(
            &request.tool_intent_id,
            &fingerprint,
            &attempt_fence,
            &grant.nonce,
        ) {
            Ok(Some(BeginResult::Replay(EffectState::CompletedLocal))) => {
                return Decision::AlreadyCompletedLocal;
            }
            Ok(Some(BeginResult::Replay(EffectState::Failed))) => return Decision::Failed,
            Ok(Some(BeginResult::Replay(EffectState::Started | EffectState::Unknown))) => {
                return Decision::Unknown;
            }
            Ok(Some(BeginResult::IntentConflict)) => return Decision::IntentConflict,
            Ok(Some(BeginResult::GrantReused)) => return Decision::GrantReused,
            Ok(Some(BeginResult::PermitReused)) => return Decision::PermitReused,
            Ok(Some(BeginResult::StartedNew)) => {}
            Ok(None) => {}
            Err(_) => return Decision::StorageError,
        }
        let permit = match self.channel.authorize_start(request, grant, now_ms) {
            Some(value) => value,
            None => return Decision::Denied("START_PERMIT_DENIED"),
        };
        // The cloud mock can drop offline while authorizing; fail closed again.
        if !self.channel.online()
            || self.phase != ConnectionPhase::OnlineMock
            || self.stopped
            || self.revoked
        {
            return Decision::Denied("OFFLINE_OR_STOP_BEFORE_START");
        }
        if !permit.matches(request, grant, now_ms) {
            return Decision::Denied("START_PERMIT_MISMATCH");
        }
        // SQLite uses IMMEDIATE transaction, commits STARTED+one-use Grant/Permit together.
        let begin = self.journal.begin(
            &request.tool_intent_id,
            &fingerprint,
            &attempt_fence,
            &grant.nonce,
            &permit.permit_nonce,
        );
        match begin {
            Ok(BeginResult::Replay(EffectState::CompletedLocal)) => {
                return Decision::AlreadyCompletedLocal;
            }
            Ok(BeginResult::Replay(EffectState::Failed)) => return Decision::Failed,
            Ok(BeginResult::Replay(EffectState::Started | EffectState::Unknown)) => {
                return Decision::Unknown;
            }
            Ok(BeginResult::IntentConflict) => return Decision::IntentConflict,
            Ok(BeginResult::GrantReused) => return Decision::GrantReused,
            Ok(BeginResult::PermitReused) => return Decision::PermitReused,
            Err(error) if error == "LOCAL_STOP_OR_REVOKED" => {
                return Decision::Denied("LOCAL_STOP_OR_REVOKED");
            }
            Err(_) => return Decision::StorageError,
            Ok(BeginResult::StartedNew) => {}
        }
        let delivery = match request.operation {
            Operation::DesktopInspect | Operation::DesktopClick => self.adapters.desktop(request),
            Operation::BrowserInspect | Operation::BrowserClick => self.adapters.chrome(request),
            Operation::BuildRun => self.adapters.build(request),
            Operation::AndroidInstall => self.adapters.android(request),
        };
        let state = match delivery {
            Delivery::LocalOnly => EffectState::CompletedLocal,
            Delivery::Failed => EffectState::Failed,
            Delivery::PossiblyApplied => EffectState::Unknown,
        };
        if self.journal.finish(&request.tool_intent_id, state).is_err() {
            // An uncertain persistence result cannot be called success.
            return Decision::Unknown;
        }
        // COMPLETED_LOCAL is not a Cloud-ledger CONFIRMED or Task completion.
        match state {
            EffectState::CompletedLocal => Decision::LocalCompleted,
            EffectState::Failed => Decision::Failed,
            EffectState::Started | EffectState::Unknown => Decision::Unknown,
        }
    }
}
