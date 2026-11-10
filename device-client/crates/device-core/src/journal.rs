use crate::protocol::Scope;
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use std::path::Path;

/// STARTED is durable before mock adapter execution. It is UNKNOWN after recovery.
/// SQLite in this slice contains metadata-only mock data and is NOT production encrypted.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EffectState { Started, CompletedLocal, Failed, Unknown }
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
    PermitReused,
}
pub struct LocalJournal {
    conn: Connection,
    scope_hash: String,
}
impl LocalJournal {
    /// This is strictly a mock metadata store. Callers must not store secrets in arguments.
    pub fn open(path: &Path, scope: &Scope) -> Result<Self, String> {
        if !scope.valid() { return Err("invalid scope".into()); }
        let conn = Connection::open(path).map_err(|e| e.to_string())?;
        conn.pragma_update(None, "journal_mode", "WAL").map_err(|e| e.to_string())?;
        conn.pragma_update(None, "synchronous", "FULL").map_err(|e| e.to_string())?;
        conn.execute_batch("
            CREATE TABLE IF NOT EXISTS effects (
                scope_hash TEXT NOT NULL,
                intent_id TEXT NOT NULL,
                args_hash TEXT NOT NULL,
                attempt_fence TEXT NOT NULL,
                permit_nonce TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('STARTED','COMPLETED_LOCAL','FAILED','UNKNOWN')),
                PRIMARY KEY (scope_hash, intent_id)
            );
            CREATE TABLE IF NOT EXISTS used_permits (
                scope_hash TEXT NOT NULL,
                permit_nonce TEXT NOT NULL,
                intent_id TEXT NOT NULL,
                PRIMARY KEY (scope_hash, permit_nonce)
            );
        ").map_err(|e| e.to_string())?;
        let scope_hash = scope.namespace();
        // A process that vanished after STARTED might have delivered an effect.
        conn.execute("UPDATE effects SET state='UNKNOWN' WHERE scope_hash=?1 AND state='STARTED'",
                     params![scope_hash]).map_err(|e| e.to_string())?;
        Ok(Self { conn, scope_hash })
    }
    pub fn state(&self, intent_id: &str) -> Result<Option<EffectState>, String> {
        let raw: Option<String> = self.conn.query_row(
            "SELECT state FROM effects WHERE scope_hash=?1 AND intent_id=?2",
            params![self.scope_hash, intent_id], |r| r.get(0),
        ).optional().map_err(|e| e.to_string())?;
        raw.map(|s| EffectState::from_db(&s)).transpose()
    }
    pub fn begin(&mut self, intent_id: &str, args_hash: &str, attempt_fence: &str,
                 permit_nonce: &str) -> Result<BeginResult, String> {
        let tx = self.conn.transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|e| e.to_string())?;
        let previous: Option<(String, String, String)> = tx.query_row(
            "SELECT args_hash, attempt_fence, state FROM effects WHERE scope_hash=?1 AND intent_id=?2",
            params![self.scope_hash, intent_id], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        ).optional().map_err(|e| e.to_string())?;
        if let Some((hash, fence, state)) = previous {
            if hash != args_hash || fence != attempt_fence {
                return Ok(BeginResult::IntentConflict);
            }
            return Ok(BeginResult::Replay(EffectState::from_db(&state)?));
        }
        let permit_used: bool = tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM used_permits WHERE scope_hash=?1 AND permit_nonce=?2)",
            params![self.scope_hash, permit_nonce], |r| r.get(0),
        ).map_err(|e| e.to_string())?;
        if permit_used { return Ok(BeginResult::PermitReused); }
        tx.execute("INSERT INTO used_permits(scope_hash,permit_nonce,intent_id) VALUES(?1,?2,?3)",
                   params![self.scope_hash, permit_nonce, intent_id]).map_err(|e| e.to_string())?;
        tx.execute("INSERT INTO effects(scope_hash,intent_id,args_hash,attempt_fence,permit_nonce,state)
                    VALUES(?1,?2,?3,?4,?5,'STARTED')",
                   params![self.scope_hash, intent_id, args_hash, attempt_fence, permit_nonce])
            .map_err(|e| e.to_string())?;
        tx.commit().map_err(|e| e.to_string())?;
        Ok(BeginResult::StartedNew)
    }
    pub fn finish(&mut self, intent_id: &str, outcome: EffectState) -> Result<(), String> {
        if outcome == EffectState::Started { return Err("cannot reenter STARTED".into()); }
        let n = self.conn.execute(
            "UPDATE effects SET state=?3 WHERE scope_hash=?1 AND intent_id=?2 AND state='STARTED'",
            params![self.scope_hash, intent_id, outcome.as_db()],
        ).map_err(|e| e.to_string())?;
        if n != 1 { return Err("not an active STARTED effect".into()); }
        Ok(())
    }
}
