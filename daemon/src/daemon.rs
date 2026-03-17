use crate::check::CheckEngine;
use crate::config;
use crate::event_log::EventLogWriter;
use crate::server::{self, ServerState};
use crate::session;
use crate::sse::SseBroadcast;
use std::io::{BufRead, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::sync::Arc;

const RUN_DIR: &str = "/tmp/dev-loop";

fn socket_path() -> PathBuf {
    PathBuf::from(RUN_DIR).join("dl.sock")
}

fn pid_path() -> PathBuf {
    PathBuf::from(RUN_DIR).join("dl.pid")
}

fn event_log_path() -> PathBuf {
    PathBuf::from(RUN_DIR).join("events.jsonl")
}

/// Check if a daemon is already running by reading the PID file and checking the process.
fn running_pid() -> Option<u32> {
    let pid_file = pid_path();
    let pid_str = std::fs::read_to_string(&pid_file).ok()?;
    let pid: u32 = pid_str.trim().parse().ok()?;

    // Check if process is alive
    let status = std::process::Command::new("kill")
        .args(["-0", &pid.to_string()])
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .ok()?;

    if status.success() {
        Some(pid)
    } else {
        // Stale PID file — clean up
        let _ = std::fs::remove_file(&pid_file);
        let _ = std::fs::remove_file(socket_path());
        None
    }
}

fn write_pid() {
    let _ = std::fs::create_dir_all(RUN_DIR);
    let _ = std::fs::write(pid_path(), std::process::id().to_string());
}

fn remove_pid() {
    let _ = std::fs::remove_file(pid_path());
    let _ = std::fs::remove_file(socket_path());
}

/// Start the daemon. If already running, print a message and exit.
pub fn start() {
    if let Some(pid) = running_pid() {
        eprintln!("Daemon already running (pid {pid})");
        std::process::exit(1);
    }

    // Fork into background: re-exec ourselves with a hidden env var
    if std::env::var("_DL_DAEMON").is_err() {
        let exe = std::env::current_exe().expect("cannot find own executable");
        let mut child = std::process::Command::new(exe)
            .arg("start")
            .env("_DL_DAEMON", "1")
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
            .expect("failed to spawn daemon");

        // Wait for socket to appear (confirms daemon bound successfully)
        let sock = socket_path();
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
        while std::time::Instant::now() < deadline {
            if sock.exists() {
                println!("Daemon started (pid {})", child.id());
                return;
            }
            std::thread::sleep(std::time::Duration::from_millis(100));
        }

        eprintln!("Daemon failed to start (socket not bound after 2s)");
        // Check if child is still alive
        match child.try_wait() {
            Ok(Some(status)) => eprintln!("  Child exited with: {status}"),
            Ok(None) => eprintln!("  Child still running but socket not created"),
            Err(e) => eprintln!("  Could not check child: {e}"),
        }
        std::process::exit(1);
    }

    // We are the daemon process
    write_pid();

    let rt = tokio::runtime::Runtime::new().expect("failed to create tokio runtime");
    rt.block_on(async {
        let sse = SseBroadcast::new();
        let check_engine = CheckEngine::new();
        let sessions = session::new_session_map();
        let global_config = config::load();
        let event_log = EventLogWriter::spawn(
            &event_log_path(),
            global_config.event_log.channel_capacity,
            global_config.event_log.max_file_size_mb,
            global_config.event_log.max_rotated_files,
        );
        let shared_config = Arc::new(tokio::sync::RwLock::new(global_config));

        let state = Arc::new(ServerState {
            sse,
            event_log,
            check_engine,
            started_at: chrono::Utc::now(),
            sessions,
            config: Arc::clone(&shared_config),
        });

        // Install signal handler for graceful shutdown
        let state_clone = Arc::clone(&state);
        tokio::spawn(async move {
            if let Ok(()) = tokio::signal::ctrl_c().await {
                tracing::info!("Received shutdown signal");
                let event = crate::sse::Event::new("daemon_stopped");
                let _ = state_clone.sse.publish(event.clone());
                state_clone.event_log.log(event);
                // Give the event log a moment to flush
                tokio::time::sleep(tokio::time::Duration::from_millis(100)).await;
                remove_pid();
                std::process::exit(0);
            }
        });

        // Also handle SIGTERM
        tokio::spawn(async {
            let mut sigterm =
                tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                    .expect("failed to install SIGTERM handler");
            sigterm.recv().await;
            tracing::info!("Received SIGTERM");
            remove_pid();
            std::process::exit(0);
        });

        // Handle SIGHUP for config reload
        let state_clone = Arc::clone(&state);
        tokio::spawn(async move {
            let mut sighup =
                tokio::signal::unix::signal(tokio::signal::unix::SignalKind::hangup())
                    .expect("failed to install SIGHUP handler");
            loop {
                sighup.recv().await;
                tracing::info!("Received SIGHUP, reloading config");
                let new_config = config::load();
                *state_clone.config.write().await = new_config;
                let event = crate::sse::Event::new("config_reloaded");
                let _ = state_clone.sse.publish(event.clone());
                state_clone.event_log.log(event);
            }
        });

        server::run(&socket_path(), state).await;
    });
}

/// Stop the daemon by sending SIGTERM to the PID.
pub fn stop() {
    match running_pid() {
        Some(pid) => {
            let status = std::process::Command::new("kill")
                .arg(pid.to_string())
                .status()
                .expect("failed to send signal");

            if status.success() {
                println!("Daemon stopped (pid {pid})");
                // Clean up PID and socket files
                remove_pid();
            } else {
                eprintln!("Failed to stop daemon (pid {pid})");
                std::process::exit(1);
            }
        }
        None => {
            eprintln!("Daemon is not running");
            std::process::exit(1);
        }
    }
}

/// Send SIGHUP to the daemon to reload configuration.
pub fn reload() {
    match running_pid() {
        Some(pid) => {
            let status = std::process::Command::new("kill")
                .args(["-HUP", &pid.to_string()])
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::null())
                .status()
                .expect("failed to send signal");

            if status.success() {
                println!("Sent SIGHUP to daemon (pid {pid}). Config reloading.");
            } else {
                eprintln!("Failed to send SIGHUP to daemon (pid {pid})");
                std::process::exit(1);
            }
        }
        None => {
            eprintln!("Daemon is not running");
            std::process::exit(1);
        }
    }
}

