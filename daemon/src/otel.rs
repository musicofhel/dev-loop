/// Lightweight OTLP/HTTP JSON span exporter.
///
/// Constructs OpenTelemetry-compatible JSON and POSTs to OpenObserve's
/// `/api/{org}/v1/traces` endpoint. No heavy SDK — just serde_json + TcpStream.
use crate::config::ObservabilityConfig;
use crate::session::SessionInfo;
use base64::Engine;
use serde_json::json;
use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// Convert a chrono DateTime to nanoseconds since epoch.
fn datetime_to_nanos(dt: &chrono::DateTime<chrono::Utc>) -> u64 {
    dt.timestamp_nanos_opt().unwrap_or(0) as u64
}

/// Current time in nanoseconds since epoch.
fn now_nanos() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos() as u64
}

/// Generate a random span ID (8 bytes → 16 hex chars).
pub fn span_id() -> String {
    let mut bytes = [0u8; 8];
    getrandom::fill(&mut bytes).expect("getrandom failed");
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// Generate a UUID v4 string.
pub fn uuid_v4() -> String {
    let mut bytes = [0u8; 16];
    getrandom::fill(&mut bytes).expect("getrandom");
    bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
    bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant 1
    let h = hex::encode(bytes);
    format!(
        "{}-{}-{}-{}-{}",
        &h[0..8],
        &h[8..12],
        &h[12..16],
        &h[16..20],
        &h[20..32]
    )
}

/// Build the session root span + summary span for a completed session.
/// Build an OTLP status object. Per the OTel spec, the `message` (description)
/// field is only populated when status_code is ERROR (code 2).
fn span_status(is_error: bool, description: Option<&str>) -> serde_json::Value {
    if is_error {
        let mut status = json!({ "code": 2 });
        if let Some(desc) = description {
            status["message"] = json!(desc);
        }
        status
    } else {
        json!({ "code": 1 })
    }
}

pub fn build_session_spans(
    session: &SessionInfo,
    outcome: Option<&str>,
    token_estimate: Option<&crate::continuity::TokenEstimate>,
    ambient_mode: &str,
) -> Vec<serde_json::Value> {
    let start_ns = datetime_to_nanos(&session.started_at_utc);
    let end_ns = now_nanos();
    let duration_s = session.started_at.elapsed().as_secs();
    let summary_span_id = span_id();

    let mut attributes = vec![
        attr_str("session.id", &session.session_id),
        attr_str("session.cwd", &session.cwd),
        attr_str("session.repo", session.repo_root.as_deref().unwrap_or("")),
        attr_int("session.duration_s", duration_s as i64),
        attr_int("session.checks", session.check_count as i64),
        attr_int("session.blocks", session.block_count as i64),
        attr_int("session.warns", session.warn_count as i64),
        // ATSC Core
        attr_str("atsc.spec_version", "0.1.0"),
        attr_str("atsc.span_kind", "agent.invoke"),
        attr_str("atsc.event_id", &uuid_v4()),
        attr_str(
            "atsc.status",
            if session.block_count > 0 { "error" } else { "ok" },
        ),
        attr_str("run.id", &session.session_id),
        attr_str("run.kind", "session"),
        attr_str("timestamp", &session.started_at_utc.to_rfc3339()),
        // ATSC Session Object
        attr_str("session.kind", "interactive"),
        attr_str("session.state", "completed"),
        attr_str("session.participant.channel", "cli"),
        // dev-loop extensions
        attr_str("x.devloop.ambient_mode", ambient_mode),
        attr_str("x.devloop.config_hash", &session.config_hash),
    ];
    if let Some(outcome) = outcome {
        attributes.push(attr_str("session.outcome", outcome));
    }
    if let Some(te) = token_estimate {
        attributes.push(attr_int(
            "session.context.tokens_consumed",
            te.total as i64,
        ));
        attributes.push(attr_str(
            "session.context.pct",
            &format!("{:.1}", te.context_pct * 100.0),
        ));
    }

    let is_error = session.block_count > 0;
    let error_desc = format!("Session had {} block(s)", session.block_count);

    let mut root_span = json!({
        "traceId": session.trace_id,
        "spanId": session.root_span_id,
        "name": "ambient.session",
        "kind": 1,
        "startTimeUnixNano": start_ns.to_string(),
        "endTimeUnixNano": end_ns.to_string(),
        "attributes": attributes,
        "status": span_status(is_error, if is_error { Some(&error_desc) } else { None })
    });

    // Cross-trace link to previous session
    if let Some(ref prev_trace) = session.previous_trace_id {
        if let Some(ref prev_span) = session.previous_span_id {
            root_span["links"] = json!([{
                "traceId": prev_trace,
                "spanId": prev_span,
            }]);
        }
    }

    let summary_span = json!({
        "traceId": session.trace_id,
        "spanId": summary_span_id,
        "parentSpanId": session.root_span_id,
        "name": "ambient.session.summary",
        "kind": 1,
        "startTimeUnixNano": end_ns.to_string(),
        "endTimeUnixNano": end_ns.to_string(),
        "attributes": [
            attr_str("session.id", &session.session_id),
            attr_int("session.duration_s", duration_s as i64),
            attr_int("session.total_checks", session.check_count as i64),
            attr_int("session.blocked", session.block_count as i64),
            attr_int("session.warned", session.warn_count as i64),
            attr_str("session.repo", session.repo_root.as_deref().unwrap_or("")),
        ],
        "status": span_status(session.block_count > 0, None)
    });

    vec![root_span, summary_span]
}

/// Build a check event span (child of session root span).
pub fn build_check_span(
    trace_id: &str,
    parent_span_id: &str,
    tool_name: &str,
    action: &str,
    check_type: &str,
    duration_us: u64,
    pattern: Option<&str>,
    category: &str,
) -> serde_json::Value {
    let now = now_nanos();

    let mut attrs = vec![
        attr_str("check.tool", tool_name),
        attr_str("check.action", action),
        attr_str("check.type", check_type),
        attr_int("check.duration_us", duration_us as i64),
        // ATSC Guardrail
        attr_str("atsc.spec_version", "0.1.0"),
        attr_str("atsc.span_kind", "guardrail.check"),
        attr_str("atsc.event_id", &uuid_v4()),
        attr_str("guardrail.name", check_type),
        attr_str("guardrail.action", action),
        attr_str(
            "guardrail.triggered",
            if action != "allow" { "true" } else { "false" },
        ),
        attr_str("guardrail.categories", category),
    ];
    if let Some(p) = pattern {
        attrs.push(attr_str("guardrail.policy", p));
    }

    json!({
        "traceId": trace_id,
        "spanId": span_id(),
        "parentSpanId": parent_span_id,
        "name": "ambient.check",
        "kind": 1,
        "startTimeUnixNano": (now - duration_us * 1000).to_string(),
        "endTimeUnixNano": now.to_string(),
        "attributes": attrs,
        "status": span_status(
            action == "block",
            if action == "block" { Some("Check blocked tool call") } else { None },
        )
    })
}

/// Build a handoff span (for session continuity).
pub fn build_handoff_span(
    trace_id: &str,
    parent_span_id: &str,
    source: &str,
    token_count: u64,
) -> serde_json::Value {
    let now = now_nanos();
    json!({
        "traceId": trace_id,
        "spanId": span_id(),
        "parentSpanId": parent_span_id,
        "name": "agent.handoff",
        "kind": 1,
        "startTimeUnixNano": now.to_string(),
        "endTimeUnixNano": now.to_string(),
        "attributes": [
            attr_str("atsc.spec_version", "0.1.0"),
            attr_str("atsc.span_kind", "agent.handoff"),
            attr_str("atsc.event_id", &uuid_v4()),
            attr_str("handoff.source", source),
            attr_str("handoff.context_transfer.strategy", "summary"),
            attr_int("handoff.context_transfer.token_count", token_count as i64),
        ],
        "status": { "code": 1 }
    })
}

/// Wrap spans into OTLP JSON envelope.
pub fn build_otlp_json(service_name: &str, spans: Vec<serde_json::Value>) -> serde_json::Value {
    json!({
        "resourceSpans": [{
            "resource": {
                "attributes": [
                    attr_str("service.name", service_name),
                ]
            },
            "scopeSpans": [{
                "scope": { "name": service_name },
                "spans": spans
            }]
        }]
    })
}

/// Export spans to OpenObserve via OTLP/HTTP JSON.
///
/// Retries up to 3 times with exponential backoff (1s, 2s, 4s).
/// Returns Ok(()) on success, Err with details on final failure.
pub fn export_spans(
    config: &ObservabilityConfig,
    spans: Vec<serde_json::Value>,
) -> Result<(), String> {
    if spans.is_empty() {
        return Ok(());
    }

    let body = build_otlp_json(&config.service_name, spans);
    let body_str = serde_json::to_string(&body)
        .map_err(|e| format!("serialize OTLP JSON: {e}"))?;

    let url = format!(
        "{}/api/{}/v1/traces",
        config.openobserve_url, config.openobserve_org
    );

    let backoff_secs = [1u64, 2, 4];
    let mut last_err = String::new();

    for (attempt, delay_s) in backoff_secs.iter().enumerate() {
        match post_json(
            &url,
            &config.openobserve_user,
            &config.openobserve_password,
            &body_str,
        ) {
            Ok(status) => {
                if attempt > 0 {
                    tracing::info!(
                        "OTel export succeeded on attempt {} (HTTP {status})",
                        attempt + 1
                    );
                }
                return Ok(());
            }
            Err(e) => {
                last_err = e;
                tracing::warn!(
                    "OTel export attempt {}/{}: {last_err}",
                    attempt + 1,
                    backoff_secs.len()
                );
                std::thread::sleep(Duration::from_secs(*delay_s));
            }
        }
    }

    Err(format!(
        "OTel export failed after {} attempts: {last_err}",
        backoff_secs.len()
    ))
}

/// POST JSON to an HTTP URL with timeouts and response status checking.
/// Returns the HTTP status code on success (2xx), or an error message.
fn post_json(url: &str, user: &str, password: &str, body: &str) -> Result<u16, String> {
    let url_stripped = url
        .strip_prefix("http://")
        .ok_or_else(|| format!("only http:// supported, got: {url}"))?;

    let (host_port, path) = match url_stripped.split_once('/') {
        Some((hp, p)) => (hp, format!("/{p}")),
        None => (url_stripped, "/".to_string()),
    };

    let auth_header = if !user.is_empty() {
        let creds = format!("{user}:{password}");
        let encoded = base64::engine::general_purpose::STANDARD.encode(creds.as_bytes());
        format!("Authorization: Basic {encoded}\r\n")
    } else {
        String::new()
    };

    // Resolve address and connect with timeout
    let addr = host_port
        .to_socket_addrs()
        .map_err(|e| format!("resolve {host_port}: {e}"))?
        .next()
        .ok_or_else(|| format!("no address for {host_port}"))?;

    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_secs(5))
        .map_err(|e| format!("connect to {host_port}: {e}"))?;

    stream
        .set_write_timeout(Some(Duration::from_secs(5)))
        .map_err(|e| format!("set write timeout: {e}"))?;
    stream
        .set_read_timeout(Some(Duration::from_secs(10)))
        .map_err(|e| format!("set read timeout: {e}"))?;

    let request = format!(
        "POST {path} HTTP/1.1\r\n\
         Host: {host_port}\r\n\
         {auth_header}\
         Content-Type: application/json\r\n\
         Content-Length: {}\r\n\
         Connection: close\r\n\
         \r\n\
         {body}",
        body.len()
    );

    stream
        .write_all(request.as_bytes())
        .map_err(|e| format!("write: {e}"))?;

    // Read response to get HTTP status code
    let mut response_buf = [0u8; 512];
    let n = stream
        .read(&mut response_buf)
        .map_err(|e| format!("read response: {e}"))?;
    let response = String::from_utf8_lossy(&response_buf[..n]);

    // Parse "HTTP/1.1 200 OK" → 200
    let status_code = response
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|code| code.parse::<u16>().ok())
        .unwrap_or(0);

    if (200..300).contains(&status_code) {
        Ok(status_code)
    } else if status_code == 401 {
        Err(format!(
            "401 Unauthorized — check OTel credentials (user: {user})"
        ))
    } else if status_code == 0 {
        Err("no valid HTTP response received".into())
    } else {
        Err(format!("HTTP {status_code} from {url}"))
    }
}

