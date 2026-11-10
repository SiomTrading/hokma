//! Cache cleanup per engine version (SPEC-001 addendum 0.0.13, section 11.2).
//!
//! The panel is served by the engine: `index.html` with `no-store`, files under `assets/` with a content hash and
//! `immutable` (core/dashboard_static.py). What was left: HTTP caches written before those headers existed, and a
//! cached page asking for files of another runtime slot (a blank panel). So, per engine version:
//! - before the window is created, when the installed engine version differs from the last one this window saw,
//!   the webview's HTTP caches are deleted from disk (only caches: the panel keeps its saved layout, chart
//!   favorites and language, which live in its local storage);
//! - the first address of every start carries `?v=<version>`, so a page of one version is never served for another.
//! A full `clear_all_browsing_data` is not used: it would also delete that local storage on every update.

use crate::paths;
use std::path::PathBuf;
use tauri::Url;

fn last_version_file() -> PathBuf {
    paths::app_state_dir().join("last-version")
}

pub fn last_seen() -> String {
    std::fs::read_to_string(last_version_file()).unwrap_or_default().trim().to_string()
}

pub fn remember(version: &str) {
    if version.is_empty() || last_seen() == version {
        return;
    }
    let _ = std::fs::create_dir_all(paths::app_state_dir());
    let _ = std::fs::write(last_version_file(), version);
}

/// `__version__ = "X.Y.Z"` in a Python source.
pub fn parse_version(source: &str) -> Option<String> {
    source.lines().find_map(|line| {
        let rest = line.trim().strip_prefix("__version__")?.trim_start().strip_prefix('=')?.trim();
        let value = rest.trim_matches(|c| c == '"' || c == '\'');
        (!value.is_empty() && value.chars().all(|c| c.is_ascii_digit() || c == '.')).then(|| value.to_string())
    })
}

/// The version of the active runtime slot, read from its files (the engine is not running yet).
pub fn installed_version() -> Option<String> {
    let data = paths::data_dir();
    let slot = std::fs::read_to_string(data.join("current")).ok()?.trim().to_string();
    if slot != "a" && slot != "b" {
        return None;
    }
    let init = data.join("runtime").join(slot).join("app").join("backend").join("hokma").join("__init__.py");
    parse_version(&std::fs::read_to_string(init).ok()?)
}

/// Folders that only hold HTTP and script caches of the window's webview.
fn cache_dirs() -> Vec<PathBuf> {
    #[cfg(windows)]
    {
        let profile = paths::webview_data_dir().join("EBWebView").join("Default");
        vec![
            profile.join("Cache"),
            profile.join("Code Cache"),
            profile.join("GPUCache"),
            profile.join("Service Worker").join("CacheStorage"),
            profile.join("Service Worker").join("ScriptCache"),
        ]
    }
    #[cfg(target_os = "macos")]
    {
        vec![paths::home().join("Library").join("Caches").join("com.siomtrading.hokma")]
    }
    #[cfg(all(not(windows), not(target_os = "macos")))]
    {
        Vec::new()
    }
}

/// Before the window exists: delete the webview caches when the engine version changed. Returns true when it did.
pub fn prune_if_version_changed() -> bool {
    let Some(installed) = installed_version() else { return false };
    if last_seen() == installed {
        return false;
    }
    for dir in cache_dirs() {
        let _ = std::fs::remove_dir_all(dir);
    }
    remember(&installed);
    true
}

/// The panel address with `?v=<version>` (the session fragment is kept).
pub fn versioned(url: &Url, version: &str) -> Url {
    let mut out = url.clone();
    if !version.is_empty() {
        out.query_pairs_mut().clear().append_pair("v", version);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_the_engine_version_from_its_source() {
        let source = "\"\"\"Hokma.\"\"\"\n__version__ = \"0.0.15\"\n";
        assert_eq!(parse_version(source).as_deref(), Some("0.0.15"));
        assert_eq!(parse_version("version = 3"), None);
    }

    #[test]
    fn the_first_address_names_the_version_and_keeps_the_session() {
        let url = Url::parse("http://127.0.0.1:8765/#t=abc&o=xyz").unwrap();
        let out = versioned(&url, "0.0.15");
        assert_eq!(out.as_str(), "http://127.0.0.1:8765/?v=0.0.15#t=abc&o=xyz");
        assert_eq!(versioned(&url, "").as_str(), url.as_str());
    }
}
