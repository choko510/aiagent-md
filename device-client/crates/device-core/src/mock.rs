use crate::agent::{
    AndroidAdapterPort, AuthenticatedChannelPort, BuildAdapterPort, ChromeAdapterPort,
    CuaAdapterPort, Delivery,
};
use crate::protocol::{ActionRequest, CloudGrant, StartPermit};

/// Stand-in for Cloud Gateway. Does not perform TLS, pairing, signing, or authorization.
pub struct MockChannel {
    pub connected: bool,
    pub approve: bool,
    pub drop_connection_on_authorize: bool,
    pub fixed_permit_nonce: Option<String>,
    pub authorize_calls: usize,
}
impl MockChannel {
    pub fn online_mock() -> Self {
        Self {
            connected: true,
            approve: true,
            drop_connection_on_authorize: false,
            fixed_permit_nonce: None,
            authorize_calls: 0,
        }
    }
}
impl AuthenticatedChannelPort for MockChannel {
    fn online(&self) -> bool {
        self.connected
    }
    fn authorize_start(
        &mut self,
        req: &ActionRequest,
        grant: &CloudGrant,
        now_ms: u64,
    ) -> Option<StartPermit> {
        self.authorize_calls += 1;
        if !self.connected || !self.approve || !grant.matches(req, now_ms) {
            return None;
        }
        self.connected = !self.drop_connection_on_authorize;
        let nonce = self
            .fixed_permit_nonce
            .clone()
            .unwrap_or_else(|| format!("mock-permit-{}", grant.nonce));
        Some(StartPermit {
            grant: grant.clone(),
            permit_nonce: nonce,
            expires_at_ms: grant.expires_at_ms,
        })
    }
}

/// Strictly local counter and failure injector. Never launches processes or touches Chrome/USB.
#[derive(Default)]
pub struct MockAdapters {
    pub calls: usize,
    pub deliver_unknown: bool,
}
impl MockAdapters {
    fn invoke(&mut self) -> Delivery {
        self.calls += 1;
        if self.deliver_unknown {
            Delivery::PossiblyApplied
        } else {
            Delivery::LocalOnly
        }
    }
}
impl CuaAdapterPort for MockAdapters {
    fn desktop(&mut self, _: &ActionRequest) -> Delivery {
        self.invoke()
    }
}
impl ChromeAdapterPort for MockAdapters {
    fn chrome(&mut self, _: &ActionRequest) -> Delivery {
        self.invoke()
    }
}
impl BuildAdapterPort for MockAdapters {
    fn build(&mut self, _: &ActionRequest) -> Delivery {
        self.invoke()
    }
}
impl AndroidAdapterPort for MockAdapters {
    fn android(&mut self, _: &ActionRequest) -> Delivery {
        self.invoke()
    }
}

/// The only grant constructor provided in this crate; simulator tests ONLY.
pub fn mock_grant(request: &ActionRequest, nonce: &str, expires_at_ms: u64) -> CloudGrant {
    CloudGrant {
        scope: request.scope.clone(),
        fence: request.fence.clone(),
        tool_intent_id: request.tool_intent_id.clone(),
        attempt_fence: request.attempt_fence,
        args_sha256: request.args_sha256.clone(),
        operation: request.operation,
        target: request.target.clone(),
        nonce: nonce.into(),
        expires_at_ms,
    }
}
