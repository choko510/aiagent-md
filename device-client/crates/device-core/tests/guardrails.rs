use aiagent_device_core::journal::EffectState;
use aiagent_device_core::mock::{mock_grant, MockAdapters, MockChannel};
use aiagent_device_core::{
    ActionRequest, Agent, Decision, Epoch, Fence, LocalJournal, LocalPolicy, Operation, Scope,
    Target,
};
use tempfile::tempdir;

fn scope(owner: &str) -> Scope {
    Scope {
        os_user: "sid-1".into(),
        tenant_id: "tenant-1".into(),
        owner_user_id: owner.into(),
        device_identity_id: "device-identity".into(),
        device_id: "device-1".into(),
    }
}
fn fence() -> Fence {
    Fence {
        device_claim_epoch: Epoch(4),
        lease_epoch: Epoch(3),
        restore_incarnation: Epoch(2),
        connection_generation: Epoch(9),
        goal_revision: Epoch(1),
        policy_revision: Epoch(2),
    }
}
fn request(owner: &str, intent: &str, op: Operation) -> ActionRequest {
    let target = match op {
        Operation::DesktopInspect | Operation::DesktopClick => Target::Desktop {
            app_id: "mock.notepad".into(),
            window_token: "window-2".into(),
            process_generation: "process-3".into(),
            browser_like: false,
        },
        Operation::BrowserInspect | Operation::BrowserClick => Target::Browser {
            origin: "https://example.com".into(),
            profile_id: "test".into(),
            tab_id: "tab".into(),
            document_generation: "doc2".into(),
        },
        Operation::BuildRun => Target::Build {
            workspace_id: "ws-1".into(),
        },
        Operation::AndroidInstall => Target::Android {
            serial: "test-serial".into(),
            package_id: "test.package".into(),
        },
    };
    ActionRequest::new(
        scope(owner),
        fence(),
        intent.into(),
        Epoch(7),
        op,
        target,
        b"mock-argument".to_vec(),
    )
}
fn policy() -> LocalPolicy {
    let mut p = LocalPolicy::default();
    p.desktop_apps.insert("mock.notepad".into());
    p
}
fn new_agent(path: &std::path::Path, owner: &str) -> Agent<MockChannel, MockAdapters> {
    Agent::new(
        scope(owner),
        fence(),
        MockChannel::online_mock(),
        MockAdapters::default(),
        policy(),
        LocalJournal::open(path, &scope(owner)).unwrap(),
    )
}
#[test]
fn canonical_epochs_are_decimal_strings_not_numbers() {
    assert_eq!(
        serde_json::to_string(&Epoch(9007199254740992)).unwrap(),
        "\"9007199254740992\""
    );
    assert_eq!(
        serde_json::from_str::<Epoch>("\"9223372036854775806\"")
            .unwrap()
            .0,
        9223372036854775806
    );
    for bad in [
        "1",
        "\"01\"",
        "\"-1\"",
        "\"1e3\"",
        "\"9223372036854775808\"",
    ] {
        assert!(serde_json::from_str::<Epoch>(bad).is_err(), "{bad}");
    }
}
#[test]
fn stale_grant_and_request_hash_mismatch_do_not_execute() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("journal.db");
    let mut a = new_agent(&path, "A");
    let mut r = request("A", "one", Operation::DesktopClick);
    let g = mock_grant(&r, "nonce", 200);
    r.fence.device_claim_epoch = Epoch(5);
    assert_eq!(
        a.execute(&r, &g, 100),
        Decision::Denied("STALE_SCOPE_OR_FENCE")
    );
    let mut r = request("A", "one", Operation::DesktopClick);
    let g = mock_grant(&r, "nonce", 200);
    r.args[0] = b'X';
    assert_eq!(
        a.execute(&r, &g, 100),
        Decision::Denied("INVALID_GRANT_OR_ARGUMENTS")
    );
    assert_eq!(a.adapters.calls, 0);
}
#[test]
fn offline_and_connection_drop_refuse_start() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("journal.db");
    let mut a = new_agent(&path, "A");
    let r = request("A", "one", Operation::DesktopClick);
    let g = mock_grant(&r, "nonce", 200);
    a.channel.connected = false;
    assert_eq!(a.execute(&r, &g, 100), Decision::Denied("OFFLINE"));
    a.channel.connected = true;
    a.channel.drop_connection_on_authorize = true;
    assert_eq!(
        a.execute(&r, &g, 100),
        Decision::Denied("OFFLINE_OR_STOP_BEFORE_START")
    );
    assert_eq!(a.adapters.calls, 0);
}
#[test]
fn permit_reuse_across_intents_is_rejected() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("journal.db");
    let mut a = new_agent(&path, "A");
    a.channel.fixed_permit_nonce = Some("same-permit".into());
    let r1 = request("A", "one", Operation::DesktopClick);
    let r2 = request("A", "two", Operation::DesktopClick);
    assert_eq!(
        a.execute(&r1, &mock_grant(&r1, "n1", 200), 100),
        Decision::LocalCompleted
    );
    assert_eq!(
        a.execute(&r2, &mock_grant(&r2, "n2", 200), 100),
        Decision::PermitReused
    );
    assert_eq!(a.adapters.calls, 1);
}
#[test]
fn owner_isolation_and_no_receipt_cross_owner() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("journal.db");
    let mut a = new_agent(&path, "A");
    let r = request("A", "one", Operation::DesktopClick);
    assert_eq!(
        a.execute(&r, &mock_grant(&r, "n", 200), 100),
        Decision::LocalCompleted
    );
    let mut b = new_agent(&path, "B");
    assert_eq!(b.effect("one").unwrap(), None);
    assert_eq!(
        b.execute(&r, &mock_grant(&r, "n2", 200), 100),
        Decision::Denied("STALE_SCOPE_OR_FENCE")
    );
    assert_eq!(b.adapters.calls, 0);
}
#[test]
fn restarted_started_is_unknown_and_never_auto_retried() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("journal.db");
    let r = request("A", "one", Operation::DesktopClick);
    {
        let mut journal = LocalJournal::open(&path, &scope("A")).unwrap();
        assert_eq!(
            journal
                .begin(&r.tool_intent_id, &r.journal_fingerprint(), "7", "permit")
                .unwrap(),
            aiagent_device_core::BeginResult::StartedNew
        );
        assert_eq!(journal.state("one").unwrap(), Some(EffectState::Started));
    }
    let mut a = new_agent(&path, "A");
    assert_eq!(a.effect("one").unwrap(), Some(EffectState::Unknown));
    assert_eq!(
        a.execute(&r, &mock_grant(&r, "other", 200), 100),
        Decision::Unknown
    );
    assert_eq!(a.adapters.calls, 0);
}
#[test]
fn unknown_is_not_transformed_into_success_or_retried() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("journal.db");
    let mut a = new_agent(&path, "A");
    a.adapters.deliver_unknown = true;
    let r = request("A", "one", Operation::DesktopClick);
    let g = mock_grant(&r, "nonce", 200);
    assert_eq!(a.execute(&r, &g, 100), Decision::Unknown);
    assert_eq!(a.execute(&r, &g, 100), Decision::Unknown);
    assert_eq!(a.adapters.calls, 1);
}
#[test]
fn stop_and_revoke_refuse_new_actions() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("journal.db");
    let mut a = new_agent(&path, "A");
    let r = request("A", "one", Operation::DesktopClick);
    let g = mock_grant(&r, "nonce", 200);
    a.stop();
    assert_eq!(
        a.execute(&r, &g, 100),
        Decision::Denied("LOCAL_STOP_OR_REVOKED")
    );
    a.revoke();
    assert_eq!(
        a.execute(&r, &g, 100),
        Decision::Denied("LOCAL_STOP_OR_REVOKED")
    );
    assert_eq!(a.adapters.calls, 0);
}
#[test]
fn desktop_browser_boundary_and_unknown_app_are_denied() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("journal.db");
    let mut a = new_agent(&path, "A");
    let mut r = request("A", "one", Operation::DesktopClick);
    if let Target::Desktop { browser_like, .. } = &mut r.target {
        *browser_like = true;
    }
    assert_eq!(
        a.execute(&r, &mock_grant(&r, "nonce", 200), 100),
        Decision::Denied("LOCAL_POLICY")
    );
    let mut r = request("A", "two", Operation::DesktopClick);
    if let Target::Desktop { app_id, .. } = &mut r.target {
        *app_id = "chrome.exe".into();
    }
    assert_eq!(
        a.execute(&r, &mock_grant(&r, "nonce2", 200), 100),
        Decision::Denied("LOCAL_POLICY")
    );
    assert_eq!(a.adapters.calls, 0);
}
#[test]
fn build_android_browser_are_disabled_without_individual_consent() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("journal.db");
    let mut a = new_agent(&path, "A");
    for op in [
        Operation::BuildRun,
        Operation::AndroidInstall,
        Operation::BrowserClick,
    ] {
        let r = request("A", &format!("{op:?}"), op);
        assert_eq!(
            a.execute(&r, &mock_grant(&r, "nonce", 200), 100),
            Decision::Denied("LOCAL_POLICY")
        );
    }
    assert_eq!(a.adapters.calls, 0);
}
#[test]
fn same_intent_with_mutated_arguments_is_conflict_not_repeat() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("journal.db");
    let mut a = new_agent(&path, "A");
    let r1 = request("A", "one", Operation::DesktopClick);
    assert_eq!(
        a.execute(&r1, &mock_grant(&r1, "n1", 200), 100),
        Decision::LocalCompleted
    );
    let r2 = ActionRequest::new(
        scope("A"),
        fence(),
        "one".into(),
        Epoch(7),
        Operation::DesktopClick,
        r1.target.clone(),
        b"different".to_vec(),
    );
    assert_eq!(
        a.execute(&r2, &mock_grant(&r2, "n2", 200), 100),
        Decision::IntentConflict
    );
    assert_eq!(a.adapters.calls, 1);
}

