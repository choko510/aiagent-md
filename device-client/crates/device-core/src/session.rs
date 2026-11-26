//! Candidate device connection client. It has no Tool dispatch or authority.
use crate::{identity::DeviceIdentity, protocol::Epoch};
use aiagent_device_transport::ServerVerifiedSocket;
use serde::de::{Error, MapAccess, Visitor};
use serde::{Deserialize, Deserializer};
use serde_json::{json, Map, Value};
use std::{
    fmt,
    sync::OnceLock,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

const MAX_FRAME: usize = 8192;
const SCHEMA: &str = include_str!("../../../contracts/device-connection-v1.schema.json");
static VALIDATOR: OnceLock<Result<jsonschema::Validator, ()>> = OnceLock::new();

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionError {
    InvalidFrame,
    Offline,
    SigningDenied,
    Expired,
}

// All fields of this candidate protocol are strings. Reject duplicate keys,
// nesting, unknown fields and non-string values before schema validation.
pub(crate) struct FlatFrame(pub(crate) Map<String, Value>);
impl<'de> Deserialize<'de> for FlatFrame {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct FrameVisitor;
        impl<'de> Visitor<'de> for FrameVisitor {
            type Value = FlatFrame;
            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("a unique-key string map")
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
                let mut out = Map::new();
                while let Some((key, value)) = map.next_entry::<String, String>()? {
                    if value.chars().any(char::is_control)
                        || out.insert(key, Value::String(value)).is_some()
                    {
                        return Err(A::Error::custom("invalid or duplicate field"));
                    }
                }
                Ok(FlatFrame(out))
            }
        }
        deserializer.deserialize_map(FrameVisitor)
    }
}

fn decode(raw: &str, expected: &str) -> Result<Map<String, Value>, SessionError> {
    if raw.len() > MAX_FRAME {
        return Err(SessionError::InvalidFrame);
    }
    let frame: FlatFrame = serde_json::from_str(raw).map_err(|_| SessionError::InvalidFrame)?;
    let validator = VALIDATOR
        .get_or_init(|| {
            let schema: Value = serde_json::from_str(SCHEMA).map_err(|_| ())?;
            jsonschema::validator_for(&schema).map_err(|_| ())
        })
        .as_ref()
        .map_err(|_| SessionError::InvalidFrame)?;
    if frame.0.get("kind").and_then(Value::as_str) != Some(expected)
        || !validator.is_valid(&Value::Object(frame.0.clone()))
    {
        return Err(SessionError::InvalidFrame);
    }
    for name in ["generation", "sequence", "issuedMs", "expiresMs"] {
        if let Some(value) = frame.0.get(name) {
            let epoch = Epoch::parse(value.as_str().ok_or(SessionError::InvalidFrame)?)
                .map_err(|_| SessionError::InvalidFrame)?;
            if epoch.0 == 0 {
                return Err(SessionError::InvalidFrame);
            }
        }
    }
    Ok(frame.0)
}

fn field<'a>(frame: &'a Map<String, Value>, key: &str) -> Result<&'a str, SessionError> {
    frame
        .get(key)
        .and_then(Value::as_str)
        .ok_or(SessionError::InvalidFrame)
}
fn integer(frame: &Map<String, Value>, key: &str) -> Result<u64, SessionError> {
    Epoch::parse(field(frame, key)?)
        .map(|v| v.0)
        .map_err(|_| SessionError::InvalidFrame)
}
fn hex32(value: &str) -> Result<[u8; 32], SessionError> {
    if value.len() != 64 {
        return Err(SessionError::InvalidFrame);
    }
    let mut out = [0; 32];
    for (idx, byte) in out.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&value[idx * 2..idx * 2 + 2], 16)
            .map_err(|_| SessionError::InvalidFrame)?;
    }
    Ok(out)
}
fn now_ms() -> Result<u64, SessionError> {
    u64::try_from(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| SessionError::Expired)?
            .as_millis(),
    )
    .map_err(|_| SessionError::Expired)
}
async fn send(
    socket: &mut ServerVerifiedSocket,
    frame: Value,
    kind: &str,
) -> Result<(), SessionError> {
    let raw = serde_json::to_string(&frame).map_err(|_| SessionError::InvalidFrame)?;
    decode(&raw, kind)?;
    socket
        .send_text(&raw)
        .await
        .map_err(|_| SessionError::Offline)
}
async fn receive(
    socket: &mut ServerVerifiedSocket,
    kind: &str,
) -> Result<Map<String, Value>, SessionError> {
    let raw = socket
        .receive_text()
        .await
        .map_err(|_| SessionError::Offline)?;
    decode(&raw, kind)
}

