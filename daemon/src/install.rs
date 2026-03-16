use serde_json::{json, Value};
use std::path::PathBuf;

fn settings_path() -> PathBuf {
    dirs::home_dir()
        .unwrap_or_else(|| PathBuf::from("/tmp"))
        .join(".claude")
        .join("settings.json")
}

/// The hook entries we install into settings.json.
fn dl_hook_entries() -> Value {
    json!({
        "PreToolUse": [
            {
                "matcher": "Write|Edit",
                "hooks": [{ "type": "command", "command": "dl hook pre-tool-use" }]
            },
            {
                "matcher": "Bash",
                "hooks": [{ "type": "command", "command": "dl hook pre-tool-use" }]
            }
        ],
        "PostToolUse": [
            {
                "matcher": "Write|Edit",
                "hooks": [{ "type": "command", "command": "dl hook post-tool-use" }]
            }
        ],
        "SessionStart": [
            {
                "hooks": [{ "type": "command", "command": "dl hook session-start" }]
            }
        ],
        "SessionEnd": [
            {
                "hooks": [{ "type": "command", "command": "dl hook session-end" }]
            }
        ],
        "Stop": [
            {
                "hooks": [{ "type": "command", "command": "dl hook stop" }]
            }
        ]
    })
}

/// Returns true if a hook entry was installed by dl (command starts with "dl hook").
fn is_dl_hook_entry(entry: &Value) -> bool {
    entry
        .get("hooks")
        .and_then(|h| h.as_array())
        .map(|hooks| {
            hooks.iter().any(|h| {
                h.get("command")
                    .and_then(|c| c.as_str())
                    .is_some_and(|c| c.starts_with("dl hook"))
            })
        })
        .unwrap_or(false)
}

/// Install dl hooks into ~/.claude/settings.json.
/// Preserves all existing hooks (idempotent — removes old dl hooks first).
pub fn install() {
    let path = settings_path();

    // Read existing settings
    let mut settings: Value = if path.exists() {
        match std::fs::read_to_string(&path) {
            Ok(content) => serde_json::from_str(&content).unwrap_or_else(|_| json!({})),
            Err(e) => {
                eprintln!("Failed to read {}: {e}", path.display());
                std::process::exit(1);
            }
        }
    } else {
        json!({})
    };

    // Ensure hooks object exists
    if settings.get("hooks").is_none() {
        settings["hooks"] = json!({});
    }

    let our_hooks = dl_hook_entries();
    let hooks = settings["hooks"].as_object_mut().unwrap();

    for (event_name, entries) in our_hooks.as_object().unwrap() {
        let existing = hooks.entry(event_name.clone()).or_insert_with(|| json!([]));

        if let Some(arr) = existing.as_array_mut() {
            // Remove any existing dl hook entries first (idempotent reinstall)
            arr.retain(|entry| !is_dl_hook_entry(entry));

            // Append our entries
            if let Some(new_entries) = entries.as_array() {
                for entry in new_entries {
                    arr.push(entry.clone());
                }
            }
        }
    }

    // Write back with pretty formatting
    let content = serde_json::to_string_pretty(&settings).unwrap();
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    std::fs::write(&path, format!("{content}\n")).unwrap_or_else(|e| {
        eprintln!("Failed to write {}: {e}", path.display());
        std::process::exit(1);
    });

    println!("Installed dl hooks into {}", path.display());
    println!(
        "  PreToolUse:  Write|Edit (deny list), Bash (dangerous ops)\n  \
         PostToolUse: Write|Edit (secret scan)\n  \
         SessionStart, SessionEnd\n  \
         Stop: context guard + handoff"
    );
}

