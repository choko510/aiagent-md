//! Server-verified outbound WSS only. A connection conveys no device authority.
#[cfg(all(feature = "test-ca", not(debug_assertions)))]
compile_error!("test-ca must not be enabled in release builds");
use futures_util::{SinkExt, StreamExt};
use rustls::{ClientConfig, RootCertStore};
use std::{sync::Arc, time::Duration};
use tokio::{net::TcpStream, time::timeout};
use tokio_tungstenite::{
    connect_async_tls_with_config,
    tungstenite::{protocol::WebSocketConfig, Message},
    Connector, MaybeTlsStream, WebSocketStream,
};
use url::Url;

const MAX_MESSAGE_BYTES: usize = 64 * 1024;
const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
const IO_TIMEOUT: Duration = Duration::from_secs(15);
type Stream = WebSocketStream<MaybeTlsStream<TcpStream>>;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TransportError {
    InvalidEndpoint,
    ConnectFailed,
    Timeout,
    Closed,
    InvalidFrame,
    MessageTooLarge,
    Io,
}

/// Errors and timeouts close the socket; a fresh connection must reauthenticate.
/// This type is not an authenticated device session and cannot authorize actions.
pub struct ServerVerifiedSocket {
    stream: Option<Stream>,
    https_origin: String,
}

impl ServerVerifiedSocket {
    /// Production accepts public WebPKI roots only; there is no insecure verifier.
    pub async fn connect(endpoint: &str) -> Result<Self, TransportError> {
        let roots = RootCertStore::from_iter(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
        Self::connect_with_roots(endpoint, roots).await
    }

    /// Explicit development-test CA; unavailable in release builds.
    #[cfg(feature = "test-ca")]
    pub async fn connect_with_test_ca(
        endpoint: &str,
        roots: RootCertStore,
    ) -> Result<Self, TransportError> {
        Self::connect_with_roots(endpoint, roots).await
    }

    async fn connect_with_roots(
        endpoint: &str,
        roots: RootCertStore,
    ) -> Result<Self, TransportError> {
        let url = validate_endpoint(endpoint)?;
        let mut https_url = url.clone();
        https_url
            .set_scheme("https")
            .map_err(|_| TransportError::InvalidEndpoint)?;
        let https_origin = https_url.origin().ascii_serialization();
        let provider = Arc::new(rustls::crypto::ring::default_provider());
        let config = ClientConfig::builder_with_provider(provider)
            .with_safe_default_protocol_versions()
            .map_err(|_| TransportError::ConnectFailed)?
            .with_root_certificates(roots)
            .with_no_client_auth();
        let ws_config = WebSocketConfig::default()
            .max_message_size(Some(MAX_MESSAGE_BYTES))
            .max_frame_size(Some(MAX_MESSAGE_BYTES));
        let result = timeout(
            CONNECT_TIMEOUT,
            connect_async_tls_with_config(
                url.as_str(),
                Some(ws_config),
                false,
                Some(Connector::Rustls(Arc::new(config))),
            ),
        )
        .await
        .map_err(|_| TransportError::Timeout)?
        .map_err(|_| TransportError::ConnectFailed)?;
        Ok(Self {
            stream: Some(result.0),
            https_origin,
        })
    }

    /// True only while this server-verified socket is present and its origin
    /// matches the identity's canonical HTTPS origin. This is not a heartbeat.
    pub fn matches_https_origin(&self, expected: &str) -> bool {
        self.stream.is_some() && self.https_origin == expected
    }

    pub async fn send_text(&mut self, text: &str) -> Result<(), TransportError> {
        if text.len() > MAX_MESSAGE_BYTES {
            return Err(TransportError::MessageTooLarge);
        }
        // Taking ownership makes cancellation drop the socket rather than leave
        // a possibly partial send usable by a later operation.
        let mut stream = self.stream.take().ok_or(TransportError::Closed)?;
        match timeout(IO_TIMEOUT, stream.send(Message::Text(text.into()))).await {
            Ok(Ok(())) => {
                self.stream = Some(stream);
                Ok(())
            }
            Ok(Err(_)) => Err(TransportError::Io),
            Err(_) => Err(TransportError::Timeout),
        }
    }

    pub async fn receive_text(&mut self) -> Result<String, TransportError> {
        let mut stream = self.stream.take().ok_or(TransportError::Closed)?;
        let next = timeout(IO_TIMEOUT, async {
            loop {
                match stream.next().await {
                    Some(Ok(Message::Text(text))) => return Ok(text.to_string()),
                    Some(Ok(Message::Ping(_) | Message::Pong(_))) => continue,
                    Some(Ok(Message::Close(_))) | None => return Err(TransportError::Closed),
                    Some(Ok(_)) => return Err(TransportError::InvalidFrame),
                    Some(Err(_)) => return Err(TransportError::Io),
                }
            }
        })
        .await;
        match next {
            Ok(Ok(text)) => {
                self.stream = Some(stream);
                Ok(text)
            }
            Ok(Err(error)) => Err(error),
            Err(_) => Err(TransportError::Timeout),
        }
    }

    pub async fn close(&mut self) {
        if let Some(mut stream) = self.stream.take() {
            let _ = timeout(IO_TIMEOUT, stream.close(None)).await;
        }
    }
}

fn validate_endpoint(input: &str) -> Result<Url, TransportError> {
    if input.len() > 2048 || input.chars().any(char::is_whitespace) {
        return Err(TransportError::InvalidEndpoint);
    }
    let url = Url::parse(input).map_err(|_| TransportError::InvalidEndpoint)?;
    if url.scheme() != "wss"
        || url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err(TransportError::InvalidEndpoint);
    }
    Ok(url)
}

