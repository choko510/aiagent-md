//! Local identity primitives. Enrollment and authenticated account binding are
//! not implemented; these APIs are not exposed to the Tauri WebView or Agent.
#[cfg(any(windows, test))]
use crate::protocol::valid_field;
use ed25519_dalek::{Signer, SigningKey};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};
#[cfg(any(windows, test))]
use url::Url;

/// Owner and tenant values must come from an authenticated server session.
/// This type binds them to a local key but does not authenticate those claims.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct IdentityBinding {
    os_user_sid: String,
    tenant_id: String,
    owner_user_id: String,
    device_identity_id: String,
    device_id: String,
    server_origin: String,
    revocation_generation: u64,
}

impl IdentityBinding {
    pub fn current_user(
        tenant_id: &str,
        owner_user_id: &str,
        device_identity_id: &str,
        device_id: &str,
        server_origin: &str,
        revocation_generation: u64,
    ) -> Result<Self, &'static str> {
        #[cfg(windows)]
        {
            let sid = crate::security::current_user_sid().map_err(|_| "OS_IDENTITY_UNAVAILABLE")?;
            Self::from_parts(
                &sid,
                tenant_id,
                owner_user_id,
                device_identity_id,
                device_id,
                server_origin,
                revocation_generation,
            )
        }
        #[cfg(not(windows))]
        {
            let _ = (
                tenant_id,
                owner_user_id,
                device_identity_id,
                device_id,
                server_origin,
                revocation_generation,
            );
            Err("SECURE_IDENTITY_STORE_UNSUPPORTED")
        }
    }

    #[cfg(any(windows, test))]
    fn from_parts(
        sid: &str,
        tenant_id: &str,
        owner_user_id: &str,
        device_identity_id: &str,
        device_id: &str,
        server_origin: &str,
        revocation_generation: u64,
    ) -> Result<Self, &'static str> {
        if ![sid, tenant_id, owner_user_id, device_identity_id, device_id]
            .iter()
            .all(|part| valid_field(part, 256))
            || revocation_generation > i64::MAX as u64
        {
            return Err("INVALID_IDENTITY_BINDING");
        }
        let url = Url::parse(server_origin).map_err(|_| "INVALID_SERVER_ORIGIN")?;
        if url.scheme() != "https"
            || url.host_str().is_none()
            || !url.username().is_empty()
            || url.password().is_some()
            || url.path() != "/"
            || url.query().is_some()
            || url.fragment().is_some()
        {
            return Err("INVALID_SERVER_ORIGIN");
        }
        Ok(Self {
            os_user_sid: sid.into(),
            tenant_id: tenant_id.into(),
            owner_user_id: owner_user_id.into(),
            device_identity_id: device_identity_id.into(),
            device_id: device_id.into(),
            server_origin: url.origin().ascii_serialization(),
            revocation_generation,
        })
    }

    fn digest(&self) -> [u8; 32] {
        let mut hash = Sha256::new();
        hash.update(b"aiagent-device-identity-binding-v1");
        for part in [
            &self.os_user_sid,
            &self.tenant_id,
            &self.owner_user_id,
            &self.device_identity_id,
            &self.device_id,
            &self.server_origin,
        ] {
            hash.update((part.len() as u64).to_be_bytes());
            hash.update(part.as_bytes());
        }
        hash.update(self.revocation_generation.to_be_bytes());
        hash.finalize().into()
    }
}

/// A locally held key. It cannot authorize a Tool or attest an OS process.
pub struct DeviceIdentity {
    binding: IdentityBinding,
    key: SigningKey,
}

impl DeviceIdentity {
    pub fn public_key(&self) -> [u8; 32] {
        self.key.verifying_key().to_bytes()
    }

    /// Opaque local binding digest; the server must separately authenticate the
    /// tenant and owner and obtain explicit confirmation of this key.
    pub fn binding_digest(&self) -> [u8; 32] {
        self.binding.digest()
    }

