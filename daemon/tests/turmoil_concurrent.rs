/// Turmoil deterministic simulation test for concurrent sessions.
///
/// Tests that the check engine handles concurrent access from multiple
/// sessions without data races or incorrect results.
///
/// Note: Turmoil is designed for async network simulation. Since our
/// check engine is synchronous and thread-safe (via pre-compiled patterns),
/// we focus on testing concurrent session map operations and override
/// consumption races.
use std::sync::Arc;

#[test]
fn concurrent_session_registration() {
    // Test that concurrent session registrations don't race
    let sessions = dl::check::CheckEngine::new();
    let sessions_map = Arc::new(dashmap::DashMap::<String, u32>::new());

    let handles: Vec<_> = (0..100)
        .map(|i| {
            let map = Arc::clone(&sessions_map);
            let _engine = &sessions;
            std::thread::spawn(move || {
                let key = format!("session-{i}");
                map.insert(key.clone(), 0);
                // Simulate check
                let request = dl::check::CheckRequest {
                    tool_name: "Write".to_string(),
                    tool_input: serde_json::json!({"file_path": format!("src/file{i}.rs")}),
                    phase: dl::check::CheckPhase::Pre,
                    session_id: Some(key.clone()),
                };
                let engine = dl::check::CheckEngine::new();
                let result = engine.check(&request);
                assert_eq!(result.action, dl::check::Action::Allow);

                // Increment check count
                if let Some(mut entry) = map.get_mut(&key) {
                    *entry += 1;
                }
            })
        })
        .collect();

    for h in handles {
        h.join().unwrap();
    }

    assert_eq!(sessions_map.len(), 100);
    for entry in sessions_map.iter() {
        assert_eq!(*entry.value(), 1);
    }
}

#[test]
fn concurrent_check_engine_is_safe() {
    // The check engine uses pre-compiled regex patterns (immutable after init).
    // This test verifies concurrent checks don't produce incorrect results.
    let engine = Arc::new(dl::check::CheckEngine::new());

    let handles: Vec<_> = (0..50)
        .map(|i| {
            let engine = Arc::clone(&engine);
            std::thread::spawn(move || {
                // Alternate between allowed and blocked paths
                if i % 2 == 0 {
                    let request = dl::check::CheckRequest {
                        tool_name: "Write".to_string(),
                        tool_input: serde_json::json!({"file_path": "src/main.rs"}),
                        phase: dl::check::CheckPhase::Pre,
                        session_id: None,
                    };
                    let result = engine.check(&request);
                    assert_eq!(result.action, dl::check::Action::Allow);
                } else {
                    let request = dl::check::CheckRequest {
                        tool_name: "Write".to_string(),
                        tool_input: serde_json::json!({"file_path": ".env"}),
                        phase: dl::check::CheckPhase::Pre,
                        session_id: None,
                    };
                    let result = engine.check(&request);
                    assert_eq!(result.action, dl::check::Action::Block);
                }
            })
        })
        .collect();

    for h in handles {
        h.join().unwrap();
    }
}

#[test]
fn concurrent_dangerous_ops_checks() {
    // Verify dangerous ops regex scanning under concurrent load
    let engine = Arc::new(dl::check::CheckEngine::new());

    let handles: Vec<_> = (0..50)
        .map(|i| {
            let engine = Arc::clone(&engine);
            std::thread::spawn(move || {
                let command = if i % 3 == 0 {
                    "rm -rf /"
                } else if i % 3 == 1 {
                    "git push --force"
                } else {
                    "cargo test"
                };

                let request = dl::check::CheckRequest {
                    tool_name: "Bash".to_string(),
                    tool_input: serde_json::json!({"command": command}),
                    phase: dl::check::CheckPhase::Pre,
                    session_id: None,
                };
                let result = engine.check(&request);

                match i % 3 {
                    0 => assert_eq!(result.action, dl::check::Action::Block),
                    1 => assert_eq!(result.action, dl::check::Action::Warn),
                    _ => assert_eq!(result.action, dl::check::Action::Allow),
                }
            })
        })
        .collect();

    for h in handles {
        h.join().unwrap();
    }
}

#[test]
fn concurrent_secret_scanning() {
    // Verify secret scanning under concurrent load
    let engine = Arc::new(dl::check::CheckEngine::new());

    let handles: Vec<_> = (0..30)
        .map(|i| {
            let engine = Arc::clone(&engine);
            std::thread::spawn(move || {
                let actual_content = if i % 2 == 0 {
                    "let x = 42;\nfn main() {}\n".to_string()
                } else {
                    let fake_key = "a".repeat(40);
                    format!("let api_key = \"sk-{fake_key}\";\n")
                };

                let request = dl::check::CheckRequest {
                    tool_name: "Write".to_string(),
                    tool_input: serde_json::json!({
                        "file_path": "src/config.rs",
                        "content": actual_content
                    }),
                    phase: dl::check::CheckPhase::Post,
                    session_id: None,
                };
                let result = engine.check(&request);

                if i % 2 == 0 {
                    assert_eq!(result.action, dl::check::Action::Allow);
                } else {
                    assert_eq!(result.action, dl::check::Action::Warn);
                }
            })
        })
        .collect();

    for h in handles {
        h.join().unwrap();
    }
}
