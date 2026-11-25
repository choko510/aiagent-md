use crate::agent::{
    AndroidAdapterPort, AuthenticatedChannelPort, BuildAdapterPort, ChromeAdapterPort,
    CuaAdapterPort, Delivery, LocalPolicy,
};
use crate::protocol::{valid_field, ActionRequest, CloudGrant, StartPermit};
use std::path::PathBuf;

/// Legacy configuration only. Boolean fields do not prove a TLS handshake.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CloudPairingConfig {
    pub endpoint: String,
    pub ca_pin_sha256: String,
    pub client_cert_present: bool,
    pub tls_handshake_verified: bool,
}

impl CloudPairingConfig {
    pub fn validate(&self) -> Result<(), &'static str> {
        if !valid_field(&self.endpoint, 512)
            || (!self.endpoint.starts_with("https://") && !self.endpoint.starts_with("wss://"))
            || self.endpoint.chars().any(char::is_whitespace)
        {
            return Err("INVALID_PAIRING_ENDPOINT");
        }
        if self.ca_pin_sha256.len() != 64
            || !self
                .ca_pin_sha256
                .bytes()
                .all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
        {
            return Err("INVALID_CA_PIN");
        }
        if !self.client_cert_present || !self.tls_handshake_verified {
            return Err("MTLS_HANDSHAKE_REQUIRED");
        }
        Ok(())
    }
}

/// Placeholder: no TLS transport or authenticated permits are implemented.
#[derive(Debug, Default)]
pub struct TlsAuthenticatedChannel {
    pub config: Option<CloudPairingConfig>,
    pub revoked: bool,
    pub active_permit: Option<StartPermit>,
}

impl TlsAuthenticatedChannel {
    pub fn unpaired() -> Self {
        Self::default()
    }

    pub fn pair(&mut self, cfg: CloudPairingConfig) -> Result<(), &'static str> {
        cfg.validate()?;
        if self.revoked {
            return Err("PAIRING_REVOKED");
        }
        // Configuration and caller-supplied booleans are not authentication evidence.
        Err("AUTHENTICATED_TRANSPORT_UNAVAILABLE")
    }
}

impl AuthenticatedChannelPort for TlsAuthenticatedChannel {
    fn online(&self) -> bool {
        false
    }

    fn authorize_start(
        &mut self,
        _request: &ActionRequest,
        _grant: &CloudGrant,
        _now_ms: u64,
    ) -> Option<StartPermit> {
        None
    }
}

/// Unimplemented adapters. Flags and probes cannot establish that an operation ran.
#[derive(Debug, Default)]
pub struct LocalAdapters {
    pub build_root: Option<PathBuf>,
    pub adb_probe_enabled: bool,
    pub chrome_native_host_connected: bool,
    pub desktop_accessibility_ready: bool,
    pub executed_count: usize,
}

impl CuaAdapterPort for LocalAdapters {
    fn desktop(&mut self, _request: &ActionRequest) -> Delivery {
        Delivery::Failed
    }
}
impl ChromeAdapterPort for LocalAdapters {
    fn chrome(&mut self, _request: &ActionRequest) -> Delivery {
        Delivery::Failed
    }
}
impl BuildAdapterPort for LocalAdapters {
    fn build(&mut self, _request: &ActionRequest) -> Delivery {
        Delivery::Failed
    }
}
impl AndroidAdapterPort for LocalAdapters {
    fn android(&mut self, _request: &ActionRequest) -> Delivery {
        Delivery::Failed
    }
}

pub fn new_default_policy() -> LocalPolicy {
    LocalPolicy::default()
}

/// Constructs an unsigned simulator fixture. It must never authorize device I/O.
pub fn make_start_permit(
    _request: &ActionRequest,
    grant: &CloudGrant,
    permit_nonce: &str,
    expires_at_ms: u64,
) -> StartPermit {
    StartPermit {
        grant: grant.clone(),
        permit_nonce: permit_nonce.into(),
        expires_at_ms,
    }
}
