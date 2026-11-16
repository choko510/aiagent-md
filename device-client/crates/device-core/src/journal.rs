use crate::protocol::Scope;
use crate::security::{
    load_or_create_journal_key, open_payload, restrict_path_owner_only, seal_payload,
};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::time::Duration;

/// STARTED is durable before mock adapter execution. It is UNKNOWN after recovery.
/// Sensitive local_control payloads (`audit_json`) are sealed with HMAC-SHA256 + SHA256-CTR
/// keyed by a DPAPI-protected (Windows) or 0o600 (Unix) keyfile, and journal files enforce owner-only OS ACLs.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EffectState {
    Started,
    CompletedLocal,
    Failed,
    Unknown,
}
impl EffectState {
    fn from_db(s: &str) -> Result<Self, String> {
        match s {
            "STARTED" => Ok(Self::Started),
            "COMPLETED_LOCAL" => Ok(Self::CompletedLocal),
            "FAILED" => Ok(Self::Failed),
            "UNKNOWN" => Ok(Self::Unknown),
            _ => Err("corrupt state".into()),
        }
    }
    fn as_db(self) -> &'static str {
        match self {
            Self::Started => "STARTED",
            Self::CompletedLocal => "COMPLETED_LOCAL",
            Self::Failed => "FAILED",
            Self::Unknown => "UNKNOWN",
        }
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BeginResult {
    StartedNew,
    Replay(EffectState),
    IntentConflict,
    GrantReused,
    PermitReused,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ControlRecord {
    pub stopped: bool,
    pub revoked: bool,
    pub audit: Vec<String>,
}

#[derive(serde::Serialize, serde::Deserialize)]
struct SealedControlPayload {
    v: u8,
    stopped: bool,
    revoked: bool,
    audit: Vec<String>,
}

pub struct LocalJournal {
    conn: Connection,
    scope_hash: String,
    master_key: [u8; 32],
    db_path: PathBuf,
}

fn companion_path(db_path: &Path, suffix: &str) -> PathBuf {
    let mut os: OsString = db_path.as_os_str().to_os_string();
    os.push(suffix);
    PathBuf::from(os)
}

fn restrict_db_companions(db_path: &Path) -> Result<(), String> {
    restrict_path_owner_only(db_path, false)?;
    restrict_path_owner_only(&companion_path(db_path, "-wal"), false)?;
    restrict_path_owner_only(&companion_path(db_path, "-shm"), false)?;
    Ok(())
}

impl LocalJournal {
    /// Local device journal with owner-only ACLs and authenticated at-rest encryption for control audit logs.
    pub fn open(path: &Path, scope: &Scope) -> Result<Self, String> {
        if !scope.valid() {
            return Err("invalid scope".into());
        }
        if path.is_dir() {
            return Err("journal path must not be a directory".into());
        }
        if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
            std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
            restrict_path_owner_only(parent, true)?;
        }
        let master_key = load_or_create_journal_key(path)?;
        let conn = Connection::open(path).map_err(|e| e.to_string())?;
        restrict_path_owner_only(path, false)?;
        conn.busy_timeout(Duration::from_millis(5000))
            .map_err(|e| e.to_string())?;
        conn.pragma_update(None, "journal_mode", "WAL")
            .map_err(|e| e.to_string())?;
        conn.pragma_update(None, "synchronous", "FULL")
            .map_err(|e| e.to_string())?;
        restrict_db_companions(path)?;
        conn.execute_batch(
            "
            CREATE TABLE IF NOT EXISTS effects (
                scope_hash TEXT NOT NULL,
                intent_id TEXT NOT NULL,
                args_hash TEXT NOT NULL,
                attempt_fence TEXT NOT NULL,
                permit_nonce TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('STARTED','COMPLETED_LOCAL','FAILED','UNKNOWN')),
                PRIMARY KEY (scope_hash, intent_id)
            );
            CREATE TABLE IF NOT EXISTS used_grants (
                scope_hash TEXT NOT NULL,
                grant_nonce TEXT NOT NULL,
                intent_id TEXT NOT NULL,
                PRIMARY KEY (scope_hash, grant_nonce)
            );
            CREATE TABLE IF NOT EXISTS used_permits (
                scope_hash TEXT NOT NULL,
                permit_nonce TEXT NOT NULL,
                intent_id TEXT NOT NULL,
                PRIMARY KEY (scope_hash, permit_nonce)
            );
            CREATE TABLE IF NOT EXISTS local_control (
                scope_hash TEXT PRIMARY KEY,
                stopped INTEGER NOT NULL CHECK(stopped IN (0, 1)),
                revoked INTEGER NOT NULL CHECK(revoked IN (0, 1)),
                audit_json TEXT NOT NULL
            );
        ",
        )
        .map_err(|e| e.to_string())?;
        let scope_hash = scope.namespace();
        // A process that vanished after STARTED might have delivered an effect.
        conn.execute(
            "UPDATE effects SET state='UNKNOWN' WHERE scope_hash=?1 AND state='STARTED'",
            params![scope_hash],
        )
        .map_err(|e| e.to_string())?;
        Ok(Self {
            conn,
            scope_hash,
            master_key,
            db_path: path.to_path_buf(),
        })
    }

