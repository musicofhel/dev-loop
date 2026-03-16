/// `dl dashboard-validate` — validate dashboard panel SQL queries against OpenObserve.
///
/// Reads `config/dashboards/*.json` files, extracts SQL queries from each panel,
/// runs them against OpenObserve's search API, and reports results.
use serde::Deserialize;
use std::io::Read;
use std::path::PathBuf;

const DASHBOARDS_DIR: &str = "config/dashboards";

#[derive(Debug, Deserialize)]
struct DashboardConfig {
    name: String,
    panels: Vec<PanelConfig>,
}

#[derive(Debug, Deserialize)]
struct PanelConfig {
    title: String,
    query: String,
}

/// Validate all dashboard panel queries.
pub fn validate(repo_root: Option<&str>) {
    let base = match repo_root {
        Some(root) => PathBuf::from(root),
        None => std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")),
    };

    let dashboards_dir = base.join(DASHBOARDS_DIR);

    if !dashboards_dir.exists() {
        println!("No dashboards directory found at {}", dashboards_dir.display());
        println!("Create dashboard configs in {DASHBOARDS_DIR}/*.json to validate.");
        return;
    }

    let entries: Vec<PathBuf> = match std::fs::read_dir(&dashboards_dir) {
        Ok(e) => e
            .filter_map(|e| e.ok())
            .map(|e| e.path())
            .filter(|p| p.extension().and_then(|x| x.to_str()) == Some("json"))
            .collect(),
        Err(e) => {
            eprintln!("Failed to read {}: {e}", dashboards_dir.display());
            std::process::exit(1);
        }
    };

    if entries.is_empty() {
        println!("No dashboard JSON files found in {}", dashboards_dir.display());
        return;
    }

    // Load OpenObserve config
    let config = crate::config::load();
    let oo_url = &config.observability.openobserve_url;
    let oo_org = &config.observability.openobserve_org;
    let oo_user = &config.observability.openobserve_user;
    let oo_pass = &config.observability.openobserve_password;

    if oo_user.is_empty() || oo_pass.is_empty() {
        eprintln!("OpenObserve credentials not configured.");
        eprintln!("Set openobserve_user and openobserve_password in ~/.config/dev-loop/ambient.yaml");
        std::process::exit(1);
    }

    let mut total_panels = 0;
    let mut valid_panels = 0;
    let mut error_panels = 0;
    let mut empty_panels = 0;

    for path in &entries {
        let content = match std::fs::read_to_string(path) {
            Ok(c) => c,
            Err(e) => {
                eprintln!("  Failed to read {}: {e}", path.display());
                continue;
            }
        };

        let dashboard: DashboardConfig = match serde_json::from_str(&content) {
            Ok(d) => d,
            Err(e) => {
                eprintln!(
                    "  Failed to parse {}: {e}",
                    path.file_name().unwrap_or_default().to_string_lossy()
                );
                continue;
            }
        };

        println!(
            "\n{} ({})",
            dashboard.name,
            path.file_name().unwrap_or_default().to_string_lossy()
        );

        for (i, panel) in dashboard.panels.iter().enumerate() {
            total_panels += 1;
            let result = run_query(oo_url, oo_org, oo_user, oo_pass, &panel.query);

            match result {
                QueryResult::Ok(rows) => {
                    valid_panels += 1;
                    println!("  Panel {} \"{}\": \u{2705} {} rows", i + 1, panel.title, rows);
                }
                QueryResult::Empty => {
                    empty_panels += 1;
                    println!("  Panel {} \"{}\": \u{26a0}\u{fe0f}  0 rows (empty)", i + 1, panel.title);
                }
                QueryResult::Error(e) => {
                    error_panels += 1;
                    println!("  Panel {} \"{}\": \u{274c} ERROR: {}", i + 1, panel.title, e);
                }
            }
        }
    }

    println!(
        "\nSummary: {valid_panels}/{total_panels} panels valid, {error_panels} errors, {empty_panels} empty"
    );
}

enum QueryResult {
    Ok(usize),
    Empty,
    Error(String),
}