#[cfg(test)]
mod tests {
    use super::*;
    use rustls::{pki_types::PrivatePkcs8KeyDer, ServerConfig};
    use tokio::net::TcpListener;
    use tokio_rustls::TlsAcceptor;
    use tokio_tungstenite::accept_async;

    async fn server(mode: u8) -> (u16, rustls::pki_types::CertificateDer<'static>) {
        let rcgen::CertifiedKey { cert, key_pair } =
            rcgen::generate_simple_self_signed(vec!["localhost".into()]).unwrap();
        let cert = cert.der().clone();
        let key = PrivatePkcs8KeyDer::from(key_pair.serialize_der());
        let config =
            ServerConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
                .with_safe_default_protocol_versions()
                .unwrap()
                .with_no_client_auth()
                .with_single_cert(vec![cert.clone()], key.into())
                .unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        tokio::spawn(async move {
            let Ok((stream, _)) = listener.accept().await else {
                return;
            };
            let Ok(tls) = TlsAcceptor::from(Arc::new(config)).accept(stream).await else {
                return;
            };
            let Ok(mut ws) = accept_async(tls).await else {
                return;
            };
            if mode == 1 {
                let _ = ws.send(Message::Binary(vec![1, 2].into())).await;
            } else if mode == 2 {
                let _ = ws
                    .send(Message::Text("x".repeat(MAX_MESSAGE_BYTES + 1).into()))
                    .await;
            } else if let Some(Ok(message)) = ws.next().await {
                let _ = ws.send(message).await;
            }
        });
        (port, cert)
    }

    fn roots(cert: rustls::pki_types::CertificateDer<'static>) -> RootCertStore {
        let mut roots = RootCertStore::empty();
        roots.add(cert).unwrap();
        roots
    }

    #[test]
    fn endpoint_rejects_plaintext_and_credentials() {
        for endpoint in [
            "ws://example.com",
            "https://example.com",
            "wss://u:p@example.com/ws",
            "wss://example.com/ws?token=secret",
            "wss://example.com/ws#fragment",
            "wss://bad host/ws",
        ] {
            assert_eq!(
                validate_endpoint(endpoint),
                Err(TransportError::InvalidEndpoint)
            );
        }
        assert!(validate_endpoint("wss://example.com/device").is_ok());
    }

    #[tokio::test]
    async fn real_tls_wss_exchange_and_message_limit() {
        let (port, cert) = server(0).await;
        let mut socket = ServerVerifiedSocket::connect_with_roots(
            &format!("wss://localhost:{port}/device"),
            roots(cert),
        )
        .await
        .unwrap();
        assert!(socket.matches_https_origin(&format!("https://localhost:{port}")));
        assert!(!socket.matches_https_origin(&format!("https://127.0.0.1:{port}")));
        assert!(!socket.matches_https_origin("https://localhost"));
        assert_eq!(
            socket.send_text(&"x".repeat(MAX_MESSAGE_BYTES + 1)).await,
            Err(TransportError::MessageTooLarge)
        );
        socket.send_text("fixture").await.unwrap();
        assert_eq!(socket.receive_text().await.unwrap(), "fixture");
        socket.close().await;
        assert!(!socket.matches_https_origin(&format!("https://localhost:{port}")));
        assert_eq!(socket.send_text("late").await, Err(TransportError::Closed));
    }

    #[tokio::test]
    async fn certificate_and_hostname_are_verified() {
        let (port, _) = server(0).await;
        let (other_port, other_cert) = server(0).await;
        assert!(ServerVerifiedSocket::connect_with_roots(
            &format!("wss://localhost:{port}/device"),
            roots(other_cert.clone()),
        )
        .await
        .is_err());
        assert!(ServerVerifiedSocket::connect_with_roots(
            &format!("wss://127.0.0.1:{other_port}/device"),
            roots(other_cert),
        )
        .await
        .is_err());
        let (port, _) = server(0).await;
        assert!(
            ServerVerifiedSocket::connect(&format!("wss://localhost:{port}/device"))
                .await
                .is_err()
        );
    }

    #[tokio::test]
    async fn unexpected_binary_frame_invalidates_connection() {
        let (port, cert) = server(1).await;
        let mut socket = ServerVerifiedSocket::connect_with_roots(
            &format!("wss://localhost:{port}/device"),
            roots(cert),
        )
        .await
        .unwrap();
        assert_eq!(
            socket.receive_text().await,
            Err(TransportError::InvalidFrame)
        );
        assert_eq!(socket.send_text("late").await, Err(TransportError::Closed));
    }

    #[tokio::test]
    async fn cancelling_pending_io_drops_connection() {
        let (port, cert) = server(0).await;
        let mut socket = ServerVerifiedSocket::connect_with_roots(
            &format!("wss://localhost:{port}/device"),
            roots(cert),
        )
        .await
        .unwrap();
        assert!(timeout(Duration::from_millis(50), socket.receive_text())
            .await
            .is_err());
        assert_eq!(socket.send_text("late").await, Err(TransportError::Closed));
    }
    #[tokio::test]
    async fn oversized_inbound_message_invalidates_connection() {
        let (port, cert) = server(2).await;
        let mut socket = ServerVerifiedSocket::connect_with_roots(
            &format!("wss://localhost:{port}/device"),
            roots(cert),
        )
        .await
        .unwrap();
        assert!(socket.receive_text().await.is_err());
        assert_eq!(socket.send_text("late").await, Err(TransportError::Closed));
    }
}