    pub const OWNER_RECOVERY_PHRASE: &'static str = "CONFIRM_OWNER_RESUME";
    pub const LEGACY_MIGRATED_MARKER: &'static str = "LEGACY_V1_JOURNAL_MIGRATED_FAILSAFE_STOP";

    pub fn load_control(&self, default_audit: Vec<String>) -> Result<ControlRecord, String> {
        let row: Option<(i64, i64, String)> = self
            .conn
            .query_row(
                "SELECT stopped, revoked, audit_json FROM local_control WHERE scope_hash=?1",
                params![self.scope_hash],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()
            .map_err(|e| e.to_string())?;
        match row {
            Some((stopped, revoked, sealed_audit)) => {
                let raw_json = open_payload(
                    &self.master_key,
                    &self.scope_hash,
                    "local_control",
                    &sealed_audit,
                )?;
                if let Ok(envelope) = serde_json::from_slice::<SealedControlPayload>(&raw_json) {
                    if envelope.v != 2
                        || envelope.stopped != (stopped != 0)
                        || envelope.revoked != (revoked != 0)
                    {
                        return Err("tampered local_control flags or unsupported version".into());
                    }
                    return Ok(ControlRecord {
                        stopped: envelope.stopped,
                        revoked: envelope.revoked,
                        audit: envelope.audit,
                    });
                }
                // Legacy v1 rows sealed only `Vec<String>` audit entries; plaintext stopped/revoked columns
                // were not HMAC-covered. Migrate without bricking the client, NEVER trusting plaintext stopped=0.
                let mut legacy_audit: Vec<String> =
                    serde_json::from_slice(&raw_json).map_err(|e| e.to_string())?;
                let revoked_in_audit = legacy_audit
                    .iter()
                    .any(|entry| entry.contains("REVOKE") || entry.contains("revoke"));
                let effective_revoked = (revoked != 0) || revoked_in_audit;
                if !legacy_audit
                    .iter()
                    .any(|e| e == Self::LEGACY_MIGRATED_MARKER)
                {
                    legacy_audit.push(Self::LEGACY_MIGRATED_MARKER.into());
                }
                Ok(ControlRecord {
                    stopped: true,
                    revoked: effective_revoked,
                    audit: legacy_audit,
                })
            }
            None => Ok(ControlRecord {
                stopped: false,
                revoked: false,
                audit: default_audit,
            }),
        }
    }

    pub fn owner_confirm_clear_legacy_stop(
        &mut self,
        confirmation_phrase: &str,
    ) -> Result<ControlRecord, String> {
        if confirmation_phrase != Self::OWNER_RECOVERY_PHRASE {
            return Err("OWNER_CONFIRMATION_REQUIRED".into());
        }
        let mut rec = self.load_control(vec![])?;
        if rec.revoked {
            return Err("CANNOT_RECOVER_REVOKED_DEVICE".into());
        }
        if !rec.audit.iter().any(|e| e == Self::LEGACY_MIGRATED_MARKER) {
            return Err("NOT_A_LEGACY_MIGRATED_STOP".into());
        }
        rec.stopped = false;
        rec.audit.push("OWNER_CONFIRMED_LEGACY_STOP_RESET".into());
        self.save_control(&rec)?;
        Ok(rec)
    }

    pub fn save_control(&mut self, record: &ControlRecord) -> Result<(), String> {
        let envelope = SealedControlPayload {
            v: 2,
            stopped: record.stopped,
            revoked: record.revoked,
            audit: record.audit.clone(),
        };
        let audit_json = serde_json::to_string(&envelope).map_err(|e| e.to_string())?;
        let sealed_audit = seal_payload(
            &self.master_key,
            &self.scope_hash,
            "local_control",
            audit_json.as_bytes(),
        )?;
        self.conn
            .execute(
                "INSERT INTO local_control(scope_hash, stopped, revoked, audit_json)
                 VALUES(?1, ?2, ?3, ?4)
                 ON CONFLICT(scope_hash) DO UPDATE SET
                   stopped=excluded.stopped,
                   revoked=excluded.revoked,
                   audit_json=excluded.audit_json",
                params![
                    self.scope_hash,
                    i64::from(record.stopped),
                    i64::from(record.revoked),
                    sealed_audit
                ],
            )
            .map_err(|e| e.to_string())?;
        restrict_db_companions(&self.db_path)?;
        Ok(())
    }
    pub fn state(&self, intent_id: &str) -> Result<Option<EffectState>, String> {
        let raw: Option<String> = self
            .conn
            .query_row(
                "SELECT state FROM effects WHERE scope_hash=?1 AND intent_id=?2",
                params![self.scope_hash, intent_id],
                |r| r.get(0),
            )
            .optional()
            .map_err(|e| e.to_string())?;
        raw.map(|s| EffectState::from_db(&s)).transpose()
    }
    pub fn precheck(
        &self,
        intent_id: &str,
        args_hash: &str,
        attempt_fence: &str,
        grant_nonce: &str,
    ) -> Result<Option<BeginResult>, String> {
        let previous: Option<(String, String, String)> = self
            .conn
            .query_row(
                "SELECT args_hash, attempt_fence, state FROM effects WHERE scope_hash=?1 AND intent_id=?2",
                params![self.scope_hash, intent_id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()
            .map_err(|e| e.to_string())?;
        if let Some((hash, fence, state)) = previous {
            if hash != args_hash || fence != attempt_fence {
                return Ok(Some(BeginResult::IntentConflict));
            }
            return Ok(Some(BeginResult::Replay(EffectState::from_db(&state)?)));
        }
        let grant_used: bool = self
            .conn
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM used_grants WHERE scope_hash=?1 AND grant_nonce=?2)",
                params![self.scope_hash, grant_nonce],
                |r| r.get(0),
            )
            .map_err(|e| e.to_string())?;
        if grant_used {
            return Ok(Some(BeginResult::GrantReused));
        }
        Ok(None)
    }
    pub fn begin(
        &mut self,
        intent_id: &str,
        args_hash: &str,
        attempt_fence: &str,
        grant_nonce: &str,
        permit_nonce: &str,
    ) -> Result<BeginResult, String> {
        let tx = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|e| e.to_string())?;
        let previous: Option<(String, String, String)> = tx
            .query_row(
                "SELECT args_hash, attempt_fence, state FROM effects WHERE scope_hash=?1 AND intent_id=?2",
                params![self.scope_hash, intent_id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()
            .map_err(|e| e.to_string())?;
        if let Some((hash, fence, state)) = previous {
            if hash != args_hash || fence != attempt_fence {
                return Ok(BeginResult::IntentConflict);
            }
            return Ok(BeginResult::Replay(EffectState::from_db(&state)?));
        }
        let grant_used: bool = tx
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM used_grants WHERE scope_hash=?1 AND grant_nonce=?2)",
                params![self.scope_hash, grant_nonce],
                |r| r.get(0),
            )
            .map_err(|e| e.to_string())?;
        if grant_used {
            return Ok(BeginResult::GrantReused);
        }
        let permit_used: bool = tx
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM used_permits WHERE scope_hash=?1 AND permit_nonce=?2)",
                params![self.scope_hash, permit_nonce],
                |r| r.get(0),
            )
            .map_err(|e| e.to_string())?;
        if permit_used {
            return Ok(BeginResult::PermitReused);
        }
        tx.execute(
            "INSERT INTO used_grants(scope_hash,grant_nonce,intent_id) VALUES(?1,?2,?3)",
            params![self.scope_hash, grant_nonce, intent_id],
        )
        .map_err(|e| e.to_string())?;
        tx.execute(
            "INSERT INTO used_permits(scope_hash,permit_nonce,intent_id) VALUES(?1,?2,?3)",
            params![self.scope_hash, permit_nonce, intent_id],
        )
        .map_err(|e| e.to_string())?;
        tx.execute(
            "INSERT INTO effects(scope_hash,intent_id,args_hash,attempt_fence,permit_nonce,state)
                    VALUES(?1,?2,?3,?4,?5,'STARTED')",
            params![
                self.scope_hash,
                intent_id,
                args_hash,
                attempt_fence,
                permit_nonce
            ],
        )
        .map_err(|e| e.to_string())?;
        tx.commit().map_err(|e| e.to_string())?;
        Ok(BeginResult::StartedNew)
    }
    pub fn finish(&mut self, intent_id: &str, outcome: EffectState) -> Result<(), String> {
        if outcome == EffectState::Started {
            return Err("cannot reenter STARTED".into());
        }
        let n = self.conn.execute(
            "UPDATE effects SET state=?3 WHERE scope_hash=?1 AND intent_id=?2 AND state='STARTED'",
            params![self.scope_hash, intent_id, outcome.as_db()],
        ).map_err(|e| e.to_string())?;
        if n != 1 {
            return Err("not an active STARTED effect".into());
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tampering_plaintext_stopped_or_revoked_column_fails_closed() {
        let base =
            std::env::temp_dir().join(format!("aiagent_journal_tamper_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();
        let db_path = base.join("journal.db");
        let scope = Scope {
            os_user: "u-tamper".into(),
            tenant_id: "t-tamper".into(),
            owner_user_id: "o-tamper".into(),
            device_identity_id: "di-tamper".into(),
            device_id: "d-tamper".into(),
        };
        {
            let mut j = LocalJournal::open(&db_path, &scope).unwrap();
            j.save_control(&ControlRecord {
                stopped: true,
                revoked: true,
                audit: vec!["STOP".into()],
            })
            .unwrap();
        }
        // Directly flip plaintext INTEGER column in SQLite without re-sealing HMAC envelope
        {
            let conn = Connection::open(&db_path).unwrap();
            conn.execute("UPDATE local_control SET stopped=0, revoked=0", [])
                .unwrap();
        }
        let reopened = LocalJournal::open(&db_path, &scope).unwrap();
        let err = reopened.load_control(vec![]).unwrap_err();
        assert!(err.contains("tampered"), "unexpected err: {err}");
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn legacy_v1_hmac_vec_payload_migrates_to_failsafe_stop_never_trusting_plaintext_zero() {
        let base =
            std::env::temp_dir().join(format!("aiagent_journal_legacy_v1_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();
        let db_path = base.join("journal.db");
        let scope = Scope {
            os_user: "u-v1".into(),
            tenant_id: "t-v1".into(),
            owner_user_id: "o-v1".into(),
            device_identity_id: "di-v1".into(),
            device_id: "d-v1".into(),
        };
        let legacy_json =
            serde_json::to_string(&vec!["LOCAL_STOP: legacy entry".to_string()]).unwrap();
        let (scope_hash, legacy_sealed) = {
            let j = LocalJournal::open(&db_path, &scope).unwrap();
            let sealed = seal_payload(
                &j.master_key,
                &j.scope_hash,
                "local_control",
                legacy_json.as_bytes(),
            )
            .unwrap();
            (j.scope_hash.clone(), sealed)
        };
        {
            let conn = Connection::open(&db_path).unwrap();
            // Even when plaintext stopped=0, revoked=0 on a legacy row, we must NEVER auto-resume!
            conn.execute(
                "INSERT INTO local_control(scope_hash, stopped, revoked, audit_json) VALUES(?1, 0, 0, ?2)",
                params![scope_hash, legacy_sealed],
            )
            .unwrap();
        }
        let mut reopened = LocalJournal::open(&db_path, &scope).unwrap();
        let migrated = reopened.load_control(vec![]).unwrap();
        assert!(
            migrated.stopped,
            "legacy migration must remain stopped until owner confirms"
        );
        assert!(!migrated.revoked);
        assert!(migrated
            .audit
            .iter()
            .any(|e| e == LocalJournal::LEGACY_MIGRATED_MARKER));

        // Wrong owner confirmation phrase fails closed
        assert_eq!(
            reopened
                .owner_confirm_clear_legacy_stop("WRONG")
                .unwrap_err(),
            "OWNER_CONFIRMATION_REQUIRED"
        );
        // Explicit owner confirmation clears legacy stop and seals into v2
        let recovered = reopened
            .owner_confirm_clear_legacy_stop(LocalJournal::OWNER_RECOVERY_PHRASE)
            .unwrap();
        assert!(!recovered.stopped);
        assert!(!recovered.revoked);
        let reloaded = reopened.load_control(vec![]).unwrap();
        assert!(!reloaded.stopped);
        assert!(reloaded
            .audit
            .iter()
            .any(|e| e == "OWNER_CONFIRMED_LEGACY_STOP_RESET"));
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn legacy_v1_revoked_audit_cannot_be_unrevoked_even_if_plaintext_column_zero() {
        let base = std::env::temp_dir().join(format!(
            "aiagent_journal_legacy_v1_revoked_{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();
        let db_path = base.join("journal.db");
        let scope = Scope {
            os_user: "u-v1r".into(),
            tenant_id: "t-v1r".into(),
            owner_user_id: "o-v1r".into(),
            device_identity_id: "di-v1r".into(),
            device_id: "d-v1r".into(),
        };
        let legacy_json =
            serde_json::to_string(&vec!["LOCAL_REVOKE: device revoked".to_string()]).unwrap();
        let (scope_hash, legacy_sealed) = {
            let j = LocalJournal::open(&db_path, &scope).unwrap();
            let sealed = seal_payload(
                &j.master_key,
                &j.scope_hash,
                "local_control",
                legacy_json.as_bytes(),
            )
            .unwrap();
            (j.scope_hash.clone(), sealed)
        };
        {
            let conn = Connection::open(&db_path).unwrap();
            // Even if SQLite plaintext columns were tampered to 0/0, authenticated REVOKE entry enforces revoked=true!
            conn.execute(
                "INSERT INTO local_control(scope_hash, stopped, revoked, audit_json) VALUES(?1, 0, 0, ?2)",
                params![scope_hash, legacy_sealed],
            )
            .unwrap();
        }
        let mut reopened = LocalJournal::open(&db_path, &scope).unwrap();
        let migrated = reopened.load_control(vec![]).unwrap();
        assert!(migrated.stopped);
        assert!(migrated.revoked);
        assert_eq!(
            reopened
                .owner_confirm_clear_legacy_stop(LocalJournal::OWNER_RECOVERY_PHRASE)
                .unwrap_err(),
            "CANNOT_RECOVER_REVOKED_DEVICE"
        );
        let _ = std::fs::remove_dir_all(&base);
    }
}
