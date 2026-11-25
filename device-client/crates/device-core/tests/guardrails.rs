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
                .begin(
                    &r.tool_intent_id,
                    &r.journal_fingerprint(),
                    "7",
                    "grant-1",
                    "permit",
                )
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

#[test]
fn reconnect_rejects_changed_authority_fences() {
    let dir = tempdir().unwrap();
    let mut agent = new_agent(&dir.path().join("journal.db"), "A");
    agent.disconnect();
    for field in 0..5 {
        let mut next = fence();
        next.connection_generation = Epoch(10);
        match field {
            0 => next.device_claim_epoch = Epoch(3),
            1 => next.lease_epoch = Epoch(2),
            2 => next.restore_incarnation = Epoch(1),
            3 => next.goal_revision = Epoch(0),
            _ => next.policy_revision = Epoch(1),
        }
        assert_eq!(agent.reconnect_mock(next), Err("RECONNECT_DENIED"));
    }
    assert_eq!(agent.adapters.calls, 0);
}

#[test]
fn grant_nonce_replay_across_intents_is_rejected_without_adapter_call() {
    let dir = tempdir().unwrap();
    let mut agent = new_agent(&dir.path().join("journal.db"), "A");
    let r1 = request("A", "one", Operation::DesktopClick);
    let r2 = request("A", "two", Operation::DesktopClick);
    assert_eq!(
        agent.execute(&r1, &mock_grant(&r1, "shared-grant-nonce", 200), 100),
        Decision::LocalCompleted
    );
    assert_eq!(
        agent.execute(&r2, &mock_grant(&r2, "shared-grant-nonce", 200), 100),
        Decision::GrantReused
    );
    assert_eq!(agent.adapters.calls, 1);
    assert_eq!(agent.channel.authorize_calls, 1);
}

#[test]
fn failed_effect_is_distinct_from_unknown_and_not_retried() {
    let dir = tempdir().unwrap();
    let mut agent = new_agent(&dir.path().join("journal.db"), "A");
    agent.adapters.deliver_failed = true;
    let r = request("A", "failed-once", Operation::DesktopClick);
    let g = mock_grant(&r, "nonce-fail", 200);
    assert_eq!(agent.execute(&r, &g, 100), Decision::Failed);
    assert_eq!(
        agent.effect("failed-once").unwrap(),
        Some(EffectState::Failed)
    );
    // Replaying an already recorded FAILED effect stops at journal precheck without calling cloud or adapter again.
    assert_eq!(agent.execute(&r, &g, 100), Decision::Failed);
    assert_eq!(agent.adapters.calls, 1);
    assert_eq!(agent.channel.authorize_calls, 1);
}

#[test]
fn target_and_policy_validation_reject_whitespace_and_non_origin_urls() {
    let dir = tempdir().unwrap();
    let mut agent = new_agent(&dir.path().join("journal.db"), "A");
    assert!(agent.policy.allow_desktop(" ").is_err());
    assert!(agent
        .policy
        .allow_browser_origin("https://example.com/path")
        .is_err());
    assert!(agent
        .policy
        .allow_browser_origin("https://user@example.com")
        .is_err());
    agent
        .policy
        .allow_browser_origin("https://example.com")
        .unwrap();

    let mut bad_desktop = request("A", "ws-desktop", Operation::DesktopClick);
    if let Target::Desktop { window_token, .. } = &mut bad_desktop.target {
        *window_token = " window-2 ".into();
    }
    assert_eq!(
        agent.execute(&bad_desktop, &mock_grant(&bad_desktop, "n1", 200), 100),
        Decision::Denied("INVALID_GRANT_OR_ARGUMENTS")
    );

    let mut bad_browser = request("A", "bad-origin", Operation::BrowserClick);
    if let Target::Browser { origin, .. } = &mut bad_browser.target {
        *origin = "https://example.com/admin".into();
    }
    assert_eq!(
        agent.execute(&bad_browser, &mock_grant(&bad_browser, "n2", 200), 100),
        Decision::Denied("INVALID_GRANT_OR_ARGUMENTS")
    );
    assert_eq!(agent.adapters.calls, 0);
}