    /// Candidate pairing possession proof; not a Tool grant or authorization.
    /// The caller must receive the challenge over a verified server connection.
    pub fn sign_pairing_challenge(
        &self,
        pairing_id: [u8; 16],
        nonce: [u8; 32],
        issued_at_ms: u64,
        expires_at_ms: u64,
    ) -> Result<[u8; 64], &'static str> {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_err(|_| "CLOCK_UNAVAILABLE")?
            .as_millis();
        self.sign_pairing_at(
            pairing_id,
            nonce,
            issued_at_ms,
            expires_at_ms,
            u64::try_from(now).map_err(|_| "CLOCK_UNAVAILABLE")?,
        )
    }

    fn sign_pairing_at(
        &self,
        pairing_id: [u8; 16],
        nonce: [u8; 32],
        issued: u64,
        expires: u64,
        now: u64,
    ) -> Result<[u8; 64], &'static str> {
        if pairing_id == [0; 16]
            || nonce == [0; 32]
            || issued > i64::MAX as u64
            || expires > i64::MAX as u64
            || issued > now.saturating_add(5_000)
            || issued < now.saturating_sub(120_000)
            || expires <= now
            || expires <= issued
            || expires.saturating_sub(issued) > 120_000
        {
            return Err("INVALID_CHALLENGE");
        }
        let mut message = b"aiagent-device-pairing-proof-candidate-v1".to_vec();
        message.extend_from_slice(&pairing_id);
        message.extend_from_slice(&self.binding_digest());
        message.extend_from_slice(&self.public_key());
        message.extend_from_slice(&nonce);
        message.extend_from_slice(&issued.to_be_bytes());
        message.extend_from_slice(&expires.to_be_bytes());
        Ok(self.key.sign(&message).to_bytes())
    }

    /// Local candidate only, not a wire contract or evidence of server auth.
    /// The future authenticated protocol must enforce nonce replay and session
    /// generation on the server and must not call this for unverified channels.
    pub fn sign_candidate_challenge(
        &self,
        nonce: [u8; 32],
        connection_generation: u64,
        issued_at_ms: u64,
        expires_at_ms: u64,
    ) -> Result<[u8; 64], &'static str> {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_err(|_| "CLOCK_UNAVAILABLE")?
            .as_millis();
        let now = u64::try_from(now).map_err(|_| "CLOCK_UNAVAILABLE")?;
        self.sign_at(
            nonce,
            connection_generation,
            issued_at_ms,
            expires_at_ms,
            now,
        )
    }

    /// Candidate v2 binds the proof to a server-generated per-socket value.
    /// The caller must verify the TLS origin against this identity before signing.
    pub fn sign_bound_connection_challenge(
        &self,
        nonce: [u8; 32],
        channel_digest: [u8; 32],
        generation: u64,
        issued: u64,
        expires: u64,
    ) -> Result<[u8; 64], &'static str> {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_err(|_| "CLOCK_UNAVAILABLE")?
            .as_millis();
        self.sign_bound_at(
            nonce,
            channel_digest,
            generation,
            issued,
            expires,
            u64::try_from(now).map_err(|_| "CLOCK_UNAVAILABLE")?,
        )
    }

    fn sign_bound_at(
        &self,
        nonce: [u8; 32],
        channel_digest: [u8; 32],
        generation: u64,
        issued: u64,
        expires: u64,
        now: u64,
    ) -> Result<[u8; 64], &'static str> {
        if nonce == [0; 32]
            || channel_digest == [0; 32]
            || generation == 0
            || generation > i64::MAX as u64
            || issued > i64::MAX as u64
            || expires > i64::MAX as u64
            || issued > now.saturating_add(5_000)
            || issued < now.saturating_sub(60_000)
            || expires <= now
            || expires <= issued
            || expires.saturating_sub(issued) > 60_000
        {
            return Err("INVALID_CHALLENGE");
        }
        let mut message = b"aiagent-device-connect-proof-candidate-v2".to_vec();
        message.extend_from_slice(&self.binding.digest());
        message.extend_from_slice(&self.public_key());
        message.extend_from_slice(&nonce);
        message.extend_from_slice(&channel_digest);
        message.extend_from_slice(&generation.to_be_bytes());
        message.extend_from_slice(&issued.to_be_bytes());
        message.extend_from_slice(&expires.to_be_bytes());
        Ok(self.key.sign(&message).to_bytes())
    }

    fn sign_at(
        &self,
        nonce: [u8; 32],
        generation: u64,
        issued: u64,
        expires: u64,
        now: u64,
    ) -> Result<[u8; 64], &'static str> {
        if nonce == [0; 32]
            || generation == 0
            || generation > i64::MAX as u64
            || issued > i64::MAX as u64
            || expires > i64::MAX as u64
            || issued > now.saturating_add(5_000)
            || issued < now.saturating_sub(60_000)
            || expires <= now
            || expires <= issued
            || expires.saturating_sub(issued) > 60_000
        {
            return Err("INVALID_CHALLENGE");
        }
        let mut message = b"aiagent-device-connect-proof-candidate-v1".to_vec();
        message.extend_from_slice(&self.binding.digest());
        message.extend_from_slice(&self.public_key());
        message.extend_from_slice(&nonce);
        message.extend_from_slice(&generation.to_be_bytes());
        message.extend_from_slice(&issued.to_be_bytes());
        message.extend_from_slice(&expires.to_be_bytes());
        Ok(self.key.sign(&message).to_bytes())
    }
}

