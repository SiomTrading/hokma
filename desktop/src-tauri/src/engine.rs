//! The engine next to the window: start it hidden, wait for /health, show the panel, stop it when the window closes.
//!
//! Start: `hokma start --no-browser` runs the supervisor in the foreground of a hidden child process and prints the
//! panel address with the session secrets (`#t=` and `&o=`) on its standard output, which only this window reads.
//! Nothing else learns the secrets: no file, no argument, no environment variable. Updates keep the same session
//! (the detached updater of 0.0.14 hands it over), so the open panel reconnects by itself.

use crate::http;
use crate::i18n::t;
use crate::paths;
use serde::Serialize;
use std::collections::VecDeque;
use std::io::{BufRead, BufReader, Read};
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::sync::mpsc;
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Emitter, Manager, Url};

pub const SLOW_AFTER: Duration = Duration::from_secs(15);
pub const GIVE_UP_AFTER: Duration = Duration::from_secs(60);
pub const POLL: Duration = Duration::from_millis(500);
pub const STOP_WAIT: Duration = Duration::from_secs(30);
pub const UPDATE_CHECK_EVERY: Duration = Duration::from_secs(6 * 60 * 60);
const TAIL_LINES: usize = 50;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

#[derive(Clone, Serialize, Default)]
pub struct Status {
    pub state: String,
    pub message: String,
    pub line: String,
}

#[derive(Default)]
pub struct Inner {
    pub status: Status,
    /// Every start gets a number; a start that is no longer the latest stops quietly.
    pub attempt: u64,
    pub port: Option<u16>,
    pub token: Option<String>,
    pub child: Option<Child>,
    pub ready: bool,
    pub closing: bool,
    pub exiting: bool,
    pub splash: Option<Url>,
    pub offered_version: Option<String>,
    pub errors: VecDeque<String>,
}

#[derive(Default)]
pub struct Shared(pub Mutex<Inner>);

pub fn shared(app: &AppHandle) -> tauri::State<'_, Shared> {
    app.state::<Shared>()
}

pub fn set_status(app: &AppHandle, state: &str, message: String, line: &str) {
    let status = Status { state: state.to_string(), message, line: line.to_string() };
    shared(app).0.lock().unwrap().status = status.clone();
    let _ = app.emit("hokma://status", status);
}

/// Back to the start screen (from the panel), to show a state there.
pub fn show_splash(app: &AppHandle) {
    let splash = shared(app).0.lock().unwrap().splash.clone();
    if let (Some(window), Some(url)) = (app.get_webview_window("main"), splash) {
        let on_splash = window.url().map(|u| u.origin() == url.origin()).unwrap_or(false);
        if !on_splash {
            let _ = window.navigate(url);
        }
    }
}

/// A command that runs the launcher hidden (no console window on Windows).
pub fn launcher_command(args: &[&str]) -> Command {
    let launcher = paths::launcher();
    #[cfg(windows)]
    let mut command = {
        use std::os::windows::process::CommandExt;
        let mut command = Command::new("cmd.exe");
        let quoted: Vec<String> = args.iter().map(|a| a.to_string()).collect();
        command.args(["/d", "/c"]);
        command.raw_arg(format!("\"\"{}\" {}\"", launcher.display(), quoted.join(" ")));
        command.creation_flags(CREATE_NO_WINDOW);
        command
    };
    #[cfg(not(windows))]
    let mut command = {
        let mut command = Command::new(&launcher);
        command.args(args);
        if let Some(path) = paths::child_path() {
            command.env("PATH", path);
        }
        command
    };
    command.stdin(Stdio::null());
    command
}

/// The port where a Hokma is already running (engine.lock names it and /health answers there).
fn running_port() -> Option<u16> {
    let text = std::fs::read_to_string(paths::data_dir().join("engine.lock")).ok()?;
    let body: serde_json::Value = serde_json::from_str(&text).ok()?;
    let port = u16::try_from(body.get("port")?.as_u64()?).ok()?;
    http::health(port).map(|_| port)
}

/// `http://127.0.0.1:<port>/#t=<token>&o=<code>` as printed by the supervisor: (port, token, full address).
pub fn parse_panel_address(line: &str) -> Option<(u16, String, Url)> {
    let line = line.trim();
    if !line.starts_with("http://127.0.0.1:") || !line.contains("#t=") {
        return None;
    }
    let url = Url::parse(line).ok()?;
    let port = url.port()?;
    let fragment = url.fragment()?.to_string();
    let token = fragment
        .split('&')
        .find_map(|part| part.strip_prefix("t="))
        .map(str::to_string)?;
    if !(http::FIRST_PORT..=http::LAST_PORT).contains(&port) || token.is_empty() {
        return None;
    }
    Some((port, token, url))
}