/// Run a SQL query against OpenObserve's search API.
fn run_query(base_url: &str, org: &str, user: &str, pass: &str, sql: &str) -> QueryResult {
    let url = format!("{base_url}/api/{org}/_search");

    let body = serde_json::json!({
        "query": {
            "sql": sql,
            "start_time": 0,
            "end_time": chrono::Utc::now().timestamp_micros(),
            "size": 100
        }
    })
    .to_string();

    // Use raw TCP for HTTP to avoid adding reqwest dependency
    let parsed = match parse_url(&url) {
        Some(p) => p,
        None => return QueryResult::Error(format!("invalid URL: {url}")),
    };

    let auth = base64::Engine::encode(&base64::engine::general_purpose::STANDARD, format!("{user}:{pass}"));

    let request = format!(
        "POST {} HTTP/1.1\r\n\
         Host: {}\r\n\
         Content-Type: application/json\r\n\
         Content-Length: {}\r\n\
         Authorization: Basic {}\r\n\
         Connection: close\r\n\
         \r\n\
         {body}",
        parsed.path,
        parsed.host,
        body.len(),
        auth
    );

    let addr = format!("{}:{}", parsed.host, parsed.port);
    let mut stream = match std::net::TcpStream::connect_timeout(
        &addr.parse().unwrap_or_else(|_| {
            std::net::SocketAddr::from(([127, 0, 0, 1], parsed.port))
        }),
        std::time::Duration::from_secs(5),
    ) {
        Ok(s) => s,
        Err(e) => return QueryResult::Error(format!("connect: {e}")),
    };

    let _ = stream.set_read_timeout(Some(std::time::Duration::from_secs(10)));

    if std::io::Write::write_all(&mut stream, request.as_bytes()).is_err() {
        return QueryResult::Error("write failed".to_string());
    }

    let mut response = String::new();
    if stream.read_to_string(&mut response).is_err() {
        return QueryResult::Error("read failed".to_string());
    }

    // Extract JSON body
    let json_body = match response.find("\r\n\r\n") {
        Some(pos) => &response[pos + 4..],
        None => return QueryResult::Error("no response body".to_string()),
    };

    let data: serde_json::Value = match serde_json::from_str(json_body) {
        Ok(v) => v,
        Err(e) => return QueryResult::Error(format!("parse response: {e}")),
    };

    // Check for error
    if let Some(error) = data.get("error").and_then(|v| v.as_str()) {
        return QueryResult::Error(error.to_string());
    }

    // Count result rows
    let hits = data
        .get("hits")
        .and_then(|v| v.as_array())
        .map(|a| a.len())
        .or_else(|| data.get("total").and_then(|v| v.as_u64()).map(|n| n as usize))
        .unwrap_or(0);

    if hits > 0 {
        QueryResult::Ok(hits)
    } else {
        QueryResult::Empty
    }
}

struct ParsedUrl {
    host: String,
    port: u16,
    path: String,
}

fn parse_url(url: &str) -> Option<ParsedUrl> {
    let without_scheme = url.strip_prefix("http://").or(url.strip_prefix("https://"))?;
    let (host_port, path) = match without_scheme.find('/') {
        Some(pos) => (&without_scheme[..pos], &without_scheme[pos..]),
        None => (without_scheme, "/"),
    };

    let (host, port) = match host_port.find(':') {
        Some(pos) => {
            let h = &host_port[..pos];
            let p: u16 = host_port[pos + 1..].parse().unwrap_or(80);
            (h.to_string(), p)
        }
        None => (host_port.to_string(), 80),
    };

    Some(ParsedUrl {
        host,
        port,
        path: path.to_string(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_url_basic() {
        let parsed = parse_url("http://localhost:5080/api/default/_search").unwrap();
        assert_eq!(parsed.host, "localhost");
        assert_eq!(parsed.port, 5080);
        assert_eq!(parsed.path, "/api/default/_search");
    }

    #[test]
    fn parse_url_no_port() {
        let parsed = parse_url("http://example.com/path").unwrap();
        assert_eq!(parsed.host, "example.com");
        assert_eq!(parsed.port, 80);
        assert_eq!(parsed.path, "/path");
    }

    #[test]
    fn dashboard_config_parse() {
        let json = r#"{
            "name": "Test Dashboard",
            "panels": [
                { "title": "Panel 1", "query": "SELECT COUNT(*) FROM default" },
                { "title": "Panel 2", "query": "SELECT * FROM default LIMIT 10" }
            ]
        }"#;
        let config: DashboardConfig = serde_json::from_str(json).unwrap();
        assert_eq!(config.name, "Test Dashboard");
        assert_eq!(config.panels.len(), 2);
        assert_eq!(config.panels[0].title, "Panel 1");
    }
}
