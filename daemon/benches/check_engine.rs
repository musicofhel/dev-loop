use criterion::{black_box, criterion_group, criterion_main, Criterion};
use dl::check::{CheckEngine, CheckPhase, CheckRequest};

fn bench_deny_list_allow(c: &mut Criterion) {
    let engine = CheckEngine::new();
    let request = CheckRequest {
        tool_name: "Write".to_string(),
        tool_input: serde_json::json!({"file_path": "src/main.rs"}),
        phase: CheckPhase::Pre,
        session_id: None,
    };

    c.bench_function("deny_list_allow", |b| {
        b.iter(|| engine.check(black_box(&request)))
    });
}

fn bench_deny_list_block(c: &mut Criterion) {
    let engine = CheckEngine::new();
    let request = CheckRequest {
        tool_name: "Write".to_string(),
        tool_input: serde_json::json!({"file_path": "/home/user/repo/.env"}),
        phase: CheckPhase::Pre,
        session_id: None,
    };

    c.bench_function("deny_list_block", |b| {
        b.iter(|| engine.check(black_box(&request)))
    });
}

fn bench_dangerous_ops_allow(c: &mut Criterion) {
    let engine = CheckEngine::new();
    let request = CheckRequest {
        tool_name: "Bash".to_string(),
        tool_input: serde_json::json!({"command": "cargo test"}),
        phase: CheckPhase::Pre,
        session_id: None,
    };

    c.bench_function("dangerous_ops_allow", |b| {
        b.iter(|| engine.check(black_box(&request)))
    });
}

fn bench_dangerous_ops_block(c: &mut Criterion) {
    let engine = CheckEngine::new();
    let request = CheckRequest {
        tool_name: "Bash".to_string(),
        tool_input: serde_json::json!({"command": "rm -rf /"}),
        phase: CheckPhase::Pre,
        session_id: None,
    };

    c.bench_function("dangerous_ops_block", |b| {
        b.iter(|| engine.check(black_box(&request)))
    });
}

fn bench_secret_scan_clean(c: &mut Criterion) {
    let engine = CheckEngine::new();
    let request = CheckRequest {
        tool_name: "Write".to_string(),
        tool_input: serde_json::json!({
            "file_path": "src/config.rs",
            "content": "fn main() {\n    let x = 42;\n    println!(\"hello world\");\n}\n"
        }),
        phase: CheckPhase::Post,
        session_id: None,
    };

    c.bench_function("secret_scan_clean", |b| {
        b.iter(|| engine.check(black_box(&request)))
    });
}

fn bench_secret_scan_with_secret(c: &mut Criterion) {
    let engine = CheckEngine::new();
    let request = CheckRequest {
        tool_name: "Write".to_string(),
        tool_input: serde_json::json!({
            "file_path": "src/config.rs",
            "content": "let api_key = \"sk-1234567890abcdef1234567890abcdef\";\n"
        }),
        phase: CheckPhase::Post,
        session_id: None,
    };

    c.bench_function("secret_scan_with_secret", |b| {
        b.iter(|| engine.check(black_box(&request)))
    });
}

criterion_group!(
    benches,
    bench_deny_list_allow,
    bench_deny_list_block,
    bench_dangerous_ops_allow,
    bench_dangerous_ops_block,
    bench_secret_scan_clean,
    bench_secret_scan_with_secret,
);
criterion_main!(benches);
