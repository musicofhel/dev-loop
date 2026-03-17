use crate::sse::Event;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use tokio::sync::mpsc;
use tracing::{error, info, warn};

/// Handle for sending events to the JSONL log writer.
#[derive(Clone)]
pub struct EventLogWriter {
    tx: mpsc::Sender<Event>,
    events_logged: Arc<AtomicU64>,
    events_dropped: Arc<AtomicU64>,
}

impl EventLogWriter {
    /// Spawn the background JSONL writer task. Returns the send handle.
    pub fn spawn(
        path: &Path,
        channel_capacity: usize,
        max_file_size_mb: u64,
        max_rotated_files: u32,
    ) -> Self {
        let events_logged = Arc::new(AtomicU64::new(0));
        let events_dropped = Arc::new(AtomicU64::new(0));
        let (tx, rx) = mpsc::channel(channel_capacity);
        let path = path.to_path_buf();
        let logged = Arc::clone(&events_logged);
        tokio::spawn(writer_loop(
            rx,
            path,
            logged,
            max_file_size_mb,
            max_rotated_files,
        ));
        Self {
            tx,
            events_logged,
            events_dropped,
        }
    }

    /// Try to log an event. Tracks drops if channel is full.
    pub fn log(&self, event: Event) {
        if self.tx.try_send(event).is_err() {
            let dropped = self.events_dropped.fetch_add(1, Ordering::Relaxed) + 1;
            if dropped % 100 == 0 {
                warn!("Event log dropped {dropped} events total (channel full)");
            }
        }
    }

    /// Number of events successfully written to disk.
    pub fn events_logged(&self) -> u64 {
        self.events_logged.load(Ordering::Relaxed)
    }

    /// Number of events dropped due to channel backpressure.
    pub fn events_dropped(&self) -> u64 {
        self.events_dropped.load(Ordering::Relaxed)
    }
}

/// Build path for rotated log file: events.jsonl.1, events.jsonl.2, etc.
fn rotated_path(base: &Path, n: u32) -> PathBuf {
    let base_str = base.to_string_lossy();
    PathBuf::from(format!("{base_str}.{n}"))
}

/// Rotate log files: current → .1, .1 → .2, ..., delete beyond max_rotated.
fn rotate_log(path: &Path, max_rotated: u32) {
    // Delete the oldest rotated file
    let _ = std::fs::remove_file(rotated_path(path, max_rotated));

    // Shift: N-1 → N, N-2 → N-1, ..., 1 → 2
    for i in (1..max_rotated).rev() {
        let _ = std::fs::rename(rotated_path(path, i), rotated_path(path, i + 1));
    }

    // Current → .1
    let _ = std::fs::rename(path, rotated_path(path, 1));
}

async fn writer_loop(
    mut rx: mpsc::Receiver<Event>,
    path: PathBuf,
    events_logged: Arc<AtomicU64>,
    max_file_size_mb: u64,
    max_rotated_files: u32,
) {
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

    let max_size_bytes = max_file_size_mb * 1024 * 1024;
    let mut writes_since_size_check: u64 = 0;

    while let Some(event) = rx.recv().await {
        if let Ok(json) = serde_json::to_string(&event) {
            if let Err(e) = writeln!(file, "{json}") {
                error!("Failed to write event: {e}");
                continue;
            }
            events_logged.fetch_add(1, Ordering::Relaxed);
            writes_since_size_check += 1;

            // Check file size every 100 writes for rotation
            if max_size_bytes > 0 && writes_since_size_check >= 100 {
                writes_since_size_check = 0;
                if let Ok(metadata) = std::fs::metadata(&path) {
                    if metadata.len() > max_size_bytes {
                        info!(
                            "Event log size {}MB exceeds {}MB, rotating",
                            metadata.len() / (1024 * 1024),
                            max_file_size_mb
                        );
                        let _ = file.flush();
                        drop(file);

                        rotate_log(&path, max_rotated_files);

                        file = match std::fs::OpenOptions::new()
                            .create(true)
                            .append(true)
                            .open(&path)
                        {
                            Ok(f) => f,
                            Err(e) => {
                                error!("Failed to open new event log after rotation: {e}");
                                return;
                            }
                        };
                    }
                }
            }
        }
    }

    info!("Event log writer stopped");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rotate_log_creates_numbered_files() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("events.jsonl");
        std::fs::write(&path, "line1\n").unwrap();

        rotate_log(&path, 3);

        assert!(!path.exists());
        assert!(rotated_path(&path, 1).exists());
        assert_eq!(
            std::fs::read_to_string(rotated_path(&path, 1)).unwrap(),
            "line1\n"
        );
    }

    #[test]
    fn rotate_log_shifts_existing() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("events.jsonl");
        std::fs::write(&path, "current\n").unwrap();
        std::fs::write(rotated_path(&path, 1), "prev1\n").unwrap();

        rotate_log(&path, 3);

        assert!(!path.exists());
        assert_eq!(
            std::fs::read_to_string(rotated_path(&path, 1)).unwrap(),
            "current\n"
        );
        assert_eq!(
            std::fs::read_to_string(rotated_path(&path, 2)).unwrap(),
            "prev1\n"
        );
    }

    #[test]
    fn rotate_log_drops_oldest() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("events.jsonl");
        std::fs::write(&path, "current\n").unwrap();
        std::fs::write(rotated_path(&path, 1), "prev1\n").unwrap();
        std::fs::write(rotated_path(&path, 2), "prev2\n").unwrap();
        std::fs::write(rotated_path(&path, 3), "prev3\n").unwrap();

        rotate_log(&path, 3);

        assert!(!path.exists());
        assert_eq!(
            std::fs::read_to_string(rotated_path(&path, 1)).unwrap(),
            "current\n"
        );
        assert_eq!(
            std::fs::read_to_string(rotated_path(&path, 2)).unwrap(),
            "prev1\n"
        );
        assert_eq!(
            std::fs::read_to_string(rotated_path(&path, 3)).unwrap(),
            "prev2\n"
        );
        // prev3 should be gone (beyond max_rotated)
    }

    #[tokio::test]
    async fn event_log_tracks_counts() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test_events.jsonl");
        let writer = EventLogWriter::spawn(&path, 100, 50, 3);

        writer.log(Event::new("test1"));
        writer.log(Event::new("test2"));

        // Give writer task time to process
        tokio::time::sleep(tokio::time::Duration::from_millis(100)).await;

        assert_eq!(writer.events_logged(), 2);
        assert_eq!(writer.events_dropped(), 0);
    }

    #[tokio::test]
    async fn event_log_tracks_drops() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test_events.jsonl");
        // Channel capacity of 1
        let writer = EventLogWriter::spawn(&path, 1, 50, 3);

        // Flood the channel — some will be dropped
        for i in 0..100 {
            writer.log(Event::new(&format!("flood_{i}")));
        }

        tokio::time::sleep(tokio::time::Duration::from_millis(200)).await;

        let logged = writer.events_logged();
        let dropped = writer.events_dropped();
        assert!(logged > 0, "should have logged some events");
        assert!(dropped > 0, "should have dropped some events with capacity=1");
        assert_eq!(logged + dropped, 100, "logged + dropped should equal total sent");
    }
}