/// Print daemon status by querying the /status endpoint.
pub fn status() {
    match running_pid() {
        Some(pid) => {
            // Try to connect and get status
            match query_endpoint("/status") {
                Ok(body) => {
                    if let Ok(v) = serde_json::from_str::<serde_json::Value>(&body) {
                        let uptime = v["uptime_s"].as_i64().unwrap_or(0);
                        let hours = uptime / 3600;
                        let mins = (uptime % 3600) / 60;
                        let secs = uptime % 60;
                        let session_count = v["active_sessions"].as_i64().unwrap_or(0);
                        let mode = v["ambient_mode"].as_str().unwrap_or("enforce");
                        println!("Status:   running");
                        println!("PID:      {pid}");
                        println!("Mode:     {mode}");
                        println!("Uptime:   {hours:02}:{mins:02}:{secs:02}");
                        println!("Sessions: {session_count} active");
                        if let Some(sessions) = v["sessions"].as_array() {
                            for s in sessions {
                                let sid = s["session_id"].as_str().unwrap_or("?");
                                let cwd = s["cwd"].as_str().unwrap_or("?");
                                let dur = s["duration_s"].as_i64().unwrap_or(0);
                                let checks = s["checks"].as_i64().unwrap_or(0);
                                let blocks = s["blocks"].as_i64().unwrap_or(0);
                                let warns = s["warns"].as_i64().unwrap_or(0);
                                let dm = dur / 60;
                                let ds = dur % 60;
                                println!(
                                    "  {sid}: {cwd} ({dm}m{ds}s, {checks} checks, {blocks} blocks, {warns} warns)"
                                );
                            }
                        }
                        let events_logged = v["events_logged"].as_u64().unwrap_or(0);
                        let events_dropped = v["events_dropped"].as_u64().unwrap_or(0);
                        println!("Events:   {events_logged} logged, {events_dropped} dropped");
                        println!("Socket:   {}", socket_path().display());
                    } else {
                        println!("Running (pid {pid}), raw response: {body}");
                    }
                }
                Err(e) => {
                    println!("Running (pid {pid}), but socket unreachable: {e}");
                }
            }
        }
        None => {
            println!("Status:  stopped");
        }
    }
}

/// Connect to the SSE /inbox endpoint and print events to stdout.
pub fn stream() {
    if running_pid().is_none() {
        eprintln!("Daemon is not running. Start it with: dl start");
        std::process::exit(1);
    }

    println!("Streaming events (Ctrl+C to stop)...\n");

    loop {
        match UnixStream::connect(socket_path()) {
            Ok(mut stream) => {
                let request = "GET /inbox HTTP/1.1\r\nHost: localhost\r\nAccept: text/event-stream\r\n\r\n";
                if stream.write_all(request.as_bytes()).is_err() {
                    eprintln!("Connection lost, reconnecting...");
                    std::thread::sleep(std::time::Duration::from_secs(1));
                    continue;
                }

                // Read and skip HTTP headers
                let mut reader = std::io::BufReader::new(&stream);
                let mut header_line = String::new();
                loop {
                    header_line.clear();
                    match reader.read_line(&mut header_line) {
                        Ok(0) => break,
                        Ok(_) => {
                            if header_line.trim().is_empty() {
                                break; // End of headers
                            }
                        }
                        Err(_) => break,
                    }
                }

                // Read SSE data lines
                let mut line = String::new();
                loop {
                    line.clear();
                    match reader.read_line(&mut line) {
                        Ok(0) => break, // EOF — server closed connection
                        Ok(_) => {
                            let trimmed = line.trim();
                            if let Some(data) = trimmed.strip_prefix("data: ") {
                                println!("{data}");
                            }
                        }
                        Err(_) => break,
                    }
                }

                // Reconnect after the long-poll completes
            }
            Err(e) => {
                eprintln!("Cannot connect to daemon: {e}");
                std::thread::sleep(std::time::Duration::from_secs(2));
            }
        }
    }
}

/// Make a simple HTTP request to the daemon over the Unix socket.
fn query_endpoint(path: &str) -> Result<String, String> {
    let sock = socket_path();
    let mut stream = UnixStream::connect(&sock).map_err(|e| format!("connect: {e}"))?;

    let request = format!("GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n");
    stream
        .write_all(request.as_bytes())
        .map_err(|e| format!("write: {e}"))?;

    let mut response = String::new();
    stream
        .read_to_string(&mut response)
        .map_err(|e| format!("read: {e}"))?;

    // Extract body from HTTP response (after \r\n\r\n)
    if let Some(pos) = response.find("\r\n\r\n") {
        Ok(response[pos + 4..].to_string())
    } else {
        Ok(response)
    }
}
