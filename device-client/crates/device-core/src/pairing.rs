use crate::agent::{
    AndroidAdapterPort, AuthenticatedChannelPort, BuildAdapterPort, ChromeAdapterPort,
    CuaAdapterPort, Delivery, LocalPolicy,
};
use crate::protocol::{valid_field, ActionRequest, CloudGrant, StartPermit};
use std::path::{Path, PathBuf};
use std::process::Command;

/// Cloud mTLS / WSS pairing contract. Fails closed unless TLS + client cert + server CA pin are verified.
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

/// Fail-closed cloud transport implementing AuthenticatedChannelPort over verified mTLS/WSS state.
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
        self.config = Some(cfg);
        Ok(())
    }
}

impl AuthenticatedChannelPort for TlsAuthenticatedChannel {
    fn online(&self) -> bool {
        if self.revoked {
            return false;
        }
        self.config.as_ref().is_some_and(|c| c.validate().is_ok())
    }

    fn authorize_start(
        &mut self,
        request: &ActionRequest,
        grant: &CloudGrant,
        now_ms: u64,
    ) -> Option<StartPermit> {
        if !self.online() {
            return None;
        }
        let permit = self.active_permit.take()?;
        if permit.matches(request, grant, now_ms) {
            Some(permit)
        } else {
            None
        }
    }
}

/// Probe-based OS / Chrome / ADB / Build local adapters that fail closed when live OS hosts or devices are absent.
#[derive(Debug, Default)]
pub struct LocalAdapters {
    pub build_root: Option<PathBuf>,
    pub adb_probe_enabled: bool,
    pub chrome_native_host_connected: bool,
    pub desktop_accessibility_ready: bool,
    pub executed_count: usize,
}

impl LocalAdapters {
    fn canonical_workspace_exists(root: &Path, workspace_id: &str) -> bool {
        if !valid_field(workspace_id, 128)
            || workspace_id.contains(['/', '\\', '.'])
            || workspace_id.contains("..")
        {
            return false;
        }
        let candidate = root.join(workspace_id);
        candidate.is_dir()
    }

    fn adb_device_ready(serial: &str) -> bool {
        if !valid_field(serial, 128) || serial.starts_with('-') {
            return false;
        }
        let Ok(out) = Command::new("adb").arg("devices").output() else {
            return false;
        };
        if !out.status.success() {
            return false;
        }
        let stdout = String::from_utf8_lossy(&out.stdout);
        stdout
            .lines()
            .any(|line| line.starts_with(serial) && line.ends_with("\tdevice"))
    }
}

impl CuaAdapterPort for LocalAdapters {
    fn desktop(&mut self, request: &ActionRequest) -> Delivery {
        if !self.desktop_accessibility_ready || !request.target.valid() {
            return Delivery::Failed;
        }
        self.executed_count += 1;
        Delivery::LocalOnly
    }
}

impl ChromeAdapterPort for LocalAdapters {
    fn chrome(&mut self, request: &ActionRequest) -> Delivery {
        if !self.chrome_native_host_connected || !request.target.valid() {
            return Delivery::Failed;
        }
        self.executed_count += 1;
        Delivery::LocalOnly
    }
}

impl BuildAdapterPort for LocalAdapters {
    fn build(&mut self, request: &ActionRequest) -> Delivery {
        let Some(root) = &self.build_root else {
            return Delivery::Failed;
        };
        let crate::protocol::Target::Build { workspace_id } = &request.target else {
            return Delivery::Failed;
        };
        if !Self::canonical_workspace_exists(root, workspace_id) {
            return Delivery::Failed;
        }
        self.executed_count += 1;
        Delivery::LocalOnly
    }
}

impl AndroidAdapterPort for LocalAdapters {
    fn android(&mut self, request: &ActionRequest) -> Delivery {
        if !self.adb_probe_enabled {
            return Delivery::Failed;
        }
        let crate::protocol::Target::Android { serial, .. } = &request.target else {
            return Delivery::Failed;
        };
        if !Self::adb_device_ready(serial) {
            return Delivery::Failed;
        }
        self.executed_count += 1;
        Delivery::LocalOnly
    }
}

pub fn new_default_policy() -> LocalPolicy {
    LocalPolicy::default()
}

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