/// Per-user identity key storage. Non-Windows platforms deliberately fail closed.
pub struct IdentityStore {
    root: PathBuf,
}

impl IdentityStore {
    pub fn open(root: &Path) -> Result<Self, &'static str> {
        #[cfg(windows)]
        {
            windows::open(root)
        }
        #[cfg(not(windows))]
        {
            let _ = root;
            Err("SECURE_IDENTITY_STORE_UNSUPPORTED")
        }
    }

    pub fn create(&self, binding: IdentityBinding) -> Result<DeviceIdentity, &'static str> {
        #[cfg(windows)]
        {
            windows::create(&self.root, binding)
        }
        #[cfg(not(windows))]
        {
            let _ = (&self.root, binding);
            Err("SECURE_IDENTITY_STORE_UNSUPPORTED")
        }
    }

    pub fn load(&self, binding: IdentityBinding) -> Result<DeviceIdentity, &'static str> {
        #[cfg(windows)]
        {
            windows::load(&self.root, binding)
        }
        #[cfg(not(windows))]
        {
            let _ = (&self.root, binding);
            Err("SECURE_IDENTITY_STORE_UNSUPPORTED")
        }
    }
}

#[cfg(windows)]
mod windows {
    use super::*;
    use crate::security::{
        current_user_sid, protect_identity_key, random_bytes, restrict_path_owner_only,
        unprotect_identity_key, verify_owner_only_acl,
    };
    use std::{
        fs::{self, OpenOptions},
        io::{Read, Write},
        os::windows::fs::{MetadataExt, OpenOptionsExt},
    };
    use zeroize::Zeroizing;

    const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
    const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
    const HEADER: &[u8] = b"ADI1";

