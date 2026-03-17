use crate::check::{CheckEngine, CheckRequest};
use crate::checkpoint;
use crate::event_log::EventLogWriter;
use crate::otel;
use crate::session::{self, SessionMap};
use crate::sse::{Event, SseBroadcast};
use http_body_util::{BodyExt, Full};
use hyper::body::{Bytes, Incoming};
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper::{Method, Request, Response, StatusCode};
use hyper_util::rt::TokioIo;
use std::convert::Infallible;
use std::path::Path;
use std::sync::Arc;
use tokio::net::UnixListener;
use tracing::{error, info};

/// Shared state accessible from all request handlers.
pub struct ServerState {
    pub sse: SseBroadcast,
    pub event_log: EventLogWriter,
    pub check_engine: CheckEngine,
    pub started_at: chrono::DateTime<chrono::Utc>,
    pub sessions: SessionMap,
    pub config: Arc<tokio::sync::RwLock<crate::config::AmbientConfig>>,
}

/// Run the HTTP server on a Unix domain socket.
pub async fn run(socket_path: &Path, state: Arc<ServerState>) {
    // Remove stale socket file if it exists
    let _ = std::fs::remove_file(socket_path);

    let listener = match UnixListener::bind(socket_path) {
        Ok(l) => l,
        Err(e) => {
            error!("Failed to bind Unix socket {}: {e}", socket_path.display());
            return;
        }
    };

    info!("Listening on {}", socket_path.display());

    // Emit a startup event
    let startup_event = Event::new("daemon_started");
    let _ = state.sse.publish(startup_event.clone());
    state.event_log.log(startup_event);

    loop {
        let (stream, _addr) = match listener.accept().await {
            Ok(conn) => conn,
            Err(e) => {
                error!("Accept error: {e}");
                continue;
            }
        };

        let state = Arc::clone(&state);
        tokio::spawn(async move {
            let io = TokioIo::new(stream);
            if let Err(e) = http1::Builder::new()
                .serve_connection(
                    io,
                    service_fn(move |req| {
                        let state = Arc::clone(&state);
                        async move { handle_request(req, state).await }
                    }),
                )
                .await
            {
                // Connection reset by peer is normal for SSE clients disconnecting
                let msg = e.to_string();
                if !msg.contains("connection reset") && !msg.contains("broken pipe") {
                    error!("Connection error: {e}");
                }
            }
        });
    }
}

async fn handle_request(
    req: Request<Incoming>,
    state: Arc<ServerState>,
) -> Result<Response<Full<Bytes>>, Infallible> {
    let response = match (req.method(), req.uri().path()) {
        (&Method::GET, "/status") => handle_status(&state),
        (&Method::GET, "/inbox") => handle_inbox(&state).await,
        (&Method::POST, "/check") => handle_check(req, &state).await,
        (&Method::POST, "/event") => handle_post_event(req, &state).await,
        (&Method::POST, "/session/start") => handle_session_start(req, &state).await,
        (&Method::POST, "/session/end") => handle_session_end(req, &state).await,
        (&Method::POST, "/checkpoint") => handle_checkpoint(req, &state).await,
        _ => {
            let body = serde_json::json!({"error": "not found"}).to_string();
            Response::builder()
                .status(StatusCode::NOT_FOUND)
                .header("content-type", "application/json")
                .body(Full::new(Bytes::from(body)))
                .unwrap()
        }
    };

    Ok(response)
}

fn handle_status(state: &ServerState) -> Response<Full<Bytes>> {
    let uptime = chrono::Utc::now() - state.started_at;

    // Collect active sessions
    let active_sessions: Vec<serde_json::Value> = state
        .sessions
        .iter()
        .map(|entry| {
            let s = entry.value();
            serde_json::json!({
                "session_id": s.session_id,
                "cwd": s.cwd,
                "repo_root": s.repo_root,
                "duration_s": s.started_at.elapsed().as_secs(),
                "checks": s.check_count,
                "blocks": s.block_count,
                "warns": s.warn_count,
            })
        })
        .collect();

    let ambient_mode = {
        let config = state.config.blocking_read();
        config.ambient_mode.clone()
    };

    let body = serde_json::json!({
        "status": "running",
        "uptime_s": uptime.num_seconds(),
        "started_at": state.started_at.to_rfc3339(),
        "pid": std::process::id(),
        "ambient_mode": ambient_mode,
        "active_sessions": active_sessions.len(),
        "sessions": active_sessions,
        "events_logged": state.event_log.events_logged(),
        "events_dropped": state.event_log.events_dropped(),
    })
    .to_string();

    Response::builder()
        .status(StatusCode::OK)
        .header("content-type", "application/json")
        .body(Full::new(Bytes::from(body)))
        .unwrap()
}

