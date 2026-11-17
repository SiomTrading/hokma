//! The few calls the window makes to the local engine, always on 127.0.0.1.

use serde_json::Value;
use std::time::Duration;

pub const FIRST_PORT: u16 = 8765;
pub const LAST_PORT: u16 = 8775;

pub struct Health {
    pub version: String,
    pub busy: bool,
}

fn base(port: u16) -> String {
    format!("http://127.0.0.1:{port}")
}

fn agent() -> ureq::Agent {
    ureq::AgentBuilder::new()
        .timeout_connect(Duration::from_secs(2))
        .timeout(Duration::from_secs(5))
        .build()
}

/// `/health` of the engine on this port, or None when nothing (or something else) answers.
pub fn health(port: u16) -> Option<Health> {
    let body: Value = serde_json::from_str(&agent().get(&format!("{}/health", base(port))).call().ok()?.into_string().ok()?).ok()?;
    if body.get("status").and_then(Value::as_str) != Some("ok") {
        return None;
    }
    Some(Health {
        version: body.get("version").and_then(Value::as_str).unwrap_or("").to_string(),
        busy: body.get("busy").and_then(Value::as_bool).unwrap_or(false),
    })
}

pub struct UpdateState {
    pub newer: bool,
    pub latest: Option<String>,
    pub updating: bool,
}

/// What the engine knows about updates (the same state the panel shows).
pub fn update_state(port: u16, token: &str) -> Option<UpdateState> {
    let response = agent()
        .get(&format!("{}/api/update/state", base(port)))
        .set("X-Hokma-Token", token)
        .call()
        .ok()?;
    let body: Value = serde_json::from_str(&response.into_string().ok()?).ok()?;
    Some(UpdateState {
        newer: body.get("newer").and_then(Value::as_bool).unwrap_or(false),
        latest: body.get("latest_version").and_then(Value::as_str).map(str::to_string),
        updating: body.get("phase").and_then(Value::as_str) == Some("updating"),
    })
}

pub enum UpdateOutcome {
    /// The detached updater of the engine started: the panel shows "Updating Hokma" and reconnects by itself.
    Launched,
    UpToDate(String),
    /// An agent started working after the window asked `/health`: ask again, then repeat with force.
    InUse,
    Failed(String),
}

/// `POST /api/update`; `force=true` is sent only after the owner accepted to cut a working agent.
fn update_url(port: u16, force: bool) -> String {
    format!("{}/api/update{}", base(port), if force { "?force=true" } else { "" })
}

/// The engine refuses with 409 `update.in_use` while an agent works and the call carried no `force`.
fn is_in_use(body: &Value) -> bool {
    body.pointer("/detail/code").and_then(Value::as_str) == Some("update.in_use")
}

/// The same request as the panel's Update button: the engine runs the detached updater (0.0.14), which stops
/// Hokma, updates outside the runtime slots and starts it again with the same session.
pub fn start_update(port: u16, token: &str, force: bool) -> UpdateOutcome {
    let agent = ureq::AgentBuilder::new()
        .timeout_connect(Duration::from_secs(2))
        .timeout(Duration::from_secs(120))
        .build();
    let result = agent
        .post(&update_url(port, force))
        .set("X-Hokma-Token", token)
        .call();
    let read = |response: ureq::Response| -> Value {
        response
            .into_string()
            .ok()
            .and_then(|text| serde_json::from_str(&text).ok())
            .unwrap_or(Value::Null)
    };
    match result {
        Ok(response) => {
            let body = read(response);
            match body.get("status").and_then(Value::as_str) {
                Some("launched") => UpdateOutcome::Launched,
                Some("up-to-date") => UpdateOutcome::UpToDate(
                    body.get("from_version").and_then(Value::as_str).unwrap_or("").to_string(),
                ),
                _ => UpdateOutcome::Failed(body.to_string()),
            }
        }
        Err(ureq::Error::Status(code, response)) => {
            let body = read(response);
            if code == 409 && is_in_use(&body) {
                return UpdateOutcome::InUse;
            }
            UpdateOutcome::Failed(body.get("detail").and_then(Value::as_str).unwrap_or("").to_string())
        }
        Err(error) => UpdateOutcome::Failed(error.to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn force_is_sent_only_when_asked_for() {
        assert_eq!(update_url(8765, false), "http://127.0.0.1:8765/api/update");
        assert_eq!(update_url(8765, true), "http://127.0.0.1:8765/api/update?force=true");
    }

    #[test]
    fn only_the_in_use_code_counts_as_a_working_agent() {
        let busy: Value = serde_json::from_str(r#"{"detail":{"code":"update.in_use","message":"x"}}"#).unwrap();
        let other: Value = serde_json::from_str(r#"{"detail":"a plain string detail"}"#).unwrap();
        assert!(is_in_use(&busy));
        assert!(!is_in_use(&other));
    }
}
