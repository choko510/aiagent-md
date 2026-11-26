//! Debug-only integration fixture. Uses a real Windows DPAPI key and test CA.
//! It is not an agent executable or a production enrollment interface.
#[cfg(not(windows))]
fn main() {
    panic!("Windows DPAPI fixture required");
}

#[cfg(windows)]
#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    use aiagent_device_core::{
        identity::{IdentityBinding, IdentityStore},
        session::DeviceSession,
    };
    use aiagent_device_transport::ServerVerifiedSocket;
    use rustls::{pki_types::CertificateDer, RootCertStore};
    use serde_json::{json, Value};
    use std::{
        io::{self, BufRead, Write},
        path::Path,
    };

    fn field<'a>(v: &'a Value, k: &str) -> Result<&'a str, &'static str> {
        v.get(k).and_then(Value::as_str).ok_or("INVALID_FIXTURE")
    }
    fn bytes<const N: usize>(s: &str) -> Result<[u8; N], &'static str> {
        if s.len() != N * 2 || !s.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err("INVALID_FIXTURE");
        }
        let mut result = [0; N];
        for (i, b) in result.iter_mut().enumerate() {
            *b = u8::from_str_radix(&s[i * 2..i * 2 + 2], 16).map_err(|_| "INVALID_FIXTURE")?;
        }
        Ok(result)
    }
    let mut input = io::stdin().lock();
    let mut line = String::new();
    input.read_line(&mut line)?;
    let v: Value = serde_json::from_str(&line)?;
    let root = Path::new(field(&v, "root")?);
    aiagent_device_core::security::restrict_path_owner_only(root, true)?;
    let binding = IdentityBinding::current_user(
        field(&v, "tenant")?,
        field(&v, "owner")?,
        field(&v, "identity")?,
        field(&v, "device")?,
        field(&v, "origin")?,
        0,
    )?;
    let store = IdentityStore::open(root)?;
    let created = store.create(binding.clone())?;
    let identity = store.load(binding)?;
    if identity.public_key() != created.public_key() {
        return Err("IDENTITY_RELOAD_MISMATCH".into());
    }
    drop(created);
    let mut roots = RootCertStore::empty();
    roots.add(CertificateDer::from(std::fs::read(field(&v, "certDer")?)?))?;
    let socket = ServerVerifiedSocket::connect_with_test_ca(field(&v, "url")?, roots)
        .await
        .map_err(|_| "TLS_FAILED")?;
    let signature = identity.sign_pairing_challenge(
        &socket,
        bytes(field(&v, "pairingIdHex")?)?,
        bytes(field(&v, "nonceHex")?)?,
        field(&v, "issuedMs")?.parse()?,
        field(&v, "expiresMs")?.parse()?,
    )?;
    println!(
        "{}",
        json!({"kind":"PROOF", "public":aiagent_device_core::protocol::hex_digest(&identity.public_key()),
        "binding":aiagent_device_core::protocol::hex_digest(&identity.binding_digest()),
        "signature":aiagent_device_core::protocol::hex_digest(&signature)})
    );
    io::stdout().flush()?;
    line.clear();
    input.read_line(&mut line)?;
    if line.trim() != "CONTINUE" {
        return Err("FIXTURE_STOPPED".into());
    }
    let mut session = DeviceSession::connect(socket, identity)
        .await
        .map_err(|_| "CONNECT_FAILED")?;
    session.heartbeat().await.map_err(|_| "HEARTBEAT_FAILED")?;
    session.heartbeat().await.map_err(|_| "HEARTBEAT_FAILED")?;
    println!("{}", json!({"kind":"OK"}));
    io::stdout().flush()?;
    session.close().await;
    Ok(())
}
