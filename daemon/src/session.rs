use dashmap::DashMap;
use std::sync::Arc;
use std::time::Instant;

/// Information about an active Claude Code session tracked by the daemon.
#[derive(Debug, Clone)]
pub struct SessionInfo {
    pub session_id: String,
    pub cwd: String,
    pub repo_root: Option<String>,
    pub started_at: Instant,
    pub started_at_utc: chrono::DateTime<chrono::Utc>,
    pub trace_id: String,
    pub root_span_id: String,
    pub check_count: u32,
    pub block_count: u32,
    pub warn_count: u32,
}

/// Thread-safe map of active sessions, keyed by session_id.
pub type SessionMap = Arc<DashMap<String, SessionInfo>>;

pub fn new_session_map() -> SessionMap {
    Arc::new(DashMap::new())
}

/// Register a new session. Returns (trace_id, root_span_id).
pub fn register(
    sessions: &SessionMap,
    session_id: String,
    cwd: String,
    repo_root: Option<String>,
) -> (String, String) {
    let trace_id = random_hex(16);
    let root_span_id = random_hex(8);

    let info = SessionInfo {
        session_id: session_id.clone(),
        cwd,
        repo_root,
        started_at: Instant::now(),
        started_at_utc: chrono::Utc::now(),
        trace_id: trace_id.clone(),
        root_span_id: root_span_id.clone(),
        check_count: 0,
        block_count: 0,
        warn_count: 0,
    };

    sessions.insert(session_id, info);
    (trace_id, root_span_id)
}

/// Record a check event for a session. Increments counters.
pub fn record_check(sessions: &SessionMap, session_id: &str, action: &str) {
    if let Some(mut session) = sessions.get_mut(session_id) {
        session.check_count += 1;
        match action {
            "block" => session.block_count += 1,
            "warn" => session.warn_count += 1,
            _ => {}
        }
    }
}

/// Remove a session and return its info for summary/span emission.
pub fn deregister(sessions: &SessionMap, session_id: &str) -> Option<SessionInfo> {
    sessions.remove(session_id).map(|(_, info)| info)
}

/// Generate random hex string (len bytes → 2*len hex chars).
fn random_hex(len: usize) -> String {
    let mut bytes = vec![0u8; len];
    getrandom::fill(&mut bytes).expect("getrandom failed");
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn register_and_deregister() {
        let sessions = new_session_map();
        let (trace_id, span_id) = register(
            &sessions,
            "s1".into(),
            "/home/user/repo".into(),
            Some("/home/user/repo".into()),
        );
        assert_eq!(trace_id.len(), 32);
        assert_eq!(span_id.len(), 16);
        assert_eq!(sessions.len(), 1);

        let info = deregister(&sessions, "s1").unwrap();
        assert_eq!(info.session_id, "s1");
        assert_eq!(info.cwd, "/home/user/repo");
        assert_eq!(sessions.len(), 0);
    }

    #[test]
    fn deregister_missing_returns_none() {
        let sessions = new_session_map();
        assert!(deregister(&sessions, "nonexistent").is_none());
    }

    #[test]
    fn record_check_counters() {
        let sessions = new_session_map();
        register(&sessions, "s1".into(), "/repo".into(), None);

        record_check(&sessions, "s1", "allow");
        record_check(&sessions, "s1", "block");
        record_check(&sessions, "s1", "warn");
        record_check(&sessions, "s1", "allow");

        let info = sessions.get("s1").unwrap();
        assert_eq!(info.check_count, 4);
        assert_eq!(info.block_count, 1);
        assert_eq!(info.warn_count, 1);
    }

    #[test]
    fn record_check_missing_session_is_noop() {
        let sessions = new_session_map();
        record_check(&sessions, "nonexistent", "block"); // should not panic
    }

    #[test]
    fn multiple_concurrent_sessions() {
        let sessions = new_session_map();
        register(&sessions, "s1".into(), "/repo1".into(), None);
        register(&sessions, "s2".into(), "/repo2".into(), None);
        register(&sessions, "s3".into(), "/repo3".into(), None);
        assert_eq!(sessions.len(), 3);

        deregister(&sessions, "s2");
        assert_eq!(sessions.len(), 2);
        assert!(sessions.get("s1").is_some());
        assert!(sessions.get("s2").is_none());
        assert!(sessions.get("s3").is_some());
    }

    #[test]
    fn random_hex_uniqueness() {
        let a = random_hex(16);
        let b = random_hex(16);
        assert_ne!(a, b);
        assert_eq!(a.len(), 32);
    }
}
