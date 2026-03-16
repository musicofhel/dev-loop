use crate::sse::Event;
use std::io::Write;
use std::path::Path;
use tokio::sync::mpsc;
use tracing::{error, info};

/// Bounded channel capacity — events silently dropped if writer can't keep up.
const LOG_CHANNEL_CAPACITY: usize = 1000;

/// Handle for sending events to the JSONL log writer.
#[derive(Clone)]
pub struct EventLogWriter {
    tx: mpsc::Sender<Event>,
}

impl EventLogWriter {
    /// Spawn the background JSONL writer task. Returns the send handle.
    pub fn spawn(path: &Path) -> Self {
        let (tx, rx) = mpsc::channel(LOG_CHANNEL_CAPACITY);
        let path = path.to_path_buf();
        tokio::spawn(writer_loop(rx, path));
        Self { tx }
    }

    /// Try to log an event. Silently drops if the channel is full (backpressure).
    pub fn log(&self, event: Event) {
        // try_send: non-blocking, drops on full buffer — correct for telemetry
        let _ = self.tx.try_send(event);
    }
}

async fn writer_loop(mut rx: mpsc::Receiver<Event>, path: std::path::PathBuf) {
    // Ensure parent directory exists
    if let Some(parent) = path.parent() {
        if let Err(e) = std::fs::create_dir_all(parent) {
            error!("Failed to create log directory {}: {e}", parent.display());
            return;
        }
    }

    let mut file = match std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
    {
        Ok(f) => f,
        Err(e) => {
            error!("Failed to open event log {}: {e}", path.display());
            return;
        }
    };

    info!("Event log writer started: {}", path.display());

    while let Some(event) = rx.recv().await {
        if let Ok(json) = serde_json::to_string(&event) {
            if let Err(e) = writeln!(file, "{json}") {
                error!("Failed to write event: {e}");
            }
        }
    }

    info!("Event log writer stopped");
}
