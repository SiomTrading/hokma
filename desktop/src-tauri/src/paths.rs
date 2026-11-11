//! Where Hokma lives on this computer. The same rules as `hokma.core.config` (backend) and the installers.

use std::path::PathBuf;

pub fn home() -> PathBuf {
    std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" })
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

#[cfg(windows)]
fn local_app_data() -> PathBuf {
    std::env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(|| home().join("AppData").join("Local"))
}

/// The `hokma` command the installers put on the PATH (`~/.local/bin/hokma[.cmd]`).
pub fn launcher() -> PathBuf {
    let bin = home().join(".local").join("bin");
    if cfg!(windows) { bin.join("hokma.cmd") } else { bin.join("hokma") }
}

/// The value of `set "NAME=..."` (Windows) or `NAME=...` / `export NAME=...` (POSIX) in the launcher, if any.
fn launcher_value(name: &str) -> Option<String> {
    let text = std::fs::read_to_string(launcher()).ok()?;
    for line in text.lines() {
        let line = line.trim();
        let rest = line
            .strip_prefix(&format!("set \"{name}="))
            .and_then(|r| r.strip_suffix('"'))
            .or_else(|| line.strip_prefix(&format!("export {name}=")))
            .or_else(|| line.strip_prefix(&format!("{name}=")));
        if let Some(value) = rest {
            let value = value.trim_matches(|c| c == '"' || c == '\'');
            if !value.is_empty() {
                return Some(value.to_string());
            }
        }
    }
    None
}

/// The engine's data folder: runtime slots, managed Python, logs, engine.lock (outside the working folder).
pub fn data_dir() -> PathBuf {
    if let Some(value) = launcher_value("HOKMA_DATA_DIR") {
        return PathBuf::from(value);
    }
    if let Some(value) = std::env::var_os("HOKMA_DATA_DIR") {
        return PathBuf::from(value);
    }
    #[cfg(windows)]
    {
        local_app_data().join("Hokma")
    }
    #[cfg(target_os = "macos")]
    {
        home().join("Library").join("Application Support").join("Hokma")
    }
    #[cfg(all(not(windows), not(target_os = "macos")))]
    {
        std::env::var_os("XDG_DATA_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| home().join(".local").join("share"))
            .join("hokma")
    }
}

/// The owner chose "Always open Hokma in the browser" (marker file of the engine's data folder).
pub fn browser_preferred() -> bool {
    data_dir().join("open-in-browser").is_file()
}

/// True when an installed engine is there: the launcher and an active runtime slot.
pub fn engine_installed() -> bool {
    launcher().is_file() && data_dir().join("current").is_file()
}

/// The window's own state (last engine version seen), never inside the engine's data folder.
pub fn app_state_dir() -> PathBuf {
    #[cfg(windows)]
    {
        local_app_data().join("Hokma-App")
    }
    #[cfg(target_os = "macos")]
    {
        home().join("Library").join("Application Support").join("Hokma-App")
    }
    #[cfg(all(not(windows), not(target_os = "macos")))]
    {
        home().join(".local").join("share").join("hokma-app")
    }
}

/// WebView2 cache and storage, separated from the student's browser (Windows).
#[cfg(windows)]
pub fn webview_data_dir() -> PathBuf {
    app_state_dir().join("WebView2")
}

/// PATH for the processes the window starts. Apps opened from Finder get a minimal PATH on macOS, so the usual
/// places of git, claude and Homebrew are added; Windows keeps its own PATH.
pub fn child_path() -> Option<std::ffi::OsString> {
    if cfg!(windows) {
        return None;
    }
    let mut parts: Vec<PathBuf> = vec![
        home().join(".local").join("bin"),
        PathBuf::from("/opt/homebrew/bin"),
        PathBuf::from("/usr/local/bin"),
    ];
    if let Some(current) = std::env::var_os("PATH") {
        parts.extend(std::env::split_paths(&current));
    }
    for extra in ["/usr/bin", "/bin", "/usr/sbin", "/sbin"] {
        parts.push(PathBuf::from(extra));
    }
    std::env::join_paths(parts).ok()
}
