//! First start on macOS: the .dmg only carries the window, so the window installs the engine itself, with no
//! Terminal: Apple's developer tools (Git) through Apple's own dialog, Claude Code (recommended, never required)
//! and then `installer/install.sh` from the source package inside the app (the same tree `git archive` makes).

use crate::engine::set_status;
use crate::i18n::{t, texts};
use crate::paths;
use std::io::{BufRead, BufReader, Read};
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Manager};

pub enum Stop {
    /// Apple's installer of the developer tools is open; the student clicks Retry when it ends.
    NeedsTools,
    Failed(String),
}

fn quiet(command: &mut Command) -> bool {
    command.stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
    if let Some(path) = paths::child_path() {
        command.env("PATH", path);
    }
    command.status().map(|s| s.success()).unwrap_or(false)
}

fn claude_present() -> bool {
    paths::home().join(".local").join("bin").join("claude").is_file()
        || quiet(Command::new("/bin/sh").args(["-c", "command -v claude"]))
}

/// Run a command, showing each output line under the message; returns (success, last line).
fn run_shown(app: &AppHandle, message: &str, command: &mut Command) -> (bool, String) {
    command.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
    if let Some(path) = paths::child_path() {
        command.env("PATH", path);
    }
    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) => return (false, error.to_string()),
    };
    let (tx, rx) = mpsc::channel::<String>();
    fn pump<R: Read + Send + 'static>(stream: R, tx: mpsc::Sender<String>) {
        thread::spawn(move || {
            for line in BufReader::new(stream).lines().map_while(Result::ok) {
                let _ = tx.send(line);
            }
        });
    }
    if let Some(out) = child.stdout.take() {
        pump(out, tx.clone());
    }
    if let Some(err) = child.stderr.take() {
        pump(err, tx);
    }
    let mut last = String::new();
    for line in rx {
        let clean = line.trim().to_string();
        if !clean.is_empty() {
            set_status(app, "installing", message.to_string(), &clean);
            last = clean;
        }
    }
    let ok = child.wait().map(|s| s.success()).unwrap_or(false);
    (ok, last)
}

pub fn run(app: &AppHandle) -> Result<(), Stop> {
    if !quiet(Command::new("/usr/bin/xcode-select").arg("-p")) {
        let _ = Command::new("/usr/bin/xcode-select").arg("--install").status();
        return Err(Stop::NeedsTools);
    }
    let installing = t("installing", &[]);
    set_status(app, "installing", installing.clone(), "");

    if !claude_present() {
        let message = t("installing_claude", &[]);
        set_status(app, "installing", message.clone(), "");
        let (ok, last) = run_shown(app, &message, Command::new("/bin/bash").args(["-c", "curl -fsSL https://claude.ai/install.sh | bash"]));
        if !ok {
            eprintln!("Claude Code could not be installed: {last}");
        }
        set_status(app, "installing", installing.clone(), "");
    }

    let archive = app
        .path()
        .resource_dir()
        .map_err(|e| Stop::Failed(e.to_string()))?
        .join("hokma-src.tar.gz");
    if !archive.is_file() {
        return Err(Stop::Failed(format!("the app has no source package ({})", archive.display())));
    }
    let stamp = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
    let work = paths::home().join("Library").join("Caches").join("Hokma-Setup").join(format!("src-{stamp}"));
    std::fs::create_dir_all(&work).map_err(|e| Stop::Failed(e.to_string()))?;
    let result = (|| {
        if !quiet(Command::new("/usr/bin/tar").arg("-xzf").arg(&archive).arg("-C").arg(&work)) {
            return Err(Stop::Failed("the source package could not be unpacked".to_string()));
        }
        let script = work.join("installer").join("install.sh");
        let (ok, last) = run_shown(
            app,
            &installing,
            Command::new("/bin/bash").arg(&script).args(["--lang", texts().lang, "--no-doctor"]),
        );
        if !ok || !paths::engine_installed() {
            return Err(Stop::Failed(if last.is_empty() { "install.sh failed".to_string() } else { last }));
        }
        Ok(())
    })();
    let _ = std::fs::remove_dir_all(&work);
    result
}
