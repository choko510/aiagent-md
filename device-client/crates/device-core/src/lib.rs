//! LDC-1 device agent simulator. No production transport, OS input, or cloud approval.
//! The only adapter implementation is a no-OS-effect mock.
pub mod agent;
pub mod journal;
pub mod mock;
pub mod protocol;

pub use agent::{Agent, ConnectionPhase, Decision, LocalPolicy};
pub use journal::{BeginResult, EffectState, LocalJournal};
pub use protocol::{ActionRequest, CloudGrant, Epoch, Fence, Operation, Scope, StartPermit, Target};