#[test]
fn unpaired_agent_cannot_execute_or_reconnect() {
    let dir = tempdir().unwrap();
    let mut agent = new_agent(&dir.path().join("journal.db"), "A");
    agent.unpair();
    assert_eq!(
        agent.phase(),
        aiagent_device_core::ConnectionPhase::Unpaired
    );
    let r = request("A", "unpaired", Operation::DesktopClick);
    assert_eq!(
        agent.execute(&r, &mock_grant(&r, "n1", 200), 100),
        Decision::Denied("LOCAL_STOP_OR_REVOKED")
    );
    let mut next = fence();
    next.connection_generation = Epoch(10);
    assert_eq!(agent.reconnect_mock(next), Err("RECONNECT_DENIED"));
    assert_eq!(agent.adapters.calls, 0);
}

#[test]
fn journal_control_payload_is_encrypted_tamper_checked_and_acl_restricted() {
    use aiagent_device_core::security::{
        key_file_path, load_or_create_journal_key, open_payload, seal_payload,
        verify_owner_only_acl,
    };
    use aiagent_device_core::ControlRecord;

    let dir = tempdir().unwrap();
    let db_dir = dir.path().join("restricted");
    let path = db_dir.join("journal.db");
    let marker = "SENSITIVE_AUDIT_MARKER_XYZ_987";

    {
        let mut journal = LocalJournal::open(&path, &scope("A")).unwrap();
        journal
            .save_control(&ControlRecord {
                stopped: true,
                revoked: false,
                audit: vec![marker.into()],
            })
            .unwrap();
    }

    assert!(verify_owner_only_acl(&db_dir).unwrap());
    assert!(verify_owner_only_acl(&path).unwrap());
    assert!(verify_owner_only_acl(&key_file_path(&path)).unwrap());

    for candidate in [
        path.clone(),
        dir.path().join("restricted/journal.db-wal"),
        key_file_path(&path),
    ] {
        if candidate.exists() {
            let raw = std::fs::read(&candidate).unwrap();
            assert!(
                !raw.windows(marker.len()).any(|w| w == marker.as_bytes()),
                "plaintext marker leaked in {}",
                candidate.display()
            );
        }
    }

    let reopened = LocalJournal::open(&path, &scope("A"))
        .unwrap()
        .load_control(vec![])
        .unwrap();
    assert!(reopened.stopped);
    assert_eq!(reopened.audit, vec![marker.to_string()]);

    let key = load_or_create_journal_key(&path).unwrap();
    let sealed = seal_payload(&key, &scope("A").namespace(), "local_control", b"[\"ok\"]").unwrap();
    assert!(sealed.starts_with("enc:v2:"));
    assert!(open_payload(&key, &scope("B").namespace(), "local_control", &sealed).is_err());
    assert!(open_payload(&key, &scope("A").namespace(), "different_label", &sealed).is_err());
    let mut tampered = sealed.clone();
    let last = tampered.pop().unwrap();
    tampered.push(if last == '0' { '1' } else { '0' });
    assert!(open_payload(&key, &scope("A").namespace(), "local_control", &tampered).is_err());
    assert!(open_payload(
        &key,
        &scope("A").namespace(),
        "local_control",
        "[\"plain\"]"
    )
    .is_err());
}

#[test]
fn missing_key_for_existing_journal_does_not_generate_a_replacement() {
    use aiagent_device_core::security::key_file_path;
    use aiagent_device_core::ControlRecord;
    let dir = tempdir().unwrap();
    let path = dir.path().join("journal.db");
    let mut journal = LocalJournal::open(&path, &scope("A")).unwrap();
    journal
        .save_control(&ControlRecord {
            stopped: true,
            revoked: true,
            audit: vec![],
        })
        .unwrap();
    drop(journal);
    let key_path = key_file_path(&path);
    std::fs::remove_file(&key_path).unwrap();
    assert!(LocalJournal::open(&path, &scope("A")).is_err());
    assert!(!key_path.exists());
}