/// Remove all dl hook entries from ~/.claude/settings.json.
/// Preserves all non-dl hooks.
pub fn uninstall() {
    let path = settings_path();

    if !path.exists() {
        println!("No settings file found at {}", path.display());
        return;
    }

    let content = match std::fs::read_to_string(&path) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("Failed to read {}: {e}", path.display());
            std::process::exit(1);
        }
    };

    let mut settings: Value = match serde_json::from_str(&content) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("Failed to parse {}: {e}", path.display());
            std::process::exit(1);
        }
    };

    let mut removed = 0;
    if let Some(hooks) = settings.get_mut("hooks").and_then(|h| h.as_object_mut()) {
        for (_event_name, entries) in hooks.iter_mut() {
            if let Some(arr) = entries.as_array_mut() {
                let before = arr.len();
                arr.retain(|entry| !is_dl_hook_entry(entry));
                removed += before - arr.len();
            }
        }

        // Remove empty event arrays
        hooks.retain(|_, v| v.as_array().map(|a| !a.is_empty()).unwrap_or(true));
    }

    let content = serde_json::to_string_pretty(&settings).unwrap();
    std::fs::write(&path, format!("{content}\n")).unwrap_or_else(|e| {
        eprintln!("Failed to write {}: {e}", path.display());
        std::process::exit(1);
    });

    println!(
        "Removed {removed} dl hook entries from {}",
        path.display()
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn is_dl_hook_detects_dl_commands() {
        let entry = json!({
            "matcher": "Write|Edit",
            "hooks": [{ "type": "command", "command": "dl hook pre-tool-use" }]
        });
        assert!(is_dl_hook_entry(&entry));
    }

    #[test]
    fn is_dl_hook_ignores_other_commands() {
        let entry = json!({
            "matcher": "Read",
            "hooks": [{ "type": "command", "command": "/home/user/resize-images.sh" }]
        });
        assert!(!is_dl_hook_entry(&entry));
    }

    #[test]
    fn install_into_empty_settings() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join(".claude").join("settings.json");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, "{}").unwrap();

        // Simulate install by testing the merge logic directly
        let mut settings: Value = json!({});
        settings["hooks"] = json!({});

        let our_hooks = dl_hook_entries();
        let hooks = settings["hooks"].as_object_mut().unwrap();

        for (event_name, entries) in our_hooks.as_object().unwrap() {
            let existing = hooks.entry(event_name.clone()).or_insert_with(|| json!([]));
            if let Some(arr) = existing.as_array_mut() {
                arr.retain(|entry| !is_dl_hook_entry(entry));
                if let Some(new_entries) = entries.as_array() {
                    for entry in new_entries {
                        arr.push(entry.clone());
                    }
                }
            }
        }

        // Verify PreToolUse has 2 entries (Write|Edit + Bash)
        let pre = hooks["PreToolUse"].as_array().unwrap();
        assert_eq!(pre.len(), 2);

        // Verify PostToolUse has 1 entry
        let post = hooks["PostToolUse"].as_array().unwrap();
        assert_eq!(post.len(), 1);

        // Verify SessionStart/End/Stop have 1 entry each
        assert_eq!(hooks["SessionStart"].as_array().unwrap().len(), 1);
        assert_eq!(hooks["SessionEnd"].as_array().unwrap().len(), 1);
        assert_eq!(hooks["Stop"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn install_preserves_existing_hooks() {
        let mut settings: Value = json!({
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Read",
                        "hooks": [{ "type": "command", "command": "/home/user/resize-images.sh" }]
                    }
                ]
            }
        });

        let our_hooks = dl_hook_entries();
        let hooks = settings["hooks"].as_object_mut().unwrap();

        for (event_name, entries) in our_hooks.as_object().unwrap() {
            let existing = hooks.entry(event_name.clone()).or_insert_with(|| json!([]));
            if let Some(arr) = existing.as_array_mut() {
                arr.retain(|entry| !is_dl_hook_entry(entry));
                if let Some(new_entries) = entries.as_array() {
                    for entry in new_entries {
                        arr.push(entry.clone());
                    }
                }
            }
        }

        // PreToolUse should have 3 entries: 1 existing + 2 dl hooks
        let pre = hooks["PreToolUse"].as_array().unwrap();
        assert_eq!(pre.len(), 3);

        // First entry should be the preserved resize hook
        assert_eq!(
            pre[0]["hooks"][0]["command"].as_str().unwrap(),
            "/home/user/resize-images.sh"
        );
    }

    #[test]
    fn install_is_idempotent() {
        let mut settings: Value = json!({ "hooks": {} });

        // Install twice
        for _ in 0..2 {
            let our_hooks = dl_hook_entries();
            let hooks = settings["hooks"].as_object_mut().unwrap();

            for (event_name, entries) in our_hooks.as_object().unwrap() {
                let existing = hooks.entry(event_name.clone()).or_insert_with(|| json!([]));
                if let Some(arr) = existing.as_array_mut() {
                    arr.retain(|entry| !is_dl_hook_entry(entry));
                    if let Some(new_entries) = entries.as_array() {
                        for entry in new_entries {
                            arr.push(entry.clone());
                        }
                    }
                }
            }
        }

        // Should still have exactly 2 PreToolUse entries
        let hooks = settings["hooks"].as_object().unwrap();
        assert_eq!(hooks["PreToolUse"].as_array().unwrap().len(), 2);
    }

    #[test]
    fn uninstall_removes_only_dl_hooks() {
        let mut settings: Value = json!({
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Read",
                        "hooks": [{ "type": "command", "command": "/home/user/resize-images.sh" }]
                    },
                    {
                        "matcher": "Write|Edit",
                        "hooks": [{ "type": "command", "command": "dl hook pre-tool-use" }]
                    },
                    {
                        "matcher": "Bash",
                        "hooks": [{ "type": "command", "command": "dl hook pre-tool-use" }]
                    }
                ],
                "PostToolUse": [
                    {
                        "matcher": "Write|Edit",
                        "hooks": [{ "type": "command", "command": "dl hook post-tool-use" }]
                    }
                ]
            }
        });

        // Simulate uninstall
        if let Some(hooks) = settings.get_mut("hooks").and_then(|h| h.as_object_mut()) {
            for (_event_name, entries) in hooks.iter_mut() {
                if let Some(arr) = entries.as_array_mut() {
                    arr.retain(|entry| !is_dl_hook_entry(entry));
                }
            }
            hooks.retain(|_, v| v.as_array().map(|a| !a.is_empty()).unwrap_or(true));
        }

        let hooks = settings["hooks"].as_object().unwrap();

        // PreToolUse should have only the resize hook left
        assert_eq!(hooks["PreToolUse"].as_array().unwrap().len(), 1);
        assert_eq!(
            hooks["PreToolUse"][0]["hooks"][0]["command"].as_str().unwrap(),
            "/home/user/resize-images.sh"
        );

        // PostToolUse should be removed entirely (was empty after removal)
        assert!(hooks.get("PostToolUse").is_none());
    }
}