#[test]
fn same_intent_with_changed_target_cannot_replay_completion() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("journal.db");
    let mut a = new_agent(&path, "A");
    let r1 = request("A", "one", Operation::DesktopClick);
    assert_eq!(
        a.execute(&r1, &mock_grant(&r1, "n1", 200), 100),
        Decision::LocalCompleted
    );
    let mut r2 = request("A", "one", Operation::DesktopClick);
    if let Target::Desktop { window_token, .. } = &mut r2.target {
        *window_token = "new-window".into();
    }
    assert_eq!(
        a.execute(&r2, &mock_grant(&r2, "n2", 200), 100),
        Decision::IntentConflict
    );
    assert_eq!(a.adapters.calls, 1);
}
#[test]
fn stale_and_disconnect_fail_closed_even_if_transport_is_online() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("journal.db");
    let mut a = new_agent(&path, "A");
    let r = request("A", "one", Operation::DesktopClick);
    let g = mock_grant(&r, "n1", 200);
    a.mark_stale();
    assert_eq!(a.execute(&r, &g, 100), Decision::Denied("OFFLINE"));
    a.disconnect();
    assert_eq!(a.execute(&r, &g, 100), Decision::Denied("OFFLINE"));
    let mut next = fence();
    next.connection_generation = Epoch(10);
    a.reconnect_mock(next).unwrap();
    assert_eq!(
        a.execute(&r, &g, 100),
        Decision::Denied("STALE_SCOPE_OR_FENCE")
    );
    assert_eq!(a.adapters.calls, 0);
}
