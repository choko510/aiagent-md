use crate::protocol::Scope;
use crate::security::{
    is_legacy_payload, load_or_create_journal_key, open_payload, restrict_path_owner_only,
    seal_payload,
};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::time::Duration;
use zeroize::Zeroize;

/// STARTED is durable before mock adapter execution. It is UNKNOWN after recovery.
/// The control envelope uses versioned AEAD. Legacy ciphertext is read only for migration.
/// This simulator is not a production key store or an authenticated whole-database journal.
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
    started_here: HashSet<String>,
}

impl Drop for LocalJournal {
    fn drop(&mut self) {
        self.master_key.zeroize();
    }
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

// This detects partial edits and deletion while the authenticated guard survives.
// It cannot detect a complete rollback or deletion of guard and all history.
fn effect_digest(conn: &Connection, scope: &str) -> Result<([u8; 32], usize), String> {
    let mut hash = Sha256::new();
    hash.update(b"aiagent-device-effect-set-v1");
    let mut count = 0usize;
    for (tag, sql, columns) in [
        (b'E', "SELECT intent_id,args_hash,attempt_fence,permit_nonce,state FROM effects WHERE scope_hash=?1 ORDER BY intent_id", 5),
        (b'G', "SELECT grant_nonce,intent_id FROM used_grants WHERE scope_hash=?1 ORDER BY grant_nonce", 2),
        (b'P', "SELECT permit_nonce,intent_id FROM used_permits WHERE scope_hash=?1 ORDER BY permit_nonce", 2),
    ] {
        let mut stmt = conn.prepare(sql).map_err(|e| e.to_string())?;
        let mut rows = stmt.query(params![scope]).map_err(|e| e.to_string())?;
        while let Some(row) = rows.next().map_err(|e| e.to_string())? {
            count += 1;
            if count > 300_000 { return Err("EFFECT_HISTORY_LIMIT".into()); }
            hash.update([tag]);
            for index in 0..columns {
                let value: String = row.get(index).map_err(|e| e.to_string())?;
                if value.len() > 4096 { return Err("INVALID_EFFECT_HISTORY".into()); }
                hash.update((value.len() as u64).to_be_bytes());
                hash.update(value.as_bytes());
            }
        }
    }
    Ok((hash.finalize().into(), count))
}

fn verify_effect_integrity(conn: &Connection, key: &[u8; 32], scope: &str) -> Result<(), String> {
    let sealed: String = conn
        .query_row(
            "SELECT sealed_digest FROM effect_integrity WHERE scope_hash=?1",
            params![scope],
            |r| r.get(0),
        )
        .map_err(|_| "MISSING_EFFECT_INTEGRITY")?;
    let authenticated = open_payload(key, scope, "effect_integrity", &sealed)?;
    if authenticated.as_slice() != effect_digest(conn, scope)?.0 {
        return Err("EFFECT_INTEGRITY_MISMATCH".into());
    }
    Ok(())
}

fn update_effect_integrity(conn: &Connection, key: &[u8; 32], scope: &str) -> Result<(), String> {
    let sealed = seal_payload(
        key,
        scope,
        "effect_integrity",
        &effect_digest(conn, scope)?.0,
    )?;
    if conn
        .execute(
            "UPDATE effect_integrity SET sealed_digest=?1 WHERE scope_hash=?2",
            params![sealed, scope],
        )
        .map_err(|e| e.to_string())?
        != 1
    {
        return Err("MISSING_EFFECT_INTEGRITY".into());
    }
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
        let mut conn = Connection::open(path).map_err(|e| e.to_string())?;
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
            CREATE TABLE IF NOT EXISTS local_control_initialized (
                scope_hash TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS effect_integrity (
                scope_hash TEXT PRIMARY KEY,
                sealed_digest TEXT NOT NULL
            );
            INSERT OR IGNORE INTO local_control_initialized(scope_hash)
                SELECT scope_hash FROM local_control;
        ",
        )
        .map_err(|e| e.to_string())?;
        let scope_hash = scope.namespace();
        // Existing unsealed history cannot be silently promoted to trusted.
        let tx = conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|e| e.to_string())?;
        let history = effect_digest(&tx, &scope_hash)?;
        let existing: Option<String> = tx
            .query_row(
                "SELECT sealed_digest FROM effect_integrity WHERE scope_hash=?1",
                params![scope_hash],
                |r| r.get(0),
            )
            .optional()
            .map_err(|e| e.to_string())?;
        let previously_initialized: bool = tx
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM local_control_initialized WHERE scope_hash=?1)",
                params![scope_hash],
                |r| r.get(0),
            )
            .map_err(|e| e.to_string())?;
        if existing.is_none() && history.1 == 0 && !previously_initialized {
            let sealed = seal_payload(&master_key, &scope_hash, "effect_integrity", &history.0)?;
            tx.execute(
                "INSERT OR IGNORE INTO effect_integrity(scope_hash,sealed_digest) VALUES(?1,?2)",
                params![scope_hash, sealed],
            )
            .map_err(|e| e.to_string())?;
        }
        verify_effect_integrity(&tx, &master_key, &scope_hash)?;
        tx.commit().map_err(|e| e.to_string())?;
        Ok(Self {
            conn,
            scope_hash,
            master_key,
            db_path: path.to_path_buf(),
            started_here: HashSet::new(),
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
                    let record = ControlRecord {
                        stopped: envelope.stopped,
                        revoked: envelope.revoked,
                        audit: envelope.audit,
                    };
                    self.migrate_control_if_legacy(stopped, revoked, &sealed_audit, &record)?;
                    return Ok(record);
                }
                if !is_legacy_payload(&sealed_audit) {
                    return Err("invalid control envelope".into());
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
                let record = ControlRecord {
                    stopped: true,
                    revoked: effective_revoked,
                    audit: legacy_audit,
                };
                self.migrate_control_if_legacy(stopped, revoked, &sealed_audit, &record)?;
                Ok(record)
            }
            None => {
                let initialized: bool = self.conn.query_row("SELECT EXISTS(SELECT 1 FROM local_control_initialized WHERE scope_hash=?1)", params![self.scope_hash], |r| r.get(0)).map_err(|e| e.to_string())?;
                if initialized {
                    return Err("MISSING_LOCAL_CONTROL".into());
                }
                Ok(ControlRecord {
                    stopped: false,
                    revoked: false,
                    audit: default_audit,
                })
            }
        }
    }

    // The conditional update commits both flags and the authenticated payload atomically.
    // Concurrent changes must be re-read, never overwritten.
    fn migrate_control_if_legacy(
        &self,
        stopped: i64,
        revoked: i64,
        original: &str,
        record: &ControlRecord,
    ) -> Result<(), String> {
        if !is_legacy_payload(original) {
            return Ok(());
        }
        let envelope = SealedControlPayload {
            v: 2,
            stopped: record.stopped,
            revoked: record.revoked,
            audit: record.audit.clone(),
        };
        let json = serde_json::to_vec(&envelope).map_err(|e| e.to_string())?;
        let sealed = seal_payload(&self.master_key, &self.scope_hash, "local_control", &json)?;
        let changed = self.conn.execute(
            "UPDATE local_control SET stopped=?1, revoked=?2, audit_json=?3 WHERE scope_hash=?4 AND stopped=?5 AND revoked=?6 AND audit_json=?7",
            params![i64::from(record.stopped), i64::from(record.revoked), sealed,
                    self.scope_hash, stopped, revoked, original],
        ).map_err(|e| e.to_string())?;
        if changed != 1 {
            return Err("journal migration concurrent modification".into());
        }
        restrict_db_companions(&self.db_path)?;
        Ok(())
    }

    #[cfg(any(test, feature = "simulator-recovery"))]
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
        let legacy = rec
            .audit
            .iter()
            .rposition(|e| e == Self::LEGACY_MIGRATED_MARKER);
        let recovered = rec
            .audit
            .iter()
            .rposition(|e| e == "OWNER_CONFIRMED_LEGACY_STOP_RESET");
        if legacy.is_none() || recovered.is_some_and(|position| Some(position) > legacy) {
            return Err("NOT_A_LEGACY_MIGRATED_STOP".into());
        }
        // Bind the explicit recovery to the exact authenticated row just read.
        let (stopped, revoked, original): (i64, i64, String) = self
            .conn
            .query_row(
                "SELECT stopped, revoked, audit_json FROM local_control WHERE scope_hash=?1",
                params![self.scope_hash],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .map_err(|e| e.to_string())?;
        let raw = open_payload(
            &self.master_key,
            &self.scope_hash,
            "local_control",
            &original,
        )?;
        let current: SealedControlPayload =
            serde_json::from_slice(&raw).map_err(|e| e.to_string())?;
        if current.v != 2
            || stopped != 1
            || revoked != 0
            || !current.stopped
            || current.revoked
            || current.audit != rec.audit
        {
            return Err("journal recovery concurrent modification".into());
        }
        rec.stopped = false;
        rec.audit.push("OWNER_CONFIRMED_LEGACY_STOP_RESET".into());
        let envelope = SealedControlPayload {
            v: 2,
            stopped: false,
            revoked: false,
            audit: rec.audit.clone(),
        };
        let json = serde_json::to_vec(&envelope).map_err(|e| e.to_string())?;
        let sealed = seal_payload(&self.master_key, &self.scope_hash, "local_control", &json)?;
        let changed = self.conn.execute(
            "UPDATE local_control SET stopped=0, audit_json=?1 WHERE scope_hash=?2 AND stopped=1 AND revoked=0 AND audit_json=?3",
            params![sealed, self.scope_hash, original],
        ).map_err(|e| e.to_string())?;
        if changed != 1 {
            return Err("journal recovery concurrent modification".into());
        }
        restrict_db_companions(&self.db_path)?;
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
        let tx = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|e| e.to_string())?;
        let initialized: bool = tx
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM local_control_initialized WHERE scope_hash=?1)",
                params![self.scope_hash],
                |r| r.get(0),
            )
            .map_err(|e| e.to_string())?;
        let exists: bool = tx
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM local_control WHERE scope_hash=?1)",
                params![self.scope_hash],
                |r| r.get(0),
            )
            .map_err(|e| e.to_string())?;
        if initialized && !exists {
            return Err("MISSING_LOCAL_CONTROL".into());
        }
        let changed = tx
            .execute(
                "INSERT INTO local_control(scope_hash, stopped, revoked, audit_json)
                 VALUES(?1, ?2, ?3, ?4)
                 ON CONFLICT(scope_hash) DO UPDATE SET
                   stopped=excluded.stopped,
                   revoked=excluded.revoked,
                   audit_json=excluded.audit_json
                 WHERE local_control.stopped <= excluded.stopped
                   AND local_control.revoked <= excluded.revoked",
                params![
                    self.scope_hash,
                    i64::from(record.stopped),
                    i64::from(record.revoked),
                    sealed_audit
                ],
            )
            .map_err(|e| e.to_string())?;
        if changed != 1 {
            return Err("control stop or revoke cannot be cleared by save".into());
        }
        tx.execute(
            "INSERT OR IGNORE INTO local_control_initialized(scope_hash) VALUES(?1)",
            params![self.scope_hash],
        )
        .map_err(|e| e.to_string())?;
        tx.commit().map_err(|e| e.to_string())?;
        restrict_db_companions(&self.db_path)?;
        Ok(())
    }
    pub fn state(&self, intent_id: &str) -> Result<Option<EffectState>, String> {
        let tx = self
            .conn
            .unchecked_transaction()
            .map_err(|e| e.to_string())?;
        verify_effect_integrity(&tx, &self.master_key, &self.scope_hash)?;
        let raw: Option<String> = tx
            .query_row(
                "SELECT state FROM effects WHERE scope_hash=?1 AND intent_id=?2",
                params![self.scope_hash, intent_id],
                |r| r.get(0),
            )
            .optional()
            .map_err(|e| e.to_string())?;
        raw.map(|s| {
            EffectState::from_db(&s).map(|state| {
                if state == EffectState::Started && !self.started_here.contains(intent_id) {
                    EffectState::Unknown
                } else {
                    state
                }
            })
        })
        .transpose()
    }
    pub fn precheck(
        &self,
        intent_id: &str,
        args_hash: &str,
        attempt_fence: &str,
        grant_nonce: &str,
    ) -> Result<Option<BeginResult>, String> {
        let tx = self
            .conn
            .unchecked_transaction()
            .map_err(|e| e.to_string())?;
        verify_effect_integrity(&tx, &self.master_key, &self.scope_hash)?;
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
                return Ok(Some(BeginResult::IntentConflict));
            }
            return Ok(Some(BeginResult::Replay(EffectState::from_db(&state)?)));
        }
        let grant_used: bool = tx
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
        verify_effect_integrity(&tx, &self.master_key, &self.scope_hash)?;
        // Stop/revoke and STARTED share the SQLite writer lock. Check the
        // authenticated record inside this transaction rather than relying on
        // an Agent's potentially stale in-memory copy.
        let control: Option<(i64, i64, String)> = tx
            .query_row(
                "SELECT stopped, revoked, audit_json FROM local_control WHERE scope_hash=?1",
                params![self.scope_hash],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()
            .map_err(|e| e.to_string())?;
        {
            let (stopped, revoked, sealed) = control.ok_or("MISSING_LOCAL_CONTROL")?;
            if is_legacy_payload(&sealed) {
                return Err("LEGACY_CONTROL_REQUIRES_MIGRATION".into());
            }
            let raw = open_payload(&self.master_key, &self.scope_hash, "local_control", &sealed)?;
            let payload: SealedControlPayload =
                serde_json::from_slice(&raw).map_err(|_| "invalid control envelope")?;
            if payload.v != 2
                || payload.stopped != (stopped != 0)
                || payload.revoked != (revoked != 0)
            {
                return Err("tampered local_control flags or unsupported version".into());
            }
            if payload.stopped || payload.revoked {
                return Err("LOCAL_STOP_OR_REVOKED".into());
            }
        }
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
        update_effect_integrity(&tx, &self.master_key, &self.scope_hash)?;
        tx.commit().map_err(|e| e.to_string())?;
        self.started_here.insert(intent_id.to_owned());
        Ok(BeginResult::StartedNew)
    }
    pub fn finish(&mut self, intent_id: &str, outcome: EffectState) -> Result<(), String> {
        if outcome == EffectState::Started {
            return Err("cannot reenter STARTED".into());
        }
        if !self.started_here.contains(intent_id) {
            return Err("effect not owned by this journal instance".into());
        }
        let tx = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|e| e.to_string())?;
        verify_effect_integrity(&tx, &self.master_key, &self.scope_hash)?;
        let n = tx.execute(
            "UPDATE effects SET state=?3 WHERE scope_hash=?1 AND intent_id=?2 AND state='STARTED'",
            params![self.scope_hash, intent_id, outcome.as_db()],
        ).map_err(|e| e.to_string())?;
        if n != 1 {
            return Err("not an active STARTED effect".into());
        }
        update_effect_integrity(&tx, &self.master_key, &self.scope_hash)?;
        tx.commit().map_err(|e| e.to_string())?;
        self.started_here.remove(intent_id);
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
            let sealed = crate::security::seal_legacy_payload(
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
        assert_eq!(
            reopened
                .begin("legacy-intent", "hash", "1", "grant", "permit")
                .unwrap_err(),
            "LEGACY_CONTROL_REQUIRES_MIGRATION"
        );
        assert!(reopened.state("legacy-intent").unwrap().is_none());
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
        let (stopped, revoked, sealed): (i64, i64, String) = reopened
            .conn
            .query_row(
                "SELECT stopped, revoked, audit_json FROM local_control WHERE scope_hash=?1",
                params![reopened.scope_hash],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!((stopped, revoked), (1, 0));
        assert!(sealed.starts_with("enc:v2:"));

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
        reopened
            .save_control(&ControlRecord {
                stopped: true,
                ..reloaded
            })
            .unwrap();
        assert_eq!(
            reopened
                .owner_confirm_clear_legacy_stop(LocalJournal::OWNER_RECOVERY_PHRASE)
                .unwrap_err(),
            "NOT_A_LEGACY_MIGRATED_STOP"
        );
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
            let sealed = crate::security::seal_legacy_payload(
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
        let sealed: String = reopened
            .conn
            .query_row(
                "SELECT audit_json FROM local_control WHERE scope_hash=?1",
                params![reopened.scope_hash],
                |row| row.get(0),
            )
            .unwrap();
        assert!(sealed.starts_with("enc:v2:"));
        assert_eq!(
            reopened
                .owner_confirm_clear_legacy_stop(LocalJournal::OWNER_RECOVERY_PHRASE)
                .unwrap_err(),
            "CANNOT_RECOVER_REVOKED_DEVICE"
        );
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn interrupted_or_corrupt_legacy_migration_never_clears_revoke() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("journal.db");
        let scope = Scope {
            os_user: "u".into(),
            tenant_id: "t".into(),
            owner_user_id: "o".into(),
            device_identity_id: "i".into(),
            device_id: "d".into(),
        };
        let journal = LocalJournal::open(&path, &scope).unwrap();
        let envelope = SealedControlPayload {
            v: 2,
            stopped: true,
            revoked: true,
            audit: vec!["revoke".into()],
        };
        let original = crate::security::seal_legacy_payload(
            &journal.master_key,
            &journal.scope_hash,
            "local_control",
            &serde_json::to_vec(&envelope).unwrap(),
        )
        .unwrap();
        journal
            .conn
            .execute(
                "INSERT INTO local_control VALUES(?1,1,1,?2)",
                params![journal.scope_hash, original],
            )
            .unwrap();
        journal.conn.execute_batch(
            "CREATE TRIGGER fail_migration BEFORE UPDATE ON local_control BEGIN SELECT RAISE(ABORT,'injected'); END;"
        ).unwrap();
        assert!(journal.load_control(vec![]).is_err());
        let unchanged: String = journal
            .conn
            .query_row("SELECT audit_json FROM local_control", [], |row| row.get(0))
            .unwrap();
        assert_eq!(unchanged, original);
        journal
            .conn
            .execute_batch("DROP TRIGGER fail_migration")
            .unwrap();
        let mut tampered = original.clone();
        let last = tampered.pop().unwrap();
        tampered.push(if last == '0' { '1' } else { '0' });
        journal
            .conn
            .execute("UPDATE local_control SET audit_json=?1", params![tampered])
            .unwrap();
        assert!(journal.load_control(vec![]).is_err());
        journal
            .conn
            .execute("UPDATE local_control SET audit_json=?1", params![original])
            .unwrap();
        drop(journal);
        let reopened = LocalJournal::open(&path, &scope).unwrap();
        let restored = reopened.load_control(vec![]).unwrap();
        assert!(restored.stopped && restored.revoked);
        let migrated: String = reopened
            .conn
            .query_row("SELECT audit_json FROM local_control", [], |row| row.get(0))
            .unwrap();
        assert!(migrated.starts_with("enc:v2:"));
    }

    #[test]
    fn stale_writer_cannot_clear_stop_or_revoke() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("journal.db");
        let scope = Scope {
            os_user: "u".into(),
            tenant_id: "t".into(),
            owner_user_id: "o".into(),
            device_identity_id: "i".into(),
            device_id: "d".into(),
        };
        let mut first = LocalJournal::open(&path, &scope).unwrap();
        let mut stale = LocalJournal::open(&path, &scope).unwrap();
        first
            .save_control(&ControlRecord {
                stopped: true,
                revoked: true,
                audit: vec!["revoke".into()],
            })
            .unwrap();
        assert!(stale
            .save_control(&ControlRecord {
                stopped: false,
                revoked: false,
                audit: vec![]
            })
            .is_err());
        let record = stale.load_control(vec![]).unwrap();
        assert!(record.stopped && record.revoked);
    }

    #[test]
    fn persisted_stop_blocks_a_previously_open_journal_before_consumption() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("journal.db");
        let scope = Scope {
            os_user: "u".into(),
            tenant_id: "t".into(),
            owner_user_id: "o".into(),
            device_identity_id: "i".into(),
            device_id: "d".into(),
        };
        let mut first = LocalJournal::open(&path, &scope).unwrap();
        let mut stale = LocalJournal::open(&path, &scope).unwrap();
        first
            .save_control(&ControlRecord {
                stopped: true,
                revoked: false,
                audit: vec!["STOP".into()],
            })
            .unwrap();
        assert_eq!(
            stale
                .begin("intent", "fingerprint", "1", "grant", "permit")
                .unwrap_err(),
            "LOCAL_STOP_OR_REVOKED"
        );
        assert!(stale.state("intent").unwrap().is_none());
        let count: i64 = stale
            .conn
            .query_row("SELECT count(*) FROM used_permits", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 0);
    }

    #[test]
    fn another_open_does_not_rewrite_a_live_started_effect() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("journal.db");
        let scope = Scope {
            os_user: "u".into(),
            tenant_id: "t".into(),
            owner_user_id: "o".into(),
            device_identity_id: "i".into(),
            device_id: "d".into(),
        };
        let mut writer = LocalJournal::open(&path, &scope).unwrap();
        writer
            .save_control(&ControlRecord {
                stopped: false,
                revoked: false,
                audit: vec![],
            })
            .unwrap();
        assert_eq!(
            writer
                .begin("intent", "fingerprint", "1", "grant", "permit")
                .unwrap(),
            BeginResult::StartedNew
        );
        let mut reader = LocalJournal::open(&path, &scope).unwrap();
        assert_eq!(reader.state("intent").unwrap(), Some(EffectState::Unknown));
        assert!(reader
            .finish("intent", EffectState::CompletedLocal)
            .is_err());
        assert_eq!(writer.state("intent").unwrap(), Some(EffectState::Started));
        writer
            .finish("intent", EffectState::CompletedLocal)
            .unwrap();
        assert_eq!(
            reader.state("intent").unwrap(),
            Some(EffectState::CompletedLocal)
        );
    }

    #[test]
    fn tampered_effect_and_deleted_nonce_block_read_and_admission() {
        for sql in [
            "UPDATE effects SET state='COMPLETED_LOCAL'",
            "DELETE FROM used_grants",
            "DELETE FROM used_permits",
            "DELETE FROM effects",
        ] {
            let dir = tempfile::tempdir().unwrap();
            let path = dir.path().join("journal.db");
            let scope = Scope {
                os_user: "u".into(),
                tenant_id: "t".into(),
                owner_user_id: "o".into(),
                device_identity_id: "i".into(),
                device_id: "d".into(),
            };
            let mut journal = LocalJournal::open(&path, &scope).unwrap();
            journal
                .save_control(&ControlRecord {
                    stopped: false,
                    revoked: false,
                    audit: vec![],
                })
                .unwrap();
            journal
                .begin("intent", "fingerprint", "1", "grant", "permit")
                .unwrap();
            journal.conn.execute(sql, []).unwrap();
            assert!(journal.state("intent").is_err(), "{sql}");
            assert!(
                journal
                    .precheck("other", "fingerprint", "1", "other-grant")
                    .is_err(),
                "{sql}"
            );
            assert!(
                journal
                    .begin("other", "fingerprint", "1", "other-grant", "other-permit")
                    .is_err(),
                "{sql}"
            );
            assert!(
                journal
                    .finish("intent", EffectState::CompletedLocal)
                    .is_err(),
                "{sql}"
            );
            drop(journal);
            assert!(LocalJournal::open(&path, &scope).is_err(), "{sql}");
        }
    }

    #[test]
    fn integrity_guard_update_failure_rolls_back_admission() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("journal.db");
        let scope = Scope {
            os_user: "u".into(),
            tenant_id: "t".into(),
            owner_user_id: "o".into(),
            device_identity_id: "i".into(),
            device_id: "d".into(),
        };
        let mut journal = LocalJournal::open(&path, &scope).unwrap();
        journal
            .save_control(&ControlRecord {
                stopped: false,
                revoked: false,
                audit: vec![],
            })
            .unwrap();
        journal.conn.execute_batch("CREATE TRIGGER reject_guard BEFORE UPDATE ON effect_integrity BEGIN SELECT RAISE(ABORT,'injected'); END;").unwrap();
        assert!(journal
            .begin("intent", "fingerprint", "1", "grant", "permit")
            .is_err());
        assert_eq!(journal.state("intent").unwrap(), None);
        let count: i64 = journal
            .conn
            .query_row("SELECT count(*) FROM used_grants", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 0);
        journal
            .conn
            .execute_batch("DROP TRIGGER reject_guard")
            .unwrap();
        assert_eq!(
            journal
                .begin("intent", "fingerprint", "1", "grant", "permit")
                .unwrap(),
            BeginResult::StartedNew
        );
    }

    #[test]
    fn existing_unsealed_effects_are_not_silently_trusted() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("journal.db");
        let scope = Scope {
            os_user: "u".into(),
            tenant_id: "t".into(),
            owner_user_id: "o".into(),
            device_identity_id: "i".into(),
            device_id: "d".into(),
        };
        let mut journal = LocalJournal::open(&path, &scope).unwrap();
        journal
            .save_control(&ControlRecord {
                stopped: false,
                revoked: false,
                audit: vec![],
            })
            .unwrap();
        journal
            .begin("intent", "fingerprint", "1", "grant", "permit")
            .unwrap();
        journal
            .conn
            .execute("DELETE FROM effect_integrity", [])
            .unwrap();
        drop(journal);
        assert!(LocalJournal::open(&path, &scope).is_err());
    }

    #[test]
    fn deleted_guard_and_history_do_not_reinitialize_existing_scope() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("journal.db");
        let scope = Scope {
            os_user: "u".into(),
            tenant_id: "t".into(),
            owner_user_id: "o".into(),
            device_identity_id: "i".into(),
            device_id: "d".into(),
        };
        let mut journal = LocalJournal::open(&path, &scope).unwrap();
        journal
            .save_control(&ControlRecord {
                stopped: false,
                revoked: false,
                audit: vec![],
            })
            .unwrap();
        journal
            .begin("intent", "fingerprint", "1", "grant", "permit")
            .unwrap();
        journal.conn.execute_batch("DELETE FROM effects; DELETE FROM used_grants; DELETE FROM used_permits; DELETE FROM effect_integrity;").unwrap();
        drop(journal);
        assert!(LocalJournal::open(&path, &scope).is_err());
    }

    #[test]
    fn corrupted_control_blocks_journal_start() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("journal.db");
        let scope = Scope {
            os_user: "u".into(),
            tenant_id: "t".into(),
            owner_user_id: "o".into(),
            device_identity_id: "i".into(),
            device_id: "d".into(),
        };
        let mut journal = LocalJournal::open(&path, &scope).unwrap();
        journal
            .save_control(&ControlRecord {
                stopped: true,
                revoked: true,
                audit: vec![],
            })
            .unwrap();
        journal
            .conn
            .execute("UPDATE local_control SET stopped=0, revoked=0", [])
            .unwrap();
        assert!(journal
            .begin("intent", "fingerprint", "1", "grant", "permit")
            .is_err());
        assert!(journal.state("intent").unwrap().is_none());
    }

    #[test]
    fn deleted_control_blocks_start_even_for_existing_writer() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("journal.db");
        let scope = Scope {
            os_user: "u".into(),
            tenant_id: "t".into(),
            owner_user_id: "o".into(),
            device_identity_id: "i".into(),
            device_id: "d".into(),
        };
        let mut journal = LocalJournal::open(&path, &scope).unwrap();
        journal
            .save_control(&ControlRecord {
                stopped: true,
                revoked: true,
                audit: vec![],
            })
            .unwrap();
        journal
            .conn
            .execute("DELETE FROM local_control", [])
            .unwrap();
        assert!(journal.load_control(vec![]).is_err());
        assert!(journal
            .save_control(&ControlRecord {
                stopped: false,
                revoked: false,
                audit: vec![]
            })
            .is_err());
        assert_eq!(
            journal
                .begin("intent", "fingerprint", "1", "grant", "permit")
                .unwrap_err(),
            "MISSING_LOCAL_CONTROL"
        );
        assert!(journal.state("intent").unwrap().is_none());
        drop(journal);
        let mut reopened = LocalJournal::open(&path, &scope).unwrap();
        assert!(reopened.load_control(vec![]).is_err());
        assert!(reopened
            .save_control(&ControlRecord {
                stopped: false,
                revoked: false,
                audit: vec![]
            })
            .is_err());
    }
}
