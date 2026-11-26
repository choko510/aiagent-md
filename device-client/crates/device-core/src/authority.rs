//! Draft signed authority verification only. This module does not dispatch,
//! authorize a start, consume a permit, or replace Gateway/Sentinel policy.
use crate::{protocol::Epoch, session::FlatFrame};
use ed25519_dalek::{Signature, VerifyingKey};
use serde_json::{Map, Value};
use std::{
    sync::OnceLock,
    time::{SystemTime, UNIX_EPOCH},
};

const SCHEMA: &str = include_str!("../../../contracts/device-authority-v1.schema.json");
const DOMAIN: &[u8] = b"aiagent-device-authority-v1\0";
const BINDING: &[&str] = &[
    "serverOrigin",
    "tenantId",
    "ownerUserId",
    "deviceIdentityId",
    "deviceId",
    "taskId",
    "runId",
    "toolIntentId",
    "operation",
    "goalRevision",
    "leaseEpoch",
    "restoreIncarnation",
    "deviceClaimEpoch",
    "connectionGeneration",
    "attemptFence",
    "policyRevision",
    "revocationGeneration",
    "targetFingerprint",
    "argumentsSha256",
];
const EPOCHS: &[&str] = &[
    "goalRevision",
    "leaseEpoch",
    "restoreIncarnation",
    "deviceClaimEpoch",
    "connectionGeneration",
    "attemptFence",
    "policyRevision",
    "revocationGeneration",
    "issuedAtMs",
    "expiresAtMs",
];
static VALIDATOR: OnceLock<Result<jsonschema::Validator, ()>> = OnceLock::new();

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AuthorityError {
    Invalid,
    Signature,
    Scope,
    Expired,
}

/// Evidence of a verified signature and bindings, not permission to execute.
pub struct VerifiedGrant {
    fields: Map<String, Value>,
    signer: [u8; 32],
}
/// Does not establish single-use, fresh online state, or durable admission.
pub struct VerifiedPermit {
    fields: Map<String, Value>,
}

fn now_ms() -> Result<u64, AuthorityError> {
    u64::try_from(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| AuthorityError::Expired)?
            .as_millis(),
    )
    .map_err(|_| AuthorityError::Expired)
}
fn parse(
    raw: &[u8],
    signature: [u8; 64],
    signer: [u8; 32],
    kind: &str,
    now: u64,
    ttl: u64,
) -> Result<Map<String, Value>, AuthorityError> {
    if raw.len() > 8192 {
        return Err(AuthorityError::Invalid);
    }
    let public = VerifyingKey::from_bytes(&signer).map_err(|_| AuthorityError::Signature)?;
    if public.is_weak() {
        return Err(AuthorityError::Signature);
    }
    let mut message = Vec::with_capacity(DOMAIN.len() + raw.len());
    message.extend_from_slice(DOMAIN);
    message.extend_from_slice(raw);
    public
        .verify_strict(&message, &Signature::from_bytes(&signature))
        .map_err(|_| AuthorityError::Signature)?;
    let frame: FlatFrame = serde_json::from_slice(raw).map_err(|_| AuthorityError::Invalid)?;
    let validator = VALIDATOR
        .get_or_init(|| {
            let schema: Value = serde_json::from_str(SCHEMA).map_err(|_| ())?;
            jsonschema::validator_for(&schema).map_err(|_| ())
        })
        .as_ref()
        .map_err(|_| AuthorityError::Invalid)?;
    if frame.0.get("kind").and_then(Value::as_str) != Some(kind)
        || !validator.is_valid(&Value::Object(frame.0.clone()))
    {
        return Err(AuthorityError::Invalid);
    }
    // IdentityBinding stores the canonical origin. Reject URLs with credentials,
    // paths, query strings or alternate spellings even when signed.
    let origin = frame
        .0
        .get("serverOrigin")
        .and_then(Value::as_str)
        .ok_or(AuthorityError::Invalid)?;
    let url = url::Url::parse(origin).map_err(|_| AuthorityError::Invalid)?;
    if url.scheme() != "https"
        || url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.path() != "/"
        || url.query().is_some()
        || url.fragment().is_some()
        || url.origin().ascii_serialization() != origin
    {
        return Err(AuthorityError::Invalid);
    }
    // Schema regex anchors can have engine-specific newline semantics.
    for name in [
        "tenantId",
        "ownerUserId",
        "deviceIdentityId",
        "deviceId",
        "taskId",
        "runId",
        "toolIntentId",
        "operation",
    ] {
        let value = frame
            .0
            .get(name)
            .and_then(Value::as_str)
            .ok_or(AuthorityError::Invalid)?;
        if value.is_empty()
            || value.len() > 256
            || !value
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"._:@/-".contains(&c))
        {
            return Err(AuthorityError::Invalid);
        }
    }
    for name in EPOCHS {
        Epoch::parse(
            frame
                .0
                .get(*name)
                .and_then(Value::as_str)
                .ok_or(AuthorityError::Invalid)?,
        )
        .map_err(|_| AuthorityError::Invalid)?;
    }
    let get_epoch = |name: &str| -> Result<u64, AuthorityError> {
        Epoch::parse(
            frame
                .0
                .get(name)
                .and_then(Value::as_str)
                .ok_or(AuthorityError::Invalid)?,
        )
        .map(|v| v.0)
        .map_err(|_| AuthorityError::Invalid)
    };
    let issued = get_epoch("issuedAtMs")?;
    let expires = get_epoch("expiresAtMs")?;
    if issued > now || expires <= now || expires <= issued || expires - issued > ttl {
        return Err(AuthorityError::Expired);
    }
    if ["grantNonce", "permitNonce"].iter().any(|name| {
        frame
            .0
            .get(*name)
            .and_then(Value::as_str)
            .is_some_and(|nonce| nonce.bytes().all(|c| c == b'0'))
    }) {
        return Err(AuthorityError::Invalid);
    }
    Ok(frame.0)
}

