//! Texts of the window (start screen, dialogs, menu) in Spanish and English: desktop/messages/<lang>.json.

use serde_json::{Map, Value};
use std::sync::OnceLock;

const ES: &str = include_str!("../../messages/es.json");
const EN: &str = include_str!("../../messages/en.json");

pub struct Texts {
    pub lang: &'static str,
    pub map: Map<String, Value>,
}

/// The engine's language (`<data>/language`, written by the installer and the app), else the system's, else es.
fn detect() -> &'static str {
    if let Ok(saved) = std::fs::read_to_string(crate::paths::data_dir().join("language")) {
        match saved.trim() {
            "en" => return "en",
            "es" => return "es",
            _ => {}
        }
    }
    for name in ["HOKMA_LANG", "LC_ALL", "LANG"] {
        if let Ok(value) = std::env::var(name) {
            if value.starts_with("en") {
                return "en";
            }
            if value.starts_with("es") {
                return "es";
            }
        }
    }
    "es"
}

pub fn texts() -> &'static Texts {
    static TEXTS: OnceLock<Texts> = OnceLock::new();
    TEXTS.get_or_init(|| {
        let lang = detect();
        let raw = if lang == "en" { EN } else { ES };
        let map = serde_json::from_str::<Map<String, Value>>(raw).unwrap_or_default();
        Texts { lang, map }
    })
}

/// One text, with `{name}` placeholders filled.
pub fn t(key: &str, values: &[(&str, &str)]) -> String {
    let mut text = texts()
        .map
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or(key)
        .to_string();
    for (name, value) in values {
        text = text.replace(&format!("{{{name}}}"), value);
    }
    text
}