fn remember_error(app: &AppHandle, line: &str) {
    let mut inner = shared(app).0.lock().unwrap();
    inner.errors.push_back(line.to_string());
    while inner.errors.len() > TAIL_LINES {
        inner.errors.pop_front();
    }
}

enum Output {
    Out(String),
    Err(String),
}

fn pump<R: Read + Send + 'static>(stream: R, tx: mpsc::Sender<Output>, is_err: bool) {
    thread::spawn(move || {
        // The pipes stay read for the whole life of the window, so the supervisor never writes into a closed pipe.
        for line in BufReader::new(stream).lines() {
            let Ok(line) = line else { break };
            let _ = tx.send(if is_err { Output::Err(line) } else { Output::Out(line) });
        }
    });
}

/// Start (or start again) the engine and show the panel when it answers. Runs on its own thread.
pub fn start(app: AppHandle) {
    thread::spawn(move || run_start(app));
}

fn run_start(app: AppHandle) {
    let attempt = {
        let mut inner = shared(&app).0.lock().unwrap();
        inner.attempt += 1;
        inner.port = None;
        inner.token = None;
        inner.ready = false;
        inner.errors.clear();
        inner.attempt
    };
    show_splash(&app);
    set_status(&app, "starting", t("starting", &[]), "");

    if !paths::engine_installed() {
        #[cfg(target_os = "macos")]
        {
            if let Err(step) = crate::macos_install::run(&app) {
                match step {
                    crate::macos_install::Stop::NeedsTools => set_status(&app, "needs_tools", t("needs_tools", &[]), ""),
                    crate::macos_install::Stop::Failed(why) => {
                        remember_error(&app, &why);
                        set_status(&app, "failed", t("install_failed", &[("error", &why)]), "")
                    }
                }
                return;
            }
            set_status(&app, "starting", t("starting", &[]), "");
        }
        #[cfg(not(target_os = "macos"))]
        {
            set_status(&app, "incomplete", t("incomplete", &[]), "");
            return;
        }
    }

    if running_port().is_some() {
        // Started from the command line or by an older shortcut: this window does not have its session.
        set_status(&app, "port_busy", t("port_busy", &[]), "");
        return;
    }

    let mut command = launcher_command(&["start", "--no-browser"]);
    command.stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) => {
            let why = error.to_string();
            remember_error(&app, &why);
            set_status(&app, "failed", t("failed", &[("error", &why)]), "");
            return;
        }
    };
    let (tx, rx) = mpsc::channel();
    if let Some(out) = child.stdout.take() {
        pump(out, tx.clone(), false);
    }
    if let Some(err) = child.stderr.take() {
        pump(err, tx, true);
    }
    shared(&app).0.lock().unwrap().child = Some(child);

    let began = Instant::now();
    let mut slow_shown = false;
    let mut address: Option<(u16, String, Url)> = None;
    loop {
        if shared(&app).0.lock().unwrap().attempt != attempt {
            return;
        }
        while let Ok(output) = rx.try_recv() {
            match output {
                Output::Out(line) => {
                    if let Some(found) = parse_panel_address(&line) {
                        address = Some(found);
                    } else if !line.trim().is_empty() {
                        remember_error(&app, &line);
                    }
                }
                Output::Err(line) => {
                    if !line.trim().is_empty() {
                        remember_error(&app, &line);
                    }
                }
            }
        }
        if let Some((port, token, url)) = address.clone() {
            if let Some(health) = http::health(port) {
                ready(&app, port, token, url, &health.version);
                watch_output(app.clone(), rx);
                return;
            }
        }
        let exited = {
            let mut inner = shared(&app).0.lock().unwrap();
            inner.child.as_mut().and_then(|c| c.try_wait().ok().flatten())
        };
        if let Some(code) = exited {
            thread::sleep(Duration::from_millis(300));
            while let Ok(Output::Out(line) | Output::Err(line)) = rx.try_recv() {
                if !line.trim().is_empty() {
                    remember_error(&app, &line);
                }
            }
            if code.code() == Some(0) && running_port().is_some() {
                set_status(&app, "port_busy", t("port_busy", &[]), "");
            } else {
                let first = first_error(&app);
                set_status(&app, "failed", t("failed", &[("error", &first)]), "");
            }
            return;
        }
        let waited = began.elapsed();
        if waited > GIVE_UP_AFTER {
            let why = t("timeout", &[]);
            set_status(&app, "failed", t("failed", &[("error", &why)]), "");
            return;
        }
        if waited > SLOW_AFTER && !slow_shown {
            slow_shown = true;
            set_status(&app, "slow", t("slow", &[]), "");
        }
        thread::sleep(POLL);
    }
}