/// SSE endpoint: subscribe to broadcast channel and stream events.
async fn handle_inbox(state: &ServerState) -> Response<Full<Bytes>> {
    let mut rx = state.sse.subscribe();
    let mut body = Vec::new();

    let deadline = tokio::time::Instant::now() + tokio::time::Duration::from_secs(30);

    loop {
        tokio::select! {
            result = rx.recv() => {
                match result {
                    Ok(event) => {
                        body.extend_from_slice(&event.to_sse_bytes());
                    }
                    Err(tokio::sync::broadcast::error::RecvError::Lagged(n)) => {
                        let msg = format!("data: {{\"type\":\"lagged\",\"missed\":{n}}}\n\n");
                        body.extend_from_slice(msg.as_bytes());
                    }
                    Err(tokio::sync::broadcast::error::RecvError::Closed) => break,
                }
            }
            _ = tokio::time::sleep_until(deadline) => break,
        }
    }

    Response::builder()
        .status(StatusCode::OK)
        .header("content-type", "text/event-stream")
        .header("cache-control", "no-cache")
        .body(Full::new(Bytes::from(body)))
        .unwrap()
}

/// POST /check — run Tier 1 checks on a tool call.
async fn handle_check(
    req: Request<Incoming>,
    state: &ServerState,
) -> Response<Full<Bytes>> {
    let body = match req.collect().await {
        Ok(b) => b.to_bytes(),
        Err(e) => {
            let body = serde_json::json!({"error": format!("read body: {e}")}).to_string();
            return Response::builder()
                .status(StatusCode::BAD_REQUEST)
                .header("content-type", "application/json")
                .body(Full::new(Bytes::from(body)))
                .unwrap();
        }
    };

    let request: CheckRequest = match serde_json::from_slice(&body) {
        Ok(r) => r,
        Err(e) => {
            let body = serde_json::json!({"error": format!("parse check request: {e}")}).to_string();
            return Response::builder()
                .status(StatusCode::BAD_REQUEST)
                .header("content-type", "application/json")
                .body(Full::new(Bytes::from(body)))
                .unwrap();
        }
    };

    let result = state.check_engine.check(&request);

    // Track check in session counters
    let action_str = format!("{:?}", result.action).to_lowercase();
    if let Some(ref sid) = request.session_id {
        session::record_check(&state.sessions, sid, &action_str);
    }

    // Emit event for SSE + JSONL
    let event_data = serde_json::json!({
        "tool": request.tool_name,
        "action": result.action,
        "check": result.check_type,
        "reason": result.reason,
        "us": result.duration_us,
    });
    let event = Event::new("check")
        .with_data(event_data)
        .with_session(request.session_id.clone().unwrap_or_default());
    let _ = state.sse.publish(event.clone());
    state.event_log.log(event);

    let body = serde_json::to_string(&result).unwrap();
    Response::builder()
        .status(StatusCode::OK)
        .header("content-type", "application/json")
        .body(Full::new(Bytes::from(body)))
        .unwrap()
}

/// Accept a JSON event via POST and broadcast + log it.
async fn handle_post_event(
    req: Request<Incoming>,
    state: &ServerState,
) -> Response<Full<Bytes>> {
    let body = match req.collect().await {
        Ok(b) => b.to_bytes(),
        Err(e) => {
            let body = serde_json::json!({"error": format!("read body: {e}")}).to_string();
            return Response::builder()
                .status(StatusCode::BAD_REQUEST)
                .header("content-type", "application/json")
                .body(Full::new(Bytes::from(body)))
                .unwrap();
        }
    };

    let data: serde_json::Value = match serde_json::from_slice(&body) {
        Ok(v) => v,
        Err(e) => {
            let body = serde_json::json!({"error": format!("parse json: {e}")}).to_string();
            return Response::builder()
                .status(StatusCode::BAD_REQUEST)
                .header("content-type", "application/json")
                .body(Full::new(Bytes::from(body)))
                .unwrap();
        }
    };

    let event_type = data
        .get("type")
        .and_then(|v| v.as_str())
        .unwrap_or("unknown")
        .to_string();

    // Track check events in session counters
    if event_type == "check" {
        if let Some(sid) = data.get("session_id").and_then(|v| v.as_str()) {
            let action = data.get("action").and_then(|v| v.as_str()).unwrap_or("allow");
            session::record_check(&state.sessions, sid, action);
        }
    }

    let session_id = data
        .get("session_id")
        .and_then(|v| v.as_str())
        .unwrap_or_default()
        .to_string();

    let event = Event::new(&event_type)
        .with_data(data)
        .with_session(session_id);
    let _ = state.sse.publish(event.clone());
    state.event_log.log(event);

    let body = serde_json::json!({"ok": true}).to_string();
    Response::builder()
        .status(StatusCode::OK)
        .header("content-type", "application/json")
        .body(Full::new(Bytes::from(body)))
        .unwrap()
}

