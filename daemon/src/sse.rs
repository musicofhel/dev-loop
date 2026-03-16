use serde::Serialize;
use tokio::sync::broadcast;

/// SSE broadcast capacity — subscribers get the last N events on connect.
const BROADCAST_CAPACITY: usize = 100;

/// An event that can be broadcast via SSE and logged to JSONL.
#[derive(Clone, Debug, Serialize)]
pub struct Event {
    pub ts: String,
    #[serde(rename = "type")]
    pub event_type: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub session: Option<String>,
    #[serde(flatten)]
    pub data: serde_json::Value,
}

impl Event {
    pub fn new(event_type: &str) -> Self {
        Self {
            ts: chrono::Utc::now().format("%H:%M:%S").to_string(),
            event_type: event_type.to_string(),
            session: None,
            data: serde_json::Value::Null,
        }
    }

    pub fn with_data(mut self, mut data: serde_json::Value) -> Self {
        // Strip "type" from flattened data to avoid duplicate key with event_type
        if let serde_json::Value::Object(ref mut map) = data {
            map.remove("type");
        }
        self.data = data;
        self
    }

    pub fn with_session(mut self, session: String) -> Self {
        self.session = Some(session);
        self
    }

    /// Serialize to SSE wire format: `data: {...}\n\n`
    pub fn to_sse_bytes(&self) -> Vec<u8> {
        let json = serde_json::to_string(self).unwrap_or_default();
        format!("data: {json}\n\n").into_bytes()
    }
}

/// Manages the broadcast channel for SSE fan-out.
#[derive(Clone)]
pub struct SseBroadcast {
    tx: broadcast::Sender<Event>,
}

impl SseBroadcast {
    pub fn new() -> Self {
        let (tx, _) = broadcast::channel(BROADCAST_CAPACITY);
        Self { tx }
    }

    /// Publish an event. Returns Ok(subscriber_count) or Err if no subscribers.
    pub fn publish(&self, event: Event) -> Result<usize, broadcast::error::SendError<Event>> {
        self.tx.send(event)
    }

    /// Get a new subscriber receiver.
    pub fn subscribe(&self) -> broadcast::Receiver<Event> {
        self.tx.subscribe()
    }
}