#[test]
fn missing_database_for_existing_key_does_not_reset_control() {
    use aiagent_device_core::{security::key_file_path, ControlRecord};
    let dir = tempdir().unwrap();
    let path = dir.path().join("journal.db");
    let mut journal = LocalJournal::open(&path, &scope("A")).unwrap();
    journal
        .save_control(&ControlRecord {
            stopped: true,
            revoked: true,
            audit: vec![],
        })
        .unwrap();
    drop(journal);
    std::fs::remove_file(&path).unwrap();
    assert!(key_file_path(&path).exists());
    assert!(LocalJournal::open(&path, &scope("A")).is_err());
    assert!(!path.exists());
}

#[test]
fn mtls_pairing_and_local_adapters_fail_closed_without_verified_hosts() {
    use aiagent_device_core::agent::{
        AndroidAdapterPort, AuthenticatedChannelPort, BuildAdapterPort, ChromeAdapterPort,
        CuaAdapterPort, Delivery,
    };
    use aiagent_device_core::mock::mock_grant;
    use aiagent_device_core::{
        make_start_permit, CloudPairingConfig, LocalAdapters, TlsAuthenticatedChannel,
    };

    let cfg = CloudPairingConfig {
        endpoint: "wss://control.aiagent.example/ws".into(),
        ca_pin_sha256: "b".repeat(64),
        client_cert_present: true,
        tls_handshake_verified: true,
    };
    let mut invalid = cfg.clone();
    invalid.endpoint = "http://insecure.local/ws".into();
    assert!(invalid.validate().is_err());
    invalid = cfg.clone();
    invalid.client_cert_present = false;
    assert!(invalid.validate().is_err());
    let mut ch = TlsAuthenticatedChannel::unpaired();
    assert_eq!(
        ch.pair(cfg.clone()),
        Err("AUTHENTICATED_TRANSPORT_UNAVAILABLE")
    );
    // Even direct field assignment and a fabricated permit cannot create authority.
    ch.config = Some(cfg);
    let req = request("A", "d1", Operation::DesktopClick);
    let grant = mock_grant(&req, "g1", 200);
    ch.active_permit = Some(make_start_permit(&req, &grant, "p1", 200));
    assert!(!ch.online());
    assert!(ch.authorize_start(&req, &grant, 100).is_none());

    let dir = tempdir().unwrap();
    std::fs::create_dir(dir.path().join("demo-ws")).unwrap();
    let mut adapters = LocalAdapters {
        build_root: Some(dir.path().to_path_buf()),
        adb_probe_enabled: true,
        chrome_native_host_connected: true,
        desktop_accessibility_ready: true,
        executed_count: 0,
    };
    let build_req = ActionRequest::new(
        scope("A"),
        fence(),
        "b1".into(),
        Epoch(7),
        Operation::BuildRun,
        Target::Build {
            workspace_id: "demo-ws".into(),
        },
        b"cargo test".to_vec(),
    );
    let android_req = ActionRequest::new(
        scope("A"),
        fence(),
        "a1".into(),
        Epoch(7),
        Operation::AndroidInstall,
        Target::Android {
            serial: "device-1".into(),
            package_id: "example.app".into(),
        },
        vec![],
    );
    let browser_req = ActionRequest::new(
        scope("A"),
        fence(),
        "c1".into(),
        Epoch(7),
        Operation::BrowserClick,
        Target::Browser {
            origin: "https://example.com".into(),
            profile_id: "profile-1".into(),
            tab_id: "tab-1".into(),
            document_generation: "doc-1".into(),
        },
        vec![],
    );
    assert_eq!(adapters.desktop(&req), Delivery::Failed);
    assert_eq!(adapters.chrome(&browser_req), Delivery::Failed);
    assert_eq!(adapters.build(&build_req), Delivery::Failed);
    assert_eq!(adapters.android(&android_req), Delivery::Failed);
    assert_eq!(adapters.executed_count, 0);
}