/// Keep reading the supervisor's output after the start: its later messages go to the detail tail.
fn watch_output(app: AppHandle, rx: mpsc::Receiver<Output>) {
    thread::spawn(move || {
        while let Ok(Output::Out(line) | Output::Err(line)) = rx.recv() {
            if !line.trim().is_empty() {
                remember_error(&app, &line);
            }
        }
    });
}

fn first_error(app: &AppHandle) -> String {
    let inner = shared(app).0.lock().unwrap();
    inner
        .errors
        .iter()
        .map(|l| l.trim())
        .find(|l| !l.is_empty())
        .map(|l| l.trim_end_matches('.').to_string())
        .unwrap_or_else(|| t("no_detail", &[]))
}

/// The engine answered: show the panel. The address carries the version (see cache.rs), so a cached page of
/// another version is never used.
fn ready(app: &AppHandle, port: u16, token: String, url: Url, version: &str) {
    let Some(window) = app.get_webview_window("main") else { return };
    crate::cache::remember(version);
    {
        let mut inner = shared(app).0.lock().unwrap();
        inner.port = Some(port);
        inner.token = Some(token);
        inner.ready = true;
    }
    set_status(app, "ready", String::new(), "");
    let _ = window.navigate(crate::cache::versioned(&url, version));
}

/// An update of the detached updater is running or ended a moment ago (update-status.json was written recently).
fn update_recent() -> bool {
    let Ok(text) = std::fs::read_to_string(paths::data_dir().join("update-status.json")) else { return false };
    let Ok(body) = serde_json::from_str::<serde_json::Value>(&text) else { return false };
    let updated = body.get("updated_at").and_then(serde_json::Value::as_f64).unwrap_or(0.0);
    let now = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0);
    now - updated < 15.0 * 60.0
}

/// Watches the engine while the panel is shown: notices a shutdown from the panel, keeps calm during an update,
/// and checks for a new version at the start and every 6 hours.
pub fn monitor(app: AppHandle) {
    thread::spawn(move || {
        let mut down_since: Option<Instant> = None;
        let mut next_update_check = Instant::now() + Duration::from_secs(60);
        loop {
            thread::sleep(Duration::from_secs(2));
            let (ready, port, closing) = {
                let inner = shared(&app).0.lock().unwrap();
                (inner.ready, inner.port, inner.closing)
            };
            let (true, Some(port), false) = (ready, port, closing) else {
                down_since = None;
                continue;
            };
            if let Some(health) = http::health(port) {
                down_since = None;
                // Updated while the window was open: the panel reloads itself; the next start knows this version.
                crate::cache::remember(&health.version);
                if Instant::now() >= next_update_check {
                    next_update_check = Instant::now() + UPDATE_CHECK_EVERY;
                    crate::update::offer_if_newer(&app);
                }
                continue;
            }
            let since = *down_since.get_or_insert_with(Instant::now);
            // An update stops Hokma for a few minutes and the panel shows its own "Updating Hokma" screen.
            let patience = if update_recent() { Duration::from_secs(10 * 60) } else { Duration::from_secs(8) };
            if since.elapsed() > patience {
                down_since = None;
                shared(&app).0.lock().unwrap().ready = false;
                show_splash(&app);
                set_status(&app, "stopped", t("stopped", &[]), "");
            }
        }
    });
}

/// Close Hokma: `hokma stop` (30 s at most), then end whatever still runs from the runtime or the managed Python.
pub fn stop_engine(app: &AppHandle) {
    if paths::launcher().is_file() {
        if let Ok(mut child) = launcher_command(&["stop"]).stdout(Stdio::null()).stderr(Stdio::null()).spawn() {
            let began = Instant::now();
            loop {
                match child.try_wait() {
                    Ok(Some(_)) => break,
                    Ok(None) if began.elapsed() < STOP_WAIT => thread::sleep(Duration::from_millis(200)),
                    _ => {
                        let _ = child.kill();
                        break;
                    }
                }
            }
        }
    }
    let child = shared(app).0.lock().unwrap().child.take();
    if let Some(mut child) = child {
        if child.try_wait().ok().flatten().is_none() {
            let _ = child.kill();
        }
        let _ = child.wait();
    }
    kill_leftovers(&paths::data_dir());
    let mut inner = shared(app).0.lock().unwrap();
    inner.ready = false;
    inner.port = None;
}

