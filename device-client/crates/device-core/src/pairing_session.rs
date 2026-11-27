//! Candidate enrollment over an already server-verified socket. No Tool authority.
//! Bootstrap values must be obtained from an authenticated owner session by the caller.
use crate::{identity::DeviceIdentity, protocol::hex_digest, session::FlatFrame};
use aiagent_device_transport::ServerVerifiedSocket;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::{
    sync::OnceLock,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use zeroize::Zeroizing;

const SCHEMA: &str = include_str!("../../../contracts/device-pairing-v1.schema.json");
static VALIDATOR: OnceLock<Result<jsonschema::Validator, ()>> = OnceLock::new();

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PairingError {
    Invalid,
    Offline,
    SigningDenied,
}

fn decode(raw: &str, kind: &str) -> Result<Map<String, Value>, PairingError> {
    if raw.len() > 8192 {
        return Err(PairingError::Invalid);
    }
    let frame: FlatFrame = serde_json::from_str(raw).map_err(|_| PairingError::Invalid)?;
    let validator = VALIDATOR
        .get_or_init(|| {
            let schema: Value = serde_json::from_str(SCHEMA).map_err(|_| ())?;
            jsonschema::validator_for(&schema).map_err(|_| ())
        })
        .as_ref()
        .map_err(|_| PairingError::Invalid)?;
    if frame.0.get("kind").and_then(Value::as_str) != Some(kind)
        || !validator.is_valid(&Value::Object(frame.0.clone()))
    {
        return Err(PairingError::Invalid);
    }
    Ok(frame.0)
}

fn uuid_string(bytes: [u8; 16]) -> String {
    let hex = hex_digest(&bytes);
    format!(
        "{}-{}-{}-{}-{}",
        &hex[..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..]
    )
}

/// The bootstrap token is short-lived and never exposed through this object's API.
pub struct PairingBootstrap {
    pub pairing_id: [u8; 16],
    pub nonce: [u8; 32],
    pub issued_ms: u64,
    pub expires_ms: u64,
    token: Zeroizing<[u8; 32]>,
}
impl PairingBootstrap {
    pub fn new(
        pairing_id: [u8; 16],
        nonce: [u8; 32],
        issued_ms: u64,
        expires_ms: u64,
        token: [u8; 32],
    ) -> Result<Self, PairingError> {
        if token == [0; 32] {
            return Err(PairingError::Invalid);
        }
        Ok(Self {
            pairing_id,
            nonce,
            issued_ms,
            expires_ms,
            token: Zeroizing::new(token),
        })
    }
}

/// Holds a single in-flight pairing. Dropping it closes the connection.
pub struct PairingSession {
    socket: ServerVerifiedSocket,
    identity: DeviceIdentity,
    code: Zeroizing<String>,
    expires_ms: u64,
    last_wall_ms: u64,
    deadline: Instant,
}
fn now_ms() -> Result<u64, PairingError> {
    u64::try_from(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| PairingError::Invalid)?
            .as_millis(),
    )
    .map_err(|_| PairingError::Invalid)
}
fn still_valid(
    now: u64,
    previous: u64,
    expires: u64,
    monotonic_now: Instant,
    deadline: Instant,
) -> bool {
    now >= previous && now < expires && monotonic_now < deadline
}
impl PairingSession {
    pub async fn attach(
        mut socket: ServerVerifiedSocket,
        identity: DeviceIdentity,
        bootstrap: PairingBootstrap,
    ) -> Result<Self, PairingError> {
        let signature = identity
            .sign_pairing_challenge(
                &socket,
                bootstrap.pairing_id,
                bootstrap.nonce,
                bootstrap.issued_ms,
                bootstrap.expires_ms,
            )
            .map_err(|_| PairingError::SigningDenied)?;
        let (tenant, owner, _, _) = identity.pairing_scope();
        if [tenant, owner].iter().any(|value| {
            value.is_empty()
                || value.len() > 256
                || !value
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || b"._:-".contains(&c))
        }) {
            return Err(PairingError::Invalid);
        }
        let token = Zeroizing::new(hex_digest(bootstrap.token.as_ref()));
        let raw = Zeroizing::new(format!("{{\"version\":\"1.0\",\"kind\":\"ATTACH\",\"tenantId\":{},\"ownerId\":{},\"pairingId\":\"{}\",\"token\":\"{}\",\"publicKey\":\"{}\",\"bindingDigest\":\"{}\",\"signature\":\"{}\"}}", serde_json::to_string(tenant).map_err(|_| PairingError::Invalid)?, serde_json::to_string(owner).map_err(|_| PairingError::Invalid)?, uuid_string(bootstrap.pairing_id), *token, hex_digest(&identity.public_key()), hex_digest(&identity.binding_digest()), hex_digest(&signature)));
        socket
            .send_text(&raw)
            .await
            .map_err(|_| PairingError::Offline)?;
        let response = Zeroizing::new(
            socket
                .receive_text()
                .await
                .map_err(|_| PairingError::Offline)?,
        );
        let mut frame = decode(&response, "CODE")?;
        if frame.get("publicKeySha256").and_then(Value::as_str)
            != Some(hex_digest(&Sha256::digest(identity.public_key())).as_str())
            || frame.get("bindingDigest").and_then(Value::as_str)
                != Some(hex_digest(&identity.binding_digest()).as_str())
        {
            return Err(PairingError::Invalid);
        }
        let Some(Value::String(code)) = frame.remove("code") else {
            return Err(PairingError::Invalid);
        };
        let now = now_ms()?;
        if now < bootstrap.issued_ms || now >= bootstrap.expires_ms {
            return Err(PairingError::Invalid);
        }
        let deadline = Instant::now()
            .checked_add(Duration::from_millis(bootstrap.expires_ms - now))
            .ok_or(PairingError::Invalid)?;
        Ok(Self {
            socket,
            identity,
            code: Zeroizing::new(code),
            expires_ms: bootstrap.expires_ms,
            last_wall_ms: now,
            deadline,
        })
    }
    /// Display locally to the user for separately authenticated owner approval.
    pub fn verification_code(&self) -> &str {
        &self.code
    }
    /// Call only after owner approval. A lost reply is ambiguous; do not retry blindly.
    pub async fn finish(mut self) -> Result<DeviceIdentity, PairingError> {
        let now = now_ms()?;
        if !still_valid(
            now,
            self.last_wall_ms,
            self.expires_ms,
            Instant::now(),
            self.deadline,
        ) {
            return Err(PairingError::Invalid);
        }
        let raw = json!({"version":"1.0", "kind":"CONSUME"}).to_string();
        self.socket
            .send_text(&raw)
            .await
            .map_err(|_| PairingError::Offline)?;
        let response = self
            .socket
            .receive_text()
            .await
            .map_err(|_| PairingError::Offline)?;
        let frame = decode(&response, "PAIRED")?;
        let (_, _, identity, device) = self.identity.pairing_scope();
        if frame.get("identityId").and_then(Value::as_str) != Some(identity)
            || frame.get("deviceId").and_then(Value::as_str) != Some(device)
        {
            return Err(PairingError::Invalid);
        }
        self.socket.close().await;
        Ok(self.identity)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_expiry_and_clock_rollback_before_consumption() {
        let now = Instant::now();
        let deadline = now + Duration::from_secs(1);
        assert!(still_valid(101, 100, 200, now, deadline));
        assert!(!still_valid(99, 100, 200, now, deadline));
        assert!(!still_valid(200, 100, 200, now, deadline));
        assert!(!still_valid(101, 100, 200, deadline, deadline));
    }
}
