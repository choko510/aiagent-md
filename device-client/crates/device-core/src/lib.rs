//! LDC-1 device agent simulator. No production transport, OS input, or cloud approval.
//! The only adapter implementation is a no-OS-effect mock.
pub mod agent;
pub mod authority;
pub mod discovery;
mod git_probe;
pub mod identity;
pub mod journal;
pub mod mock;
pub mod pairing;
pub mod protocol;
pub mod security;
pub mod session;

pub use agent::{Agent, ConnectionPhase, Decision, Delivery, LocalPolicy};
pub use journal::{BeginResult, ControlRecord, EffectState, LocalJournal};
pub use pairing::{make_start_permit, CloudPairingConfig, LocalAdapters, TlsAuthenticatedChannel};
pub use protocol::{
    ActionRequest, CloudGrant, Epoch, Fence, Operation, Scope, StartPermit, Target,
};