impl VerifiedGrant {
    /// `expected` and `signer` must come from independently trusted current
    /// state and operator-pinned server configuration, never from the message.
    pub fn verify(
        raw: &[u8],
        signature: [u8; 64],
        signer: [u8; 32],
        expected: &Map<String, Value>,
    ) -> Result<Self, AuthorityError> {
        Self::verify_at(raw, signature, signer, expected, now_ms()?)
    }
    fn verify_at(
        raw: &[u8],
        signature: [u8; 64],
        signer: [u8; 32],
        expected: &Map<String, Value>,
        now: u64,
    ) -> Result<Self, AuthorityError> {
        let fields = parse(raw, signature, signer, "CLOUD_GRANT", now, 120_000)?;
        if expected.len() != BINDING.len()
            || BINDING
                .iter()
                .any(|key| expected.get(*key).is_none() || expected.get(*key) != fields.get(*key))
        {
            return Err(AuthorityError::Scope);
        }
        Ok(Self { fields, signer })
    }
}

impl VerifiedPermit {
    pub fn verify(
        raw: &[u8],
        signature: [u8; 64],
        grant: &VerifiedGrant,
    ) -> Result<Self, AuthorityError> {
        Self::verify_at(raw, signature, grant, now_ms()?)
    }
    fn verify_at(
        raw: &[u8],
        signature: [u8; 64],
        grant: &VerifiedGrant,
        now: u64,
    ) -> Result<Self, AuthorityError> {
        let fields = parse(raw, signature, grant.signer, "START_PERMIT", now, 15_000)?;
        if BINDING
            .iter()
            .chain([&"grantNonce"])
            .any(|key| fields.get(*key) != grant.fields.get(*key))
        {
            return Err(AuthorityError::Scope);
        }
        let value = |map: &Map<String, Value>, name: &str| -> Result<u64, AuthorityError> {
            Epoch::parse(
                map.get(name)
                    .and_then(Value::as_str)
                    .ok_or(AuthorityError::Invalid)?,
            )
            .map(|v| v.0)
            .map_err(|_| AuthorityError::Invalid)
        };
        if value(&grant.fields, "expiresAtMs")? <= now
            || value(&fields, "expiresAtMs")? > value(&grant.fields, "expiresAtMs")?
            || value(&fields, "issuedAtMs")? < value(&grant.fields, "issuedAtMs")?
        {
            return Err(AuthorityError::Expired);
        }
        Ok(Self { fields })
    }
    pub fn nonce(&self) -> &str {
        self.fields["permitNonce"]
            .as_str()
            .expect("validated schema")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::{Signer, SigningKey};
    use serde_json::json;

    fn grant() -> Value {
        json!({"version":"1.0", "kind":"CLOUD_GRANT", "serverOrigin":"https://example.com", "tenantId":"t", "ownerUserId":"o",
            "deviceIdentityId":"di", "deviceId":"d", "taskId":"t1", "runId":"r1", "toolIntentId":"i1",
            "operation":"desktop.click", "goalRevision":"1", "leaseEpoch":"2", "restoreIncarnation":"3",
            "deviceClaimEpoch":"4", "connectionGeneration":"5", "attemptFence":"6", "policyRevision":"7",
            "revocationGeneration":"8", "issuedAtMs":"1000", "expiresAtMs":"61000",
            "targetFingerprint":"a".repeat(64), "argumentsSha256":"b".repeat(64), "grantNonce":"c".repeat(64)})
    }
    fn signed(key: &SigningKey, value: &Value) -> (Vec<u8>, [u8; 64]) {
        let raw = serde_json::to_vec(value).unwrap();
        let mut payload = DOMAIN.to_vec();
        payload.extend_from_slice(&raw);
        (raw, key.sign(&payload).to_bytes())
    }
    fn expected(value: &Value) -> Map<String, Value> {
        BINDING
            .iter()
            .map(|key| ((*key).into(), value[*key].clone()))
            .collect()
    }

    #[test]
    fn signature_scope_freshness_and_permit_binding_are_required() {
        let key = SigningKey::from_bytes(&[7; 32]);
        let public = key.verifying_key().to_bytes();
        let value = grant();
        let (raw, signature) = signed(&key, &value);
        let trusted = expected(&value);
        let verified = VerifiedGrant::verify_at(&raw, signature, public, &trusted, 2_000).unwrap();
        assert!(matches!(
            VerifiedGrant::verify_at(&raw, signature, [0; 32], &trusted, 2_000),
            Err(AuthorityError::Signature)
        ));
        let mut altered = raw.clone();
        altered.push(b' ');
        assert!(matches!(
            VerifiedGrant::verify_at(&altered, signature, public, &trusted, 2_000),
            Err(AuthorityError::Signature)
        ));
        let mut wrong = trusted.clone();
        wrong.insert("ownerUserId".into(), json!("other"));
        assert!(matches!(
            VerifiedGrant::verify_at(&raw, signature, public, &wrong, 2_000),
            Err(AuthorityError::Scope)
        ));
        assert!(matches!(
            VerifiedGrant::verify_at(&raw, signature, public, &trusted, 61_000),
            Err(AuthorityError::Expired)
        ));
        assert!(matches!(
            VerifiedGrant::verify_at(&raw, signature, public, &trusted, 999),
            Err(AuthorityError::Expired)
        ));
        let mut permit = value.clone();
        permit["kind"] = json!("START_PERMIT");
        permit["issuedAtMs"] = json!("2000");
        permit["expiresAtMs"] = json!("12000");
        permit["permitNonce"] = json!("d".repeat(64));
        let (raw_permit, signed_permit) = signed(&key, &permit);
        assert_eq!(
            VerifiedPermit::verify_at(&raw_permit, signed_permit, &verified, 3_000)
                .unwrap()
                .nonce(),
            "d".repeat(64)
        );
        let wrong_key = SigningKey::from_bytes(&[8; 32]);
        let (_, forged) = signed(&wrong_key, &permit);
        assert!(matches!(
            VerifiedPermit::verify_at(&raw_permit, forged, &verified, 3_000),
            Err(AuthorityError::Signature)
        ));
        permit["deviceClaimEpoch"] = json!("9");
        let (raw_other, sig_other) = signed(&key, &permit);
        assert!(matches!(
            VerifiedPermit::verify_at(&raw_other, sig_other, &verified, 3_000),
            Err(AuthorityError::Scope)
        ));
        assert!(matches!(
            VerifiedPermit::verify_at(&raw_permit, signed_permit, &verified, 12_000),
            Err(AuthorityError::Expired)
        ));
        permit["deviceClaimEpoch"] = json!("4");
        permit["permitNonce"] = json!("0".repeat(64));
        let (zero_nonce, zero_sig) = signed(&key, &permit);
        assert!(matches!(
            VerifiedPermit::verify_at(&zero_nonce, zero_sig, &verified, 3_000),
            Err(AuthorityError::Invalid)
        ));
    }

    #[test]
    fn signed_ambiguous_or_noncanonical_payload_is_rejected() {
        let key = SigningKey::from_bytes(&[7; 32]);
        let public = key.verifying_key().to_bytes();
        let mut value = grant();
        value["leaseEpoch"] = json!("01");
        let (raw, sig) = signed(&key, &value);
        assert!(matches!(
            VerifiedGrant::verify_at(&raw, sig, public, &expected(&value), 2_000),
            Err(AuthorityError::Invalid)
        ));
        let good = grant();
        let (raw, _) = signed(&key, &good);
        let mut duplicated = raw[..raw.len() - 1].to_vec();
        duplicated.extend_from_slice(b",\"kind\":\"CLOUD_GRANT\"}");
        let mut payload = DOMAIN.to_vec();
        payload.extend_from_slice(&duplicated);
        assert!(matches!(
            VerifiedGrant::verify_at(
                &duplicated,
                key.sign(&payload).to_bytes(),
                public,
                &expected(&good),
                2_000
            ),
            Err(AuthorityError::Invalid)
        ));
    }

    #[test]
    fn signed_origin_and_identifiers_must_be_canonical() {
        let key = SigningKey::from_bytes(&[7; 32]);
        for origin in [
            "https://",
            "https://user@example.com",
            "https://example.com/path",
            "https://example.com?x=1",
            "https://EXAMPLE.com",
            "https://example.com:443",
            "https://example.com/",
        ] {
            let mut value = grant();
            value["serverOrigin"] = json!(origin);
            let (raw, signature) = signed(&key, &value);
            assert!(
                matches!(
                    VerifiedGrant::verify_at(
                        &raw,
                        signature,
                        key.verifying_key().to_bytes(),
                        &expected(&value),
                        2_000
                    ),
                    Err(AuthorityError::Invalid)
                ),
                "accepted {origin}"
            );
        }
        let mut value = grant();
        value["ownerUserId"] = json!("owner\n");
        let (raw, signature) = signed(&key, &value);
        assert!(matches!(
            VerifiedGrant::verify_at(
                &raw,
                signature,
                key.verifying_key().to_bytes(),
                &expected(&value),
                2_000
            ),
            Err(AuthorityError::Invalid)
        ));
    }
}