/// Only an authenticated connection candidate; no grant, action or ONLINE claim.
pub struct DeviceSession {
    socket: Option<ServerVerifiedSocket>,
    identity: DeviceIdentity,
    channel_digest: [u8; 32],
    generation: u64,
    expires_ms: u64,
    sequence: u64,
    deadline: Instant,
    last_wall_ms: u64,
}
impl DeviceSession {
    pub async fn connect(
        mut socket: ServerVerifiedSocket,
        identity: DeviceIdentity,
    ) -> Result<Self, SessionError> {
        if !identity.matches_socket(&socket) {
            return Err(SessionError::SigningDenied);
        }
        let (tenant, owner, id) = identity.connection_scope();
        send(&mut socket, json!({"version":"1.1", "kind":"HELLO", "tenantId":tenant, "ownerId":owner, "identityId":id}), "HELLO").await?;
        let challenge = receive(&mut socket, "CHALLENGE").await?;
        let generation = integer(&challenge, "generation")?;
        let channel_digest = hex32(field(&challenge, "channelDigest")?)?;
        let signature = identity
            .sign_bound_connection_challenge(
                &socket,
                hex32(field(&challenge, "nonce")?)?,
                channel_digest,
                generation,
                integer(&challenge, "issuedMs")?,
                integer(&challenge, "expiresMs")?,
            )
            .map_err(|_| SessionError::SigningDenied)?;
        send(&mut socket, json!({"version":"1.1", "kind":"PROOF", "signature":crate::protocol::hex_digest(&signature)}), "PROOF").await?;
        let accepted = receive(&mut socket, "ACCEPTED").await?;
        let expires_ms = integer(&accepted, "expiresMs")?;
        let now = now_ms()?;
        if integer(&accepted, "generation")? != generation
            || expires_ms <= now
            || expires_ms > now.saturating_add(125_000)
        {
            return Err(SessionError::InvalidFrame);
        }
        Ok(Self {
            socket: Some(socket),
            identity,
            channel_digest,
            generation,
            expires_ms,
            sequence: 0,
            deadline: Instant::now()
                .checked_add(Duration::from_millis(expires_ms - now))
                .ok_or(SessionError::InvalidFrame)?,
            last_wall_ms: now,
        })
    }

