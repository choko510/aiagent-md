use serde::{de, Deserialize, Deserializer, Serialize, Serializer};
use sha2::{Digest, Sha256};
use std::fmt;

/// Canonical signed-int64 nonnegative decimal string. Never serialize epochs as JSON numbers.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Epoch(pub u64);

impl Epoch {
    pub fn parse(value: &str) -> Result<Self, &'static str> {
        if value.is_empty()
            || (value.len() > 1 && value.starts_with('0'))
            || !value.bytes().all(|b| b.is_ascii_digit())
        {
            return Err("noncanonical decimal epoch");
        }
        let number: u64 = value.parse().map_err(|_| "epoch overflow")?;
        if number > i64::MAX as u64 {
            return Err("epoch exceeds signed int64");
        }
        Ok(Self(number))
    }
}
impl Serialize for Epoch {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&self.0.to_string())
    }
}
impl<'de> Deserialize<'de> for Epoch {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let value = String::deserialize(deserializer)?;
        Epoch::parse(&value).map_err(de::Error::custom)
    }
}
impl fmt::Display for Epoch {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Scope {
    pub os_user: String,
    pub tenant_id: String,
    pub owner_user_id: String,
    pub device_identity_id: String,
    pub device_id: String,
}
impl Scope {
    /// Only a namespace digest reaches disk; separate encrypted per-owner files are a later gate.
    pub fn namespace(&self) -> String {
        let mut hasher = Sha256::new();
        for part in [&self.os_user, &self.tenant_id, &self.owner_user_id,
                     &self.device_identity_id, &self.device_id] {
            hasher.update((part.len() as u64).to_be_bytes());
            hasher.update(part.as_bytes());
        }
        hex_digest(&hasher.finalize())
    }
    pub fn valid(&self) -> bool {
        [&self.os_user, &self.tenant_id, &self.owner_user_id,
         &self.device_identity_id, &self.device_id]
            .iter().all(|s| !s.trim().is_empty() && s.len() <= 256)
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Fence {
    pub device_claim_epoch: Epoch,
    pub lease_epoch: Epoch,
    pub restore_incarnation: Epoch,
    pub connection_generation: Epoch,
    pub goal_revision: Epoch,
    pub policy_revision: Epoch,
}
impl Fence {
    pub fn valid(&self) -> bool {
        [self.device_claim_epoch, self.lease_epoch, self.restore_incarnation,
         self.connection_generation, self.goal_revision, self.policy_revision]
            .iter().all(|value| value.0 <= i64::MAX as u64)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Operation {
    DesktopInspect,
    DesktopClick,
    BrowserInspect,
    BrowserClick,
    BuildRun,
    AndroidInstall,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Target {
    Desktop {
        app_id: String,
        window_token: String,
        process_generation: String,
        browser_like: bool,
    },
    Browser {
        origin: String,
        profile_id: String,
        tab_id: String,
        document_generation: String,
    },
    Build { workspace_id: String },
    Android { serial: String, package_id: String },
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ActionRequest {
    pub scope: Scope,
    pub fence: Fence,
    pub tool_intent_id: String,
    pub attempt_fence: Epoch,
    pub operation: Operation,
    pub target: Target,
    /// Digest of validated arguments; derived from bytes, not from model-supplied hash.
    pub args_sha256: String,
    /// This mock deliberately does not persist or log argument bytes.
    pub args: Vec<u8>,
}
impl ActionRequest {
    pub fn new(scope: Scope, fence: Fence, tool_intent_id: String, attempt_fence: Epoch,
               operation: Operation, target: Target, args: Vec<u8>) -> Self {
        let args_sha256 = hex_digest(&Sha256::digest(&args));
        Self { scope, fence, tool_intent_id, attempt_fence, operation,
               target, args_sha256, args }
    }
    pub fn has_valid_shape(&self) -> bool {
        !self.tool_intent_id.trim().is_empty() && self.tool_intent_id.len() <= 256
            && self.args.len() <= 64 * 1024
            && self.scope.valid()
            && self.args_sha256 == hex_digest(&Sha256::digest(&self.args))
            && self.attempt_fence.0 <= i64::MAX as u64
            && self.fence.valid()
    }
}

/// MOCK-ONLY placeholder for an authenticated Cloud grant; NOT a cryptographic signature.
#[derive(Clone, Debug)]
pub struct CloudGrant {
    pub scope: Scope,
    pub fence: Fence,
    pub tool_intent_id: String,
    pub attempt_fence: Epoch,
    pub args_sha256: String,
    pub operation: Operation,
    pub target: Target,
    pub nonce: String,
    pub expires_at_ms: u64,
}
impl CloudGrant {
    pub fn matches(&self, req: &ActionRequest, now_ms: u64) -> bool {
        self.scope == req.scope && self.fence == req.fence
            && self.tool_intent_id == req.tool_intent_id
            && self.attempt_fence == req.attempt_fence
            && self.args_sha256 == req.args_sha256 && self.operation == req.operation
            && self.target == req.target && !self.nonce.is_empty()
            && self.expires_at_ms > now_ms
    }
}

/// MOCK-ONLY start permit. Real implementation MUST verify server authentication and CAS.
#[derive(Clone, Debug)]
pub struct StartPermit {
    pub grant: CloudGrant,
    pub permit_nonce: String,
    pub expires_at_ms: u64,
}
impl StartPermit {
    pub fn matches(&self, req: &ActionRequest, grant: &CloudGrant, now_ms: u64) -> bool {
        self.grant.matches(req, now_ms)
            && self.grant.nonce == grant.nonce
            && self.expires_at_ms > now_ms
            && self.expires_at_ms <= grant.expires_at_ms
            && !self.permit_nonce.is_empty()
    }
}
pub fn hex_digest(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push(DIGITS[(b >> 4) as usize] as char);
        out.push(DIGITS[(b & 15) as usize] as char);
    }
    out
}