/// POST /session/start — register a new session with the daemon.
///
/// Input: `{ "session_id": "...", "cwd": "...", "repo_root": "..." }`
/// Output: `{ "trace_id": "...", "root_span_id": "..." }`
async fn handle_session_start(
    req: Request<Incoming>,
    state: &ServerState,
) -> Response<Full<Bytes>> {
    let body = match req.collect().await {
        Ok(b) => b.to_bytes(),
        Err(e) => {
            let body = serde_json::json!({"error": format!("read body: {e}")}).to_string();
            return Response::builder()
                .status(StatusCode::BAD_REQUEST)
                .header("content-type", "application/json")
                .body(Full::new(Bytes::from(body)))
                .unwrap();
        }
    };

    let data: serde_json::Value = match serde_json::from_slice(&body) {
        Ok(v) => v,
        Err(e) => {
            let body =
                serde_json::json!({"error": format!("parse session start: {e}")}).to_string();
            return Response::builder()
                .status(StatusCode::BAD_REQUEST)
                .header("content-type", "application/json")
                .body(Full::new(Bytes::from(body)))
                .unwrap();
        }
    };

    let session_id = data
        .get("session_id")
        .and_then(|v| v.as_str())
        .unwrap_or("unknown")
        .to_string();
    let cwd = data
        .get("cwd")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let repo_root = data
        .get("repo_root")
        .and_then(|v| v.as_str())
        .map(String::from);

    let (trace_id, root_span_id) =
        session::register(&state.sessions, session_id.clone(), cwd.clone(), repo_root.clone());

    info!(
        "Session registered: {session_id} (cwd: {cwd}, trace: {trace_id})"
    );

    // Emit SSE event
    let event = Event::new("session_start")
        .with_data(serde_json::json!({
            "session_id": session_id,
            "cwd": cwd,
            "repo_root": repo_root,
            "trace_id": trace_id,
        }))
        .with_session(session_id);
    let _ = state.sse.publish(event.clone());
    state.event_log.log(event);

    let body = serde_json::json!({
        "trace_id": trace_id,
        "root_span_id": root_span_id,
    })
    .to_string();

    Response::builder()
        .status(StatusCode::OK)
        .header("content-type", "application/json")
        .body(Full::new(Bytes::from(body)))
        .unwrap()
}

/// POST /session/end — deregister a session, flush OTel spans.
///
/// Input: `{ "session_id": "..." }`
/// Output: `{ "ok": true, "duration_s": N, "checks": N, "spans_exported": N }`
async fn handle_session_end(
    req: Request<Incoming>,
    state: &ServerState,
) -> Response<Full<Bytes>> {
    let body = match req.collect().await {
        Ok(b) => b.to_bytes(),
        Err(e) => {
            let body = serde_json::json!({"error": format!("read body: {e}")}).to_string();
            return Response::builder()
                .status(StatusCode::BAD_REQUEST)
                .header("content-type", "application/json")
                .body(Full::new(Bytes::from(body)))
                .unwrap();
        }
    };

    let data: serde_json::Value = match serde_json::from_slice(&body) {
        Ok(v) => v,
        Err(e) => {
            let body =
                serde_json::json!({"error": format!("parse session end: {e}")}).to_string();
            return Response::builder()
                .status(StatusCode::BAD_REQUEST)
                .header("content-type", "application/json")
                .body(Full::new(Bytes::from(body)))
                .unwrap();
        }
    };

    let session_id = data
        .get("session_id")
        .and_then(|v| v.as_str())
        .unwrap_or("unknown");

    let session_info = session::deregister(&state.sessions, session_id);

    let (duration_s, checks, spans_exported) = match session_info {
        Some(ref info) => {
            let duration = info.started_at.elapsed().as_secs();
            let checks = info.check_count;

            info!(
                "Session ended: {} (duration: {}s, checks: {}, blocks: {}, warns: {})",
                session_id, duration, checks, info.block_count, info.warn_count
            );

            // Read outcome from handoff YAML if available
            let outcome = crate::continuity::read_handoff(session_id)
                .and_then(|h| h.outcome);

            // Build and export OTel spans
            let spans = otel::build_session_spans(info, outcome.as_deref());
            let span_count = spans.len();
            let config = state.config.read().await;
            if let Err(e) = otel::export_spans(&config.observability, spans) {
                tracing::error!("OTel span export failed for session {session_id}: {e}");
                let err_event = Event::new("otel_export_error")
                    .with_data(serde_json::json!({
                        "session_id": session_id,
                        "error": e,
                    }))
                    .with_session(session_id.to_string());
                state.event_log.log(err_event);
            }

            (duration, checks, span_count)
        }
        None => {
            info!("Session end for unknown session: {session_id}");
            (0, 0, 0)
        }
    };

    // Emit SSE event
    let event = Event::new("session_end")
        .with_data(serde_json::json!({
            "session_id": session_id,
            "duration_s": duration_s,
            "checks": checks,
        }))
        .with_session(session_id.to_string());
    let _ = state.sse.publish(event.clone());
    state.event_log.log(event);

    let body = serde_json::json!({
        "ok": true,
        "duration_s": duration_s,
        "checks": checks,
        "spans_exported": spans_exported,
    })
    .to_string();

    Response::builder()
        .status(StatusCode::OK)
        .header("content-type", "application/json")
        .body(Full::new(Bytes::from(body)))
        .unwrap()
}