    fn check_path(path: &Path) -> Result<(), &'static str> {
        use std::path::{Component, Prefix};
        if !path.is_absolute() {
            return Err("INVALID_STORE_PATH");
        }
        if !matches!(path.components().next(), Some(Component::Prefix(prefix))
            if matches!(prefix.kind(), Prefix::Disk(_) | Prefix::VerbatimDisk(_)))
        {
            return Err("INVALID_STORE_PATH");
        }
        for ancestor in path.ancestors() {
            let meta = fs::symlink_metadata(ancestor).map_err(|_| "STORE_PATH_UNAVAILABLE")?;
            if meta.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
                return Err("REPARSE_POINT_DENIED");
            }
        }
        Ok(())
    }

    pub(super) fn open(parent: &Path) -> Result<IdentityStore, &'static str> {
        check_path(parent)?;
        if !parent.is_dir() {
            return Err("INVALID_STORE_PATH");
        }
        // Require an already protected parent before creating anything under it.
        if !verify_owner_only_acl(parent).map_err(|_| "STORE_ACL_FAILED")? {
            return Err("STORE_ACL_FAILED");
        }
        let root = parent.join("device-identities-v1");
        match fs::create_dir(&root) {
            Ok(()) => restrict_path_owner_only(&root, true).map_err(|_| "STORE_ACL_FAILED")?,
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => (),
            Err(_) => return Err("STORE_UNAVAILABLE"),
        }
        check_path(&root)?;
        if !root.is_dir() || !verify_owner_only_acl(&root).map_err(|_| "STORE_ACL_FAILED")? {
            return Err("STORE_ACL_FAILED");
        }
        Ok(IdentityStore { root })
    }

    pub(super) fn checked_path(
        root: &Path,
        binding: &IdentityBinding,
    ) -> Result<PathBuf, &'static str> {
        check_path(root)?;
        if !verify_owner_only_acl(root).map_err(|_| "STORE_ACL_FAILED")?
            || current_user_sid().map_err(|_| "OS_IDENTITY_UNAVAILABLE")? != binding.os_user_sid
        {
            return Err("IDENTITY_SCOPE_MISMATCH");
        }
        Ok(root.join(format!(
            "{}.key",
            crate::protocol::hex_digest(&binding.digest())
        )))
    }

    pub(super) fn create(
        root: &Path,
        binding: IdentityBinding,
    ) -> Result<DeviceIdentity, &'static str> {
        let path = checked_path(root, &binding)?;
        let seed = Zeroizing::new(random_bytes::<32>().map_err(|_| "RANDOM_UNAVAILABLE")?);
        let key = SigningKey::from_bytes(&seed);
        let blob = protect_identity_key(seed.as_ref(), &binding.digest())
            .map_err(|_| "KEY_PROTECTION_FAILED")?;
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
            .open(&path)
            .map_err(|_| "IDENTITY_CREATE_FAILED")?;
        restrict_path_owner_only(&path, false).map_err(|_| "STORE_ACL_FAILED")?;
        if file
            .metadata()
            .map_err(|_| "STORE_UNAVAILABLE")?
            .file_attributes()
            & FILE_ATTRIBUTE_REPARSE_POINT
            != 0
            || !verify_owner_only_acl(&path).map_err(|_| "STORE_ACL_FAILED")?
        {
            return Err("STORE_ACL_FAILED");
        }
        file.write_all(HEADER)
            .and_then(|_| file.write_all(&blob))
            .and_then(|_| file.sync_all())
            .map_err(|_| "IDENTITY_WRITE_FAILED")?;
        Ok(DeviceIdentity { binding, key })
    }

    pub(super) fn load(
        root: &Path,
        binding: IdentityBinding,
    ) -> Result<DeviceIdentity, &'static str> {
        let path = checked_path(root, &binding)?;
        let file = OpenOptions::new()
            .read(true)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
            .open(&path)
            .map_err(|_| "IDENTITY_NOT_FOUND")?;
        let meta = file.metadata().map_err(|_| "STORE_UNAVAILABLE")?;
        if !meta.is_file()
            || meta.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
            || meta.len() > 4096
            || !verify_owner_only_acl(&path).map_err(|_| "STORE_ACL_FAILED")?
        {
            return Err("STORE_ACL_FAILED");
        }
        let mut bytes = Vec::new();
        file.take(4097)
            .read_to_end(&mut bytes)
            .map_err(|_| "IDENTITY_READ_FAILED")?;
        if bytes.len() > 4096 || !bytes.starts_with(HEADER) {
            return Err("IDENTITY_INVALID");
        }
        let seed = Zeroizing::new(
            unprotect_identity_key(&bytes[HEADER.len()..], &binding.digest())
                .map_err(|_| "KEY_UNPROTECTION_FAILED")?,
        );
        let seed_array: &[u8; 32] = seed.as_slice().try_into().map_err(|_| "IDENTITY_INVALID")?;
        Ok(DeviceIdentity {
            binding,
            key: SigningKey::from_bytes(seed_array),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::{Signature, VerifyingKey};

    fn binding(owner: &str) -> IdentityBinding {
        IdentityBinding::from_parts(
            "S-1-5-21-1",
            "tenant",
            owner,
            "identity",
            "device",
            "https://example.com",
            3,
        )
        .unwrap()
    }

    #[test]
    fn candidate_signature_binds_identity_nonce_and_generation() {
        let identity = DeviceIdentity {
            binding: binding("alice"),
            key: SigningKey::from_bytes(&[7; 32]),
        };
        let sig = identity.sign_at([9; 32], 4, 1_000, 31_000, 2_000).unwrap();
        let vector: serde_json::Value =
            serde_json::from_str(include_str!("../../../fixtures/connection-proof-v1.json"))
                .unwrap();
        assert_eq!(
            crate::protocol::hex_digest(&sig),
            vector["signatureHex"].as_str().unwrap()
        );
        let v2: serde_json::Value =
            serde_json::from_str(include_str!("../../../fixtures/connection-proof-v2.json"))
                .unwrap();
        let bound = identity
            .sign_bound_at([9; 32], [8; 32], 4, 1_000, 31_000, 2_000)
            .unwrap();
        assert_eq!(
            crate::protocol::hex_digest(&bound),
            v2["signatureHex"].as_str().unwrap()
        );
        assert!(identity
            .sign_bound_at([9; 32], [0; 32], 4, 1_000, 31_000, 2_000)
            .is_err());
        let mut message = b"aiagent-device-connect-proof-candidate-v1".to_vec();
        message.extend_from_slice(&identity.binding.digest());
        message.extend_from_slice(&identity.public_key());
        message.extend_from_slice(&[9; 32]);
        message.extend_from_slice(&4u64.to_be_bytes());
        message.extend_from_slice(&1_000u64.to_be_bytes());
        message.extend_from_slice(&31_000u64.to_be_bytes());
        let public = VerifyingKey::from_bytes(&identity.public_key()).unwrap();
        assert!(public
            .verify_strict(&message, &Signature::from_bytes(&sig))
            .is_ok());
        message[42] ^= 1;
        assert!(public
            .verify_strict(&message, &Signature::from_bytes(&sig))
            .is_err());
        assert_ne!(binding("alice").digest(), binding("bob").digest());
        let different_origin = IdentityBinding::from_parts(
            "S-1-5-21-1",
            "tenant",
            "alice",
            "identity",
            "device",
            "https://other.example",
            3,
        )
        .unwrap();
        let revoked_generation = IdentityBinding::from_parts(
            "S-1-5-21-1",
            "tenant",
            "alice",
            "identity",
            "device",
            "https://example.com",
            4,
        )
        .unwrap();
        assert_ne!(binding("alice").digest(), different_origin.digest());
        assert_ne!(binding("alice").digest(), revoked_generation.digest());
        assert!(identity.sign_at([0; 32], 4, 1_000, 31_000, 2_000).is_err());
        assert!(identity.sign_at([9; 32], 0, 1_000, 31_000, 2_000).is_err());
        assert!(identity.sign_at([9; 32], 4, 1_000, 61_001, 2_000).is_err());
        assert!(identity.sign_at([9; 32], 4, 1_000, 2_000, 2_000).is_err());
        assert!(IdentityBinding::from_parts(
            "sid",
            "t",
            "o",
            "i",
            "d",
            "https://u:p@example.com",
            1
        )
        .is_err());
        assert!(
            IdentityBinding::from_parts("sid", "t", "o", "i", "d", "http://example.com", 1)
                .is_err()
        );
    }

    #[test]
    fn pairing_proof_matches_shared_public_test_vector() {
        let vector: serde_json::Value =
            serde_json::from_str(include_str!("../../../fixtures/pairing-proof-v1.json")).unwrap();
        let identity = DeviceIdentity {
            binding: binding("alice"),
            key: SigningKey::from_bytes(&[7; 32]),
        };
        let signature = identity
            .sign_pairing_at([1; 16], [9; 32], 1_000, 61_000, 2_000)
            .unwrap();
        assert_eq!(
            crate::protocol::hex_digest(&identity.binding_digest()),
            vector["bindingDigestHex"].as_str().unwrap()
        );
        assert_eq!(
            crate::protocol::hex_digest(&identity.public_key()),
            vector["publicKeyHex"].as_str().unwrap()
        );
        assert_eq!(
            crate::protocol::hex_digest(&signature),
            vector["signatureHex"].as_str().unwrap()
        );
        assert!(identity
            .sign_pairing_at([0; 16], [9; 32], 1_000, 61_000, 2_000)
            .is_err());
        assert!(identity
            .sign_pairing_at([1; 16], [9; 32], 1_000, 121_001, 2_000)
            .is_err());
    }

    #[cfg(not(windows))]
    #[test]
    fn no_plaintext_fallback_when_secure_store_missing() {
        assert!(matches!(
            IdentityStore::open(Path::new("/tmp")),
            Err("SECURE_IDENTITY_STORE_UNSUPPORTED")
        ));
        assert!(matches!(
            IdentityBinding::current_user("t", "o", "i", "d", "https://example.com", 1),
            Err("SECURE_IDENTITY_STORE_UNSUPPORTED")
        ));
    }

    #[cfg(windows)]
    #[test]
    fn dpapi_keys_are_distinct_scoped_and_not_replaceable() {
        use crate::security::{restrict_path_owner_only, verify_owner_only_acl};
        let dir = tempfile::tempdir().unwrap();
        restrict_path_owner_only(dir.path(), true).unwrap();
        let store = IdentityStore::open(dir.path()).unwrap();
        let alice = IdentityBinding::current_user("t", "alice", "i", "d", "https://example.com", 1)
            .unwrap();
        let bob =
            IdentityBinding::current_user("t", "bob", "i", "d", "https://example.com", 1).unwrap();
        let a = store.create(alice.clone()).unwrap();
        let b = store.create(bob.clone()).unwrap();
        assert_ne!(a.public_key(), b.public_key());
        assert_eq!(
            a.public_key(),
            store.load(alice.clone()).unwrap().public_key()
        );
        assert!(store.create(alice.clone()).is_err());
        let a_path = windows::checked_path(&store.root, &alice).unwrap();
        let b_path = windows::checked_path(&store.root, &bob).unwrap();
        assert!(verify_owner_only_acl(&a_path).unwrap());
        let mut encrypted = std::fs::read(&a_path).unwrap();
        let secret = zeroize::Zeroizing::new(a.key.to_bytes());
        assert!(!encrypted
            .windows(secret.len())
            .any(|window| window == secret.as_ref()));
        std::fs::copy(&a_path, b_path).unwrap();
        assert!(store.load(bob).is_err());
        let last = encrypted.last_mut().unwrap();
        *last ^= 1;
        std::fs::write(a_path, encrypted).unwrap();
        assert!(store.load(alice).is_err());
        assert!(matches!(
            IdentityStore::open(Path::new(r"\\server\share")),
            Err("INVALID_STORE_PATH")
        ));
    }
}
