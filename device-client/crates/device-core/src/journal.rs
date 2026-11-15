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
                let audit: Vec<String> =
                    serde_json::from_slice(&raw_json).map_err(|e| e.to_string())?;
                Ok(ControlRecord {
                    stopped: stopped != 0,
                    revoked: revoked != 0,
                    audit,
                })
            }
            None => Ok(ControlRecord {
                stopped: false,
                revoked: false,
                audit: default_audit,
            }),
        }
    }

    pub fn save_control(&mut self, record: &ControlRecord) -> Result<(), String> {
        let audit_json = serde_json::to_string(&record.audit).map_err(|e| e.to_string())?;
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