/// Every process whose executable or command line is under `<data>/runtime` or `<data>/python` (the rule of the
/// installer's stage 2), so nothing keeps port 8765 or a slot's files after the window is gone.
fn kill_leftovers(data: &Path) {
    let roots = [data.join("runtime"), data.join("python")];
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        let joined = roots
            .iter()
            .map(|r| format!("{}\\", r.display().to_string().trim_end_matches('\\')))
            .collect::<Vec<_>>()
            .join("|");
        let script = "$roots = $env:HOKMA_KILL_ROOTS -split '\\|'; \
            Get-CimInstance Win32_Process | Where-Object { $p = $_; $hit = $false; \
              foreach ($r in $roots) { \
                if ($p.ExecutablePath -and $p.ExecutablePath.StartsWith($r, [StringComparison]::OrdinalIgnoreCase)) { $hit = $true } \
                if ($p.CommandLine -and $p.CommandLine.IndexOf($r, [StringComparison]::OrdinalIgnoreCase) -ge 0) { $hit = $true } }; \
              $hit -and $p.ProcessId -ne $PID } | \
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }";
        for _ in 0..2 {
            let _ = Command::new("powershell.exe")
                .args(["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand"])
                .arg(encode_powershell(script))
                .env("HOKMA_KILL_ROOTS", &joined)
                .creation_flags(CREATE_NO_WINDOW)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status();
            thread::sleep(Duration::from_millis(800));
        }
    }
    #[cfg(not(windows))]
    {
        for root in roots {
            let pattern = format!("{}/", root.display());
            let _ = Command::new("pkill").args(["-f", &pattern]).stdout(Stdio::null()).stderr(Stdio::null()).status();
        }
    }
}

/// `-EncodedCommand` takes base64 of UTF-16LE: no quoting problems with paths.
#[cfg(windows)]
fn encode_powershell(script: &str) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let bytes: Vec<u8> = script.encode_utf16().flat_map(|u| u.to_le_bytes()).collect();
    let mut out = String::with_capacity(bytes.len() * 4 / 3 + 4);
    for chunk in bytes.chunks(3) {
        let n = (chunk[0] as u32) << 16 | (*chunk.get(1).unwrap_or(&0) as u32) << 8 | *chunk.get(2).unwrap_or(&0) as u32;
        out.push(TABLE[(n >> 18) as usize & 63] as char);
        out.push(TABLE[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 { TABLE[(n >> 6) as usize & 63] as char } else { '=' });
        out.push(if chunk.len() > 2 { TABLE[n as usize & 63] as char } else { '=' });
    }
    out
}

/// Last lines of the engine's logs and of what the start printed, for "Show detail".
pub fn detail(app: &AppHandle) -> String {
    let data = paths::data_dir();
    let mut lines: Vec<String> = Vec::new();
    for file in [data.join("logs").join("supervisor.out"), data.join("logs").join("engine.log")] {
        if let Ok(text) = std::fs::read_to_string(&file) {
            lines.push(format!("== {}", file.display()));
            let all: Vec<&str> = text.lines().collect();
            let from = all.len().saturating_sub(TAIL_LINES);
            lines.extend(all[from..].iter().map(|l| l.to_string()));
        }
    }
    let captured: Vec<String> = shared(app).0.lock().unwrap().errors.iter().cloned().collect();
    if !captured.is_empty() {
        lines.push("== hokma start".to_string());
        lines.extend(captured);
    }
    let from = lines.len().saturating_sub(TAIL_LINES * 2);
    lines[from..].join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_the_panel_address_printed_by_the_supervisor() {
        let (port, token, url) = parse_panel_address("http://127.0.0.1:8766/#t=abc123&o=xyz\n").unwrap();
        assert_eq!(port, 8766);
        assert_eq!(token, "abc123");
        assert_eq!(url.fragment(), Some("t=abc123&o=xyz"));
    }

    #[test]
    fn ignores_other_lines_and_other_hosts() {
        assert!(parse_panel_address("Hokma is running").is_none());
        assert!(parse_panel_address("http://example.com:8765/#t=a&o=b").is_none());
        assert!(parse_panel_address("http://127.0.0.1:9999/#t=a&o=b").is_none());
        assert!(parse_panel_address("http://127.0.0.1:8765/").is_none());
    }

    #[cfg(windows)]
    #[test]
    fn encodes_powershell_commands_as_utf16_base64() {
        assert_eq!(encode_powershell("dir"), "ZABpAHIA");
    }
}
