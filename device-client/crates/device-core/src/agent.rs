use crate::journal::{BeginResult, EffectState, LocalJournal};
use crate::protocol::{ActionRequest, CloudGrant, Fence, Operation, Scope, StartPermit, Target};
use std::collections::HashSet;

/// These traits are capability boundaries, NOT a licence to forward raw MCP/CDP/shell.
pub trait AuthenticatedChannelPort {
    fn online(&self) -> bool;
    /// Production adapter must validate an authenticated server START_PERMIT and CAS state.
    fn authorize_start(&mut self, request: &ActionRequest, grant: &CloudGrant,
                       now_ms: u64) -> Option<StartPermit>;
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Delivery { LocalOnly, PossiblyApplied }
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
pub trait AdapterPorts: CuaAdapterPort + ChromeAdapterPort + BuildAdapterPort + AndroidAdapterPort {}
impl<T: CuaAdapterPort + ChromeAdapterPort + BuildAdapterPort + AndroidAdapterPort> AdapterPorts for T {}

/// Configured locally by the user; Cloud permission is always separately required.
#[derive(Default)]
pub struct LocalPolicy {
    pub desktop_apps: HashSet<String>,
    pub browser_origins: HashSet<String>,
    pub allow_mock_build: bool,
    pub allow_mock_android: bool,
}
impl LocalPolicy {
    fn allows(&self, request: &ActionRequest) -> bool {
        match (&request.operation, &request.target) {
            (Operation::DesktopInspect | Operation::DesktopClick,
             Target::Desktop { app_id, window_token, process_generation, browser_like }) => {
                // Positive allowlist prevents desktop-input browser-origin bypass.
                !browser_like && !window_token.is_empty() && !process_generation.is_empty()
                    && self.desktop_apps.contains(app_id)
            }
            (Operation::BrowserInspect | Operation::BrowserClick,
             Target::Browser { origin, profile_id, tab_id, document_generation }) => {
                !profile_id.is_empty() && !tab_id.is_empty() && !document_generation.is_empty()
                    && self.browser_origins.contains(origin)
            }
            (Operation::BuildRun, Target::Build { workspace_id }) =>
                self.allow_mock_build && !workspace_id.is_empty(),
            (Operation::AndroidInstall, Target::Android { serial, package_id }) =>
                self.allow_mock_android && !serial.is_empty() && !package_id.is_empty(),
            _ => false,
        }
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ConnectionPhase { Unpaired, PairedOffline, OnlineMock, Stale, Revoked }

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Decision {
    Denied(&'static str),
    LocalCompleted,
    AlreadyCompletedLocal,
    Unknown,
    IntentConflict,
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
    pub fn new(scope: Scope, fence: Fence, channel: C, adapters: A,
               policy: LocalPolicy, journal: LocalJournal) -> Self {
        let phase = if channel.online() { ConnectionPhase::OnlineMock }
                    else { ConnectionPhase::PairedOffline };
        Self { scope, fence, channel, adapters, policy, journal, phase,
               stopped: false, revoked: false }
    }
    pub fn stop(&mut self) { self.stopped = true; }
    pub fn revoke(&mut self) {
        self.revoked = true;
        self.stopped = true;
        self.phase = ConnectionPhase::Revoked;
    }
    pub fn phase(&self) -> ConnectionPhase { self.phase }
    pub fn disconnect(&mut self) {
        if !self.revoked { self.phase = ConnectionPhase::PairedOffline; }
    }
    pub fn mark_stale(&mut self) {
        if !self.revoked { self.phase = ConnectionPhase::Stale; }
    }
    /// Simulator-only reconnect. An old connection generation can never be revived.
    pub fn reconnect_mock(&mut self, next_fence: Fence) -> Result<(), &'static str> {
        if self.stopped || self.revoked || !next_fence.valid()
            || next_fence.connection_generation <= self.fence.connection_generation
            || !self.channel.online()
        {
            return Err("RECONNECT_DENIED");
        }
        self.fence = next_fence;
        self.phase = ConnectionPhase::OnlineMock;
        Ok(())
    }
    pub fn stopped(&self) -> bool { self.stopped }
    pub fn revoked(&self) -> bool { self.revoked }
    pub fn effect(&self, intent_id: &str) -> Result<Option<EffectState>, String> {
        self.journal.state(intent_id)
    }
    pub fn execute(&mut self, request: &ActionRequest,
                   grant: &CloudGrant, now_ms: u64) -> Decision {
        // No offline starts, including "already approved" grants.
        if self.stopped || self.revoked { return Decision::Denied("LOCAL_STOP_OR_REVOKED"); }
        if self.phase != ConnectionPhase::OnlineMock || !self.channel.online() {
            return Decision::Denied("OFFLINE");
        }
        if request.scope != self.scope || request.fence != self.fence {
            return Decision::Denied("STALE_SCOPE_OR_FENCE");
        }
        if !request.has_valid_shape() || !grant.matches(request, now_ms) {
            return Decision::Denied("INVALID_GRANT_OR_ARGUMENTS");
        }
        if !self.policy.allows(request) { return Decision::Denied("LOCAL_POLICY"); }
        let permit = match self.channel.authorize_start(request, grant, now_ms) {
            Some(value) => value,
            None => return Decision::Denied("START_PERMIT_DENIED"),
        };
        // The cloud mock can drop offline while authorizing; fail closed again.
        if !self.channel.online() || self.phase != ConnectionPhase::OnlineMock
            || self.stopped || self.revoked {
            return Decision::Denied("OFFLINE_OR_STOP_BEFORE_START");
        }
        if !permit.matches(request, grant, now_ms) {
            return Decision::Denied("START_PERMIT_MISMATCH");
        }
        // SQLite uses IMMEDIATE transaction, commits STARTED+one-use Permit together.
        let begin = self.journal.begin(&request.tool_intent_id, &request.journal_fingerprint(),
                                       &request.attempt_fence.to_string(), &permit.permit_nonce);
        match begin {
            Ok(BeginResult::Replay(EffectState::CompletedLocal)) => {
                return Decision::AlreadyCompletedLocal;
            }
            Ok(BeginResult::Replay(_)) => return Decision::Unknown,
            Ok(BeginResult::IntentConflict) => return Decision::IntentConflict,
            Ok(BeginResult::PermitReused) => return Decision::PermitReused,
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
            Delivery::PossiblyApplied => EffectState::Unknown,
        };
        if self.journal.finish(&request.tool_intent_id, state).is_err() {
            // An uncertain persistence result cannot be called success.
            return Decision::Unknown;
        }
        // COMPLETED_LOCAL is not a Cloud-ledger CONFIRMED or Task completion.
        if state == EffectState::CompletedLocal { Decision::LocalCompleted }
        else { Decision::Unknown }
    }
}
