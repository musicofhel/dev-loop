/// Lightweight OTLP/HTTP JSON span exporter.
///
/// Constructs OpenTelemetry-compatible JSON and POSTs to OpenObserve's
/// `/api/{org}/v1/traces` endpoint. No heavy SDK — just serde_json + TcpStream.
use crate::config::ObservabilityConfig;
use crate::session::SessionInfo;
use base64::Engine;
use serde_json::json;
use std::io::Write;
use std::net::TcpStream;
use std::time::{SystemTime, UNIX_EPOCH};

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

/// Build the session root span + summary span for a completed session.
pub fn build_session_spans(session: &SessionInfo, outcome: Option<&str>) -> Vec<serde_json::Value> {
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
    ];
    if let Some(outcome) = outcome {
        attributes.push(attr_str("session.outcome", outcome));
    }

    let root_span = json!({
        "traceId": session.trace_id,
        "spanId": session.root_span_id,
        "name": "ambient.session",
        "kind": 1,
        "startTimeUnixNano": start_ns.to_string(),
        "endTimeUnixNano": end_ns.to_string(),
        "attributes": attributes,
        "status": { "code": 1 }
    });

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
        "status": { "code": 1 }
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
) -> serde_json::Value {
    let now = now_nanos();

    json!({
        "traceId": trace_id,
        "spanId": span_id(),
        "parentSpanId": parent_span_id,
        "name": "ambient.check",
        "kind": 1,
        "startTimeUnixNano": (now - duration_us * 1000).to_string(),
        "endTimeUnixNano": now.to_string(),
        "attributes": [
            attr_str("check.tool", tool_name),
            attr_str("check.action", action),
            attr_str("check.type", check_type),
            attr_int("check.duration_us", duration_us as i64),
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
/// Fire-and-forget: logs errors but never panics.
pub fn export_spans(config: &ObservabilityConfig, spans: Vec<serde_json::Value>) {
    if spans.is_empty() {
        return;
    }

    let body = build_otlp_json(&config.service_name, spans);
    let body_str = match serde_json::to_string(&body) {
        Ok(s) => s,
        Err(e) => {
            tracing::warn!("Failed to serialize OTLP JSON: {e}");
            return;
        }
    };

    let url = format!(
        "{}/api/{}/v1/traces",
        config.openobserve_url, config.openobserve_org
    );

    if let Err(e) = post_json(&url, &config.openobserve_user, &config.openobserve_password, &body_str) {
        tracing::warn!("Failed to export spans to OpenObserve: {e}");
    }
}

/// POST JSON to an HTTP URL. Supports Basic auth if user is non-empty.
fn post_json(url: &str, user: &str, password: &str, body: &str) -> Result<(), String> {
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

    let mut stream = TcpStream::connect(host_port)
        .map_err(|e| format!("connect to {host_port}: {e}"))?;
    stream
        .set_write_timeout(Some(std::time::Duration::from_secs(5)))
        .map_err(|e| format!("set timeout: {e}"))?;

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

    Ok(())
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
        };

        let spans = build_session_spans(&session, None);
        assert_eq!(spans.len(), 2);

        // Root span
        assert_eq!(spans[0]["name"], "ambient.session");
        assert_eq!(spans[0]["traceId"], "a".repeat(32));
        assert_eq!(spans[0]["spanId"], "b".repeat(16));
        assert!(spans[0].get("parentSpanId").is_none());

        // Summary span
        assert_eq!(spans[1]["name"], "ambient.session.summary");
        assert_eq!(spans[1]["traceId"], "a".repeat(32));
        assert_eq!(spans[1]["parentSpanId"], "b".repeat(16));

        // No outcome attribute
        let attrs: Vec<_> = spans[0]["attributes"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|a| a["key"] == "session.outcome")
            .collect();
        assert!(attrs.is_empty());

        // With outcome
        let spans = build_session_spans(&session, Some("success"));
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
    fn check_span_has_parent() {
        let span = build_check_span(
            &"a".repeat(32),
            &"b".repeat(16),
            "Write",
            "block",
            "deny_list",
            150,
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
}