/// POST /checkpoint — run Tier 2 gate suite before a commit.
///
/// Input: `{ "cwd": "...", "session_id": "..." }`
/// Output: `{ "passed": bool, "gates_run": N, "trailer": "...", ... }`
async fn handle_checkpoint(
    req: Request<Incoming>,
    state: &ServerState,
) -> Response<Full<Bytes>> {
    let body = match req.collect().await {
        Ok(b) => b.to_bytes(),
        Err(e) => {
            let body = serde_json::json!({"error": format!("read body: {e}")}).to_string();
            return Response::builder()
                .status(StatusCode::BAD_REQUEST)
                .header("content-type", "application/json")
                .body(Full::new(Bytes::from(body)))
                .unwrap();
        }
    };

    let request: checkpoint::CheckpointRequest = match serde_json::from_slice(&body) {
        Ok(r) => r,
        Err(e) => {
            let body =
                serde_json::json!({"error": format!("parse checkpoint request: {e}")}).to_string();
            return Response::builder()
                .status(StatusCode::BAD_REQUEST)
                .header("content-type", "application/json")
                .body(Full::new(Bytes::from(body)))
                .unwrap();
        }
    };

    let cwd = &request.cwd;

    // Load merged config for this repo to get checkpoint settings
    let merged = crate::config::load_merged(Some(cwd));

    info!(
        "Checkpoint triggered for {cwd} (gates: {:?})",
        merged.checkpoint.gates
    );

    // Run checkpoint in a blocking task with overall timeout
    let checkpoint_config = merged.checkpoint.clone();
    let timeout_s = checkpoint_config.gate_timeout_s;
    let cwd_owned = cwd.to_string();
    let result = tokio::time::timeout(
        tokio::time::Duration::from_secs(timeout_s),
        tokio::task::spawn_blocking(move || {
            checkpoint::run_checkpoint(&cwd_owned, &checkpoint_config)
        }),
    )
    .await;

    let result = match result {
        Ok(Ok(r)) => r,
        Ok(Err(e)) => checkpoint::CheckpointResult {
            passed: true, // fail-open on panic
            gates_run: 0,
            gates_passed: 0,
            gates_failed: 0,
            first_failure: Some(format!("checkpoint task panicked: {e}")),
            trailer: None,
            gate_results: vec![],
            duration_ms: 0,
        },
        Err(_) => checkpoint::CheckpointResult {
            passed: false, // timeout = fail (don't pass uncommitted code)
            gates_run: 0,
            gates_passed: 0,
            gates_failed: 1,
            first_failure: Some(format!("checkpoint timed out after {timeout_s}s")),
            trailer: None,
            gate_results: vec![],
            duration_ms: timeout_s * 1000,
        },
    };

    info!(
        "Checkpoint result: passed={}, gates={}/{}, duration={}ms",
        result.passed, result.gates_passed, result.gates_run, result.duration_ms
    );

    // Emit SSE event
    let session_id = request.session_id.clone().unwrap_or_default();
    let event = Event::new("checkpoint")
        .with_data(serde_json::json!({
            "cwd": cwd,
            "passed": result.passed,
            "gates_run": result.gates_run,
            "gates_passed": result.gates_passed,
            "gates_failed": result.gates_failed,
            "first_failure": result.first_failure,
            "duration_ms": result.duration_ms,
        }))
        .with_session(session_id);
    let _ = state.sse.publish(event.clone());
    state.event_log.log(event);

    let body = serde_json::to_string(&result).unwrap();
    Response::builder()
        .status(StatusCode::OK)
        .header("content-type", "application/json")
        .body(Full::new(Bytes::from(body)))
        .unwrap()
}