    /// A successful acknowledgement is evidence of one heartbeat, not a Tool grant.
    pub async fn heartbeat(&mut self) -> Result<(), SessionError> {
        let mut socket = self.socket.take().ok_or(SessionError::Offline)?;
        let start_wall = now_ms()?;
        if start_wall < self.last_wall_ms
            || start_wall >= self.expires_ms
            || Instant::now() >= self.deadline
        {
            return Err(SessionError::Expired);
        }
        let next = self
            .sequence
            .checked_add(1)
            .filter(|n| *n <= i64::MAX as u64)
            .ok_or(SessionError::InvalidFrame)?;
        let signature = self
            .identity
            .sign_heartbeat(&socket, self.channel_digest, self.generation, next)
            .map_err(|_| SessionError::SigningDenied)?;
        send(&mut socket, json!({"version":"1.1", "kind":"HEARTBEAT", "generation":self.generation.to_string(), "sequence":next.to_string(), "signature":crate::protocol::hex_digest(&signature)}), "HEARTBEAT").await?;
        let ack = receive(&mut socket, "HEARTBEAT_ACK").await?;
        let end_wall = now_ms()?;
        if integer(&ack, "generation")? != self.generation
            || integer(&ack, "sequence")? != next
            || end_wall < start_wall
            || end_wall >= self.expires_ms
            || Instant::now() >= self.deadline
        {
            return Err(SessionError::InvalidFrame);
        }
        self.sequence = next;
        self.last_wall_ms = end_wall;
        self.socket = Some(socket);
        Ok(())
    }
    pub async fn close(&mut self) {
        if let Some(mut socket) = self.socket.take() {
            socket.close().await;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::{Signature, VerifyingKey};
    use futures_util::{SinkExt, StreamExt};
    use rustls::{pki_types::PrivatePkcs8KeyDer, RootCertStore, ServerConfig};
    use std::sync::Arc;
    use tokio::net::TcpListener;
    use tokio_rustls::TlsAcceptor;
    use tokio_tungstenite::{
        accept_hdr_async,
        tungstenite::{
            handshake::server::{Request, Response},
            http::HeaderValue,
            Message,
        },
    };

    #[test]
    fn strict_schema_rejects_duplicate_unknown_and_overflow() {
        for raw in [
            r#"{"version":"1.1","version":"1.1","kind":"ACCEPTED","generation":"1","expiresMs":"2"}"#,
            r#"{"version":"2.0","kind":"ACCEPTED","generation":"1","expiresMs":"2"}"#,
            r#"{"version":"1.1","kind":"ACCEPTED","generation":"9223372036854775808","expiresMs":"2"}"#,
            r#"{"version":"1.1","kind":"ACCEPTED","generation":"1","expiresMs":"2","extra":"x"}"#,
            r#"{"version":"1.1","kind":"ACCEPTED","generation":1,"expiresMs":"2"}"#,
        ] {
            assert_eq!(decode(raw, "ACCEPTED"), Err(SessionError::InvalidFrame));
        }
    }

    #[tokio::test]
    #[allow(clippy::result_large_err)] // tungstenite's required server callback error type
    async fn rust_client_authenticates_and_heartbeats_over_real_tls() {
        let rcgen::CertifiedKey { cert, key_pair } =
            rcgen::generate_simple_self_signed(vec!["localhost".into()]).unwrap();
        let cert = cert.der().clone();
        let key = PrivatePkcs8KeyDer::from(key_pair.serialize_der());
        let tls =
            ServerConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
                .with_safe_default_protocol_versions()
                .unwrap()
                .with_no_client_auth()
                .with_single_cert(vec![cert.clone()], key.into())
                .unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let identity =
            crate::identity::test_identity_for_origin(&format!("https://localhost:{port}"));
        let public = identity.public_key();
        let binding = identity.binding_digest();
        let server = tokio::spawn(async move {
            let tls = Arc::new(tls);
            let (stream, _) = listener.accept().await.unwrap();
            let first_tls = TlsAcceptor::from(tls.clone()).accept(stream).await.unwrap();
            let mut first =
                accept_hdr_async(first_tls, |_request: &Request, mut response: Response| {
                    response.headers_mut().insert(
                        "Sec-WebSocket-Protocol",
                        HeaderValue::from_static("aiagent-device.v1"),
                    );
                    Ok(response)
                })
                .await
                .unwrap();
            assert!(!matches!(first.next().await, Some(Ok(Message::Text(_)))));
            let (stream, _) = listener.accept().await.unwrap();
            let tls = TlsAcceptor::from(tls).accept(stream).await.unwrap();
            let mut ws = accept_hdr_async(tls, |_request: &Request, mut response: Response| {
                response.headers_mut().insert(
                    "Sec-WebSocket-Protocol",
                    HeaderValue::from_static("aiagent-device.v1"),
                );
                Ok(response)
            })
            .await
            .unwrap();
            let hello = ws.next().await.unwrap().unwrap().into_text().unwrap();
            assert_eq!(
                field(&decode(&hello, "HELLO").unwrap(), "identityId").unwrap(),
                "01010101-0101-0101-0101-010101010101"
            );
            let issued = now_ms().unwrap();
            let expires = issued + 60_000;
            ws.send(Message::Text(json!({"version":"1.1", "kind":"CHALLENGE", "nonce":"09".repeat(32),
                "channelDigest":"08".repeat(32), "generation":"1", "issuedMs":issued.to_string(), "expiresMs":expires.to_string()}).to_string().into())).await.unwrap();
            let proof = decode(
                &ws.next().await.unwrap().unwrap().into_text().unwrap(),
                "PROOF",
            )
            .unwrap();
            let text = field(&proof, "signature").unwrap();
            let mut signature = [0u8; 64];
            for (index, byte) in signature.iter_mut().enumerate() {
                *byte = u8::from_str_radix(&text[index * 2..index * 2 + 2], 16).unwrap();
            }
            let mut payload = b"aiagent-device-connect-proof-candidate-v2".to_vec();
            payload.extend_from_slice(&binding);
            payload.extend_from_slice(&public);
            payload.extend_from_slice(&[9; 32]);
            payload.extend_from_slice(&[8; 32]);
            payload.extend_from_slice(&1u64.to_be_bytes());
            payload.extend_from_slice(&issued.to_be_bytes());
            payload.extend_from_slice(&expires.to_be_bytes());
            VerifyingKey::from_bytes(&public)
                .unwrap()
                .verify_strict(&payload, &Signature::from_bytes(&signature))
                .unwrap();
            ws.send(Message::Text(json!({"version":"1.1", "kind":"ACCEPTED", "generation":"1", "expiresMs":(issued+120_000).to_string()}).to_string().into())).await.unwrap();
            let heartbeat = decode(
                &ws.next().await.unwrap().unwrap().into_text().unwrap(),
                "HEARTBEAT",
            )
            .unwrap();
            assert_eq!(field(&heartbeat, "sequence").unwrap(), "1");
            let heartbeat_sig = field(&heartbeat, "signature").unwrap();
            let mut heartbeat_signature = [0u8; 64];
            for (index, byte) in heartbeat_signature.iter_mut().enumerate() {
                *byte = u8::from_str_radix(&heartbeat_sig[index * 2..index * 2 + 2], 16).unwrap();
            }
            let mut heartbeat_payload = b"aiagent-device-heartbeat-candidate-v1".to_vec();
            heartbeat_payload.extend_from_slice(&binding);
            heartbeat_payload.extend_from_slice(&public);
            heartbeat_payload.extend_from_slice(&[8; 32]);
            heartbeat_payload.extend_from_slice(&1u64.to_be_bytes());
            heartbeat_payload.extend_from_slice(&1u64.to_be_bytes());
            VerifyingKey::from_bytes(&public)
                .unwrap()
                .verify_strict(
                    &heartbeat_payload,
                    &Signature::from_bytes(&heartbeat_signature),
                )
                .unwrap();
            ws.send(Message::Text(
                json!({"version":"1.1", "kind":"HEARTBEAT_ACK", "generation":"1", "sequence":"1"})
                    .to_string()
                    .into(),
            ))
            .await
            .unwrap();
            let second = decode(
                &ws.next().await.unwrap().unwrap().into_text().unwrap(),
                "HEARTBEAT",
            )
            .unwrap();
            assert_eq!(field(&second, "sequence").unwrap(), "2");
            ws.send(Message::Text(
                json!({"version":"1.1", "kind":"HEARTBEAT_ACK", "generation":"1", "sequence":"1"})
                    .to_string()
                    .into(),
            ))
            .await
            .unwrap();
            let _ = ws.next().await;
        });
        let mut roots = RootCertStore::empty();
        roots.add(cert).unwrap();
        let wrong = crate::identity::test_identity_for_origin("https://other.example");
        let wrong_socket = ServerVerifiedSocket::connect_with_test_ca(
            &format!("wss://localhost:{port}/v1/device/connect"),
            roots.clone(),
        )
        .await
        .unwrap();
        assert!(matches!(
            DeviceSession::connect(wrong_socket, wrong).await,
            Err(SessionError::SigningDenied)
        ));
        let socket = ServerVerifiedSocket::connect_with_test_ca(
            &format!("wss://localhost:{port}/v1/device/connect"),
            roots,
        )
        .await
        .unwrap();
        let mut session = DeviceSession::connect(socket, identity).await.unwrap();
        session.heartbeat().await.unwrap();
        assert_eq!(session.heartbeat().await, Err(SessionError::InvalidFrame));
        assert_eq!(session.heartbeat().await, Err(SessionError::Offline));
        session.close().await;
        assert_eq!(session.heartbeat().await, Err(SessionError::Offline));
        server.await.unwrap();
    }
}