fn attr_str(key: &str, value: &str) -> serde_json::Value {
    json!({ "key": key, "value": { "stringValue": value } })
}

fn attr_int(key: &str, value: i64) -> serde_json::Value {
    json!({ "key": key, "value": { "intValue": value.to_string() } })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn span_id_format() {
        let id = span_id();
        assert_eq!(id.len(), 16);
        assert!(id.chars().all(|c| c.is_ascii_hexdigit()));
    }

    #[test]
    fn otlp_json_structure() {
        let spans = vec![json!({
            "traceId": "a".repeat(32),
            "spanId": "b".repeat(16),
            "name": "test.span",
            "kind": 1,
            "startTimeUnixNano": "1000",
            "endTimeUnixNano": "2000",
            "attributes": [],
            "status": { "code": 1 }
        })];

        let envelope = build_otlp_json("test-service", spans);

        let resource_spans = envelope["resourceSpans"].as_array().unwrap();
        assert_eq!(resource_spans.len(), 1);

        let resource = &resource_spans[0]["resource"];
        let svc = &resource["attributes"][0];
        assert_eq!(svc["key"], "service.name");
        assert_eq!(svc["value"]["stringValue"], "test-service");

        let scope_spans = resource_spans[0]["scopeSpans"].as_array().unwrap();
        assert_eq!(scope_spans.len(), 1);
        assert_eq!(scope_spans[0]["spans"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn session_spans_have_correct_hierarchy() {
        let session = SessionInfo {
            session_id: "test-session".into(),
            cwd: "/home/user/repo".into(),
            repo_root: Some("/home/user/repo".into()),
            started_at: std::time::Instant::now(),
            started_at_utc: chrono::Utc::now(),
            trace_id: "a".repeat(32),
            root_span_id: "b".repeat(16),
            check_count: 10,
            block_count: 2,
            warn_count: 3,
            config_hash: "abc123def456".into(),
            previous_trace_id: None,
            previous_span_id: None,
        };

        let spans = build_session_spans(&session, None, None, "enforce");
        assert_eq!(spans.len(), 2);

        // Root span
        assert_eq!(spans[0]["name"], "ambient.session");
        assert_eq!(spans[0]["traceId"], "a".repeat(32));
        assert_eq!(spans[0]["spanId"], "b".repeat(16));
        assert!(spans[0].get("parentSpanId").is_none());

        // ATSC attributes present
        let root_attrs = spans[0]["attributes"].as_array().unwrap();
        let atsc_version: Vec<_> = root_attrs
            .iter()
            .filter(|a| a["key"] == "atsc.spec_version")
            .collect();
        assert_eq!(atsc_version.len(), 1);
        assert_eq!(atsc_version[0]["value"]["stringValue"], "0.1.0");

        // Summary span
        assert_eq!(spans[1]["name"], "ambient.session.summary");
        assert_eq!(spans[1]["traceId"], "a".repeat(32));
        assert_eq!(spans[1]["parentSpanId"], "b".repeat(16));

        // No outcome attribute
        let attrs: Vec<_> = root_attrs
            .iter()
            .filter(|a| a["key"] == "session.outcome")
            .collect();
        assert!(attrs.is_empty());

        // With outcome
        let spans = build_session_spans(&session, Some("success"), None, "enforce");
        let attrs: Vec<_> = spans[0]["attributes"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|a| a["key"] == "session.outcome")
            .collect();
        assert_eq!(attrs.len(), 1);
        assert_eq!(attrs[0]["value"]["stringValue"], "success");
    }

    #[test]
    fn uuid_v4_format() {
        let id = uuid_v4();
        assert_eq!(id.len(), 36);
        let parts: Vec<&str> = id.split('-').collect();
        assert_eq!(parts.len(), 5);
        assert_eq!(parts[0].len(), 8);
        assert_eq!(parts[1].len(), 4);
        assert_eq!(parts[2].len(), 4);
        assert!(parts[2].starts_with('4')); // version 4
        assert_eq!(parts[3].len(), 4);
        assert_eq!(parts[4].len(), 12);
    }

    #[test]
    fn cross_trace_link_when_previous_set() {
        let session = SessionInfo {
            session_id: "test-session".into(),
            cwd: "/repo".into(),
            repo_root: None,
            started_at: std::time::Instant::now(),
            started_at_utc: chrono::Utc::now(),
            trace_id: "a".repeat(32),
            root_span_id: "b".repeat(16),
            check_count: 0,
            block_count: 0,
            warn_count: 0,
            config_hash: "test".into(),
            previous_trace_id: Some("c".repeat(32)),
            previous_span_id: Some("d".repeat(16)),
        };

        let spans = build_session_spans(&session, None, None, "enforce");
        let links = spans[0]["links"].as_array().unwrap();
        assert_eq!(links.len(), 1);
        assert_eq!(links[0]["traceId"], "c".repeat(32));
        assert_eq!(links[0]["spanId"], "d".repeat(16));
    }

    #[test]
    fn check_span_has_parent() {
        let span = build_check_span(
            &"a".repeat(32),
            &"b".repeat(16),
            "Write",
            "block",
            "deny_list",
            150,
            Some(".env"),
            "file_protection",
        );

        assert_eq!(span["name"], "ambient.check");
        assert_eq!(span["parentSpanId"], "b".repeat(16));
        assert_eq!(span["traceId"], "a".repeat(32));

        let attrs: Vec<_> = span["attributes"]
            .as_array()
            .unwrap()
            .iter()
            .map(|a| a["key"].as_str().unwrap())
            .collect();
        assert!(attrs.contains(&"check.tool"));
        assert!(attrs.contains(&"check.action"));
        assert!(attrs.contains(&"check.type"));
        assert!(attrs.contains(&"check.duration_us"));
        // ATSC guardrail attributes
        assert!(attrs.contains(&"atsc.spec_version"));
        assert!(attrs.contains(&"guardrail.name"));
        assert!(attrs.contains(&"guardrail.action"));
        assert!(attrs.contains(&"guardrail.triggered"));
        assert!(attrs.contains(&"guardrail.categories"));
        assert!(attrs.contains(&"guardrail.policy"));
    }

    #[test]
    fn attr_helpers() {
        let s = attr_str("key1", "value1");
        assert_eq!(s["key"], "key1");
        assert_eq!(s["value"]["stringValue"], "value1");

        let i = attr_int("key2", 42);
        assert_eq!(i["key"], "key2");
        assert_eq!(i["value"]["intValue"], "42");
    }

    #[test]
    fn export_spans_empty_is_ok() {
        let config = ObservabilityConfig::default();
        let result = export_spans(&config, vec![]);
        assert!(result.is_ok());
    }

    #[test]
    fn export_spans_connection_refused() {
        let config = ObservabilityConfig {
            openobserve_url: "http://127.0.0.1:19999".into(), // not listening
            ..Default::default()
        };
        let spans = vec![json!({
            "traceId": "a".repeat(32),
            "spanId": "b".repeat(16),
            "name": "test",
            "kind": 1,
            "startTimeUnixNano": "1000",
            "endTimeUnixNano": "2000",
            "attributes": [],
            "status": { "code": 1 }
        })];
        let result = export_spans(&config, spans);
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert!(
            err.contains("connect") || err.contains("failed"),
            "Error should mention connection: {err}"
        );
    }

    #[test]
    fn post_json_rejects_https() {
        let result = post_json("https://example.com/traces", "", "", "{}");
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("only http://"));
    }
}
