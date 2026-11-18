//! What went wrong at start, read from the engine's own log (task 3288).
//!
//! The window used to show the FIRST line the start printed, which on a crashing computer is a harmless
//! "INFO: Started server process [16236]". This reads the lines newest first and takes the last thing that is really an
//! error: a crash with its exit code, the end of a Traceback, or an ERROR line. Plain std: no window, no translation, so
//! it is tested alone. The sentences the student reads are built in `engine.rs` from the messages files.

#[derive(Debug, PartialEq, Eq, Clone)]
pub enum Failure {
    /// The engine ended abnormally. `code` is shown as the log wrote it, or as 0xC0000005 for a Windows status.
    /// `safe` is true when it crashed again after safe mode had been started.
    Crash { code: String, safe: bool },
    /// A line that says what failed (the end of a Traceback, an ERROR line).
    Error(String),
}

const MAX_ERROR: usize = 200;

/// The line without the log's own time stamp ("2026-11-18T10:00:00Z ").
fn body(line: &str) -> &str {
    let line = line.trim();
    match line.split_once(' ') {
        Some((stamp, rest)) if is_stamp(stamp) => rest.trim_start(),
        _ => line,
    }
}

fn is_stamp(text: &str) -> bool {
    let bytes = text.as_bytes();
    bytes.len() == 20 && bytes[4] == b'-' && bytes[10] == b'T' && bytes[19] == b'Z'
}

/// Lines that are never the reason: progress and notes (INFO, DEBUG, WARNING) and the old "start again" hint.
fn is_noise(text: &str) -> bool {
    text.is_empty()
        || text.starts_with("INFO")
        || text.starts_with("DEBUG")
        || text.starts_with("WARNING")
        || text.contains("Start Hokma again with")
}

/// `engine crashed (code 3221225477); crash 4 ...` or `stopping: ... (last code 0xC0000005)`: the code as written.
fn crash_code(text: &str) -> Option<String> {
    let after = if let Some(rest) = text.strip_prefix("engine crashed (code ") {
        rest
    } else if text.starts_with("stopping:") {
        text.split_once("(last code ")?.1
    } else {
        return None;
    };
    let code = after.split(')').next()?.trim();
    if code.is_empty() {
        None
    } else {
        Some(code.to_string())
    }
}

/// An ERROR line, or the last line of a Traceback ("ModuleNotFoundError: No module named 'x'").
fn is_error(text: &str) -> bool {
    if text.starts_with("ERROR") || text.starts_with("CRITICAL") {
        return true;
    }
    match text.split_once(':') {
        Some((name, _)) => {
            (name.ends_with("Error") || name.ends_with("Exception"))
                && !name.is_empty()
                && name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '.')
        }
        None => false,
    }
}

fn shorten(text: &str) -> String {
    let text = text.trim().trim_end_matches('.');
    if text.chars().count() > MAX_ERROR {
        let cut: String = text.chars().take(MAX_ERROR).collect();
        format!("{cut}...")
    } else {
        text.to_string()
    }
}

/// The code of the crash that made the supervisor start safe mode, when a `safe mode:` line is there.
pub fn safe_mode_started(lines: &[String]) -> Option<String> {
    lines.iter().rev().find_map(|line| {
        let text = body(line);
        if text.starts_with("safe mode:") && text.contains("(last code ") {
            let code = text.split_once("(last code ")?.1.split(')').next()?.trim();
            Some(code.to_string())
        } else {
            None
        }
    })
}

/// The last real failure in `lines` (oldest first, as in the log), or None when there is only noise.
pub fn classify(lines: &[String]) -> Option<Failure> {
    let safe_at = lines.iter().rposition(|line| body(line).starts_with("safe mode:"));
    for (index, line) in lines.iter().enumerate().rev() {
        let text = body(line);
        if is_noise(text) {
            continue;
        }
        if let Some(code) = crash_code(text) {
            let safe = text.contains("already in safe mode") || safe_at.map_or(false, |at| index > at);
            return Some(Failure::Crash { code, safe });
        }
        if is_error(text) {
            return Some(Failure::Error(shorten(text)));
        }
    }
    None
}

/// 3221225477 -> 0xC0000005 (a Windows status); small numbers and codes already written as 0x... stay as they are.
pub fn show_code(code: &str) -> String {
    match code.parse::<u64>() {
        Ok(number) if number > 255 => format!("0x{:08X}", number & 0xFFFF_FFFF),
        _ => code.to_string(),
    }
}

/// The last line that is not noise, for a failure that has no recognisable error (never an INFO line).
pub fn last_useful(lines: &[String]) -> Option<String> {
    lines
        .iter()
        .rev()
        .map(|line| body(line))
        .find(|text| !is_noise(text))
        .map(shorten)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lines(text: &str) -> Vec<String> {
        text.lines().map(|l| l.to_string()).collect()
    }

    /// The engine.log of the student's HP computer, as the supervisor writes it.
    const STUDENT: &str = "\
2026-11-18T10:00:01Z INFO: Started server process [1408]
2026-11-18T10:00:01Z INFO: Waiting for application startup.
2026-11-18T10:00:02Z engines: engine at start: Gemini (gemini_cli), available=False
2026-11-18T10:00:02Z plugins: migration 0.0.20: 0 plugin(s) without files: -
2026-11-18T10:00:06Z engine crashed (code 3221225477); crash 4 in the last 60 s
2026-11-18T10:00:06Z INFO: Started server process [1408]
2026-11-18T10:00:06Z INFO: Waiting for application startup.
2026-11-18T10:00:06Z stopping: 4 crashes in 60 s. Start Hokma again with: hokma start";

    #[test]
    fn the_students_log_is_a_crash_with_its_code_never_an_info_line() {
        let failure = classify(&lines(STUDENT)).unwrap();
        assert_eq!(failure, Failure::Crash { code: "3221225477".to_string(), safe: false });
        assert_eq!(show_code("3221225477"), "0xC0000005");
    }

    #[test]
    fn the_last_line_of_a_traceback_wins_over_the_crash_line_before_it() {
        let log = "\
2026-11-18T10:00:06Z engine crashed (code 1); crash 1 in the last 60 s
2026-11-18T10:00:06Z INFO: Started server process [7]
2026-11-18T10:00:06Z Traceback (most recent call last):
2026-11-18T10:00:06Z   File \"x.py\", line 3, in <module>
2026-11-18T10:00:06Z ModuleNotFoundError: No module named 'sqlite_vec'";
        assert_eq!(
            classify(&lines(log)),
            Some(Failure::Error("ModuleNotFoundError: No module named 'sqlite_vec'".to_string()))
        );
    }

    #[test]
    fn an_error_level_line_counts_and_info_and_warning_do_not() {
        let log = "ERROR:    Application startup failed. Exiting.\nINFO:     Finished server process [3]\nWARNING: slow";
        assert_eq!(classify(&lines(log)), Some(Failure::Error("ERROR:    Application startup failed. Exiting".to_string())));
        assert_eq!(classify(&lines("INFO: Started server process [16236].\nWARNING: x")), None);
        assert_eq!(last_useful(&lines("INFO: Started server process [16236].")), None);
    }

    #[test]
    fn a_crash_after_safe_mode_is_marked_as_such() {
        let log = "\
2026-11-18T10:00:06Z engine crashed (code 3221225477); crash 4 in the last 60 s
2026-11-18T10:00:06Z safe mode: 4 crashes in 60 s (last code 0xC0000005); starting the engine again without its optional native parts
2026-11-18T10:01:06Z engine crashed (code 3221225477); crash 4 in the last 60 s";
        assert_eq!(classify(&lines(log)), Some(Failure::Crash { code: "3221225477".to_string(), safe: true }));
        assert_eq!(safe_mode_started(&lines(log)), Some("0xC0000005".to_string()));
        let only_before = "\
2026-11-18T10:00:06Z engine crashed (code 3221225477); crash 4 in the last 60 s
2026-11-18T10:00:06Z safe mode: 4 crashes in 60 s (last code 0xC0000005); starting";
        assert_eq!(classify(&lines(only_before)), Some(Failure::Crash { code: "3221225477".to_string(), safe: false }));
    }

    #[test]
    fn the_stopping_line_carries_the_last_code() {
        let log = "2026-11-18T10:01:06Z stopping: 4 crashes in 60 s (last code 0xC0000005), already in safe mode";
        assert_eq!(classify(&lines(log)), Some(Failure::Crash { code: "0xC0000005".to_string(), safe: true }));
    }

    #[test]
    fn codes_are_shown_in_hex_only_when_they_are_a_windows_status() {
        assert_eq!(show_code("1"), "1");
        assert_eq!(show_code("0xC0000005"), "0xC0000005");
        assert_eq!(show_code("-11"), "-11");
        assert_eq!(show_code("3221225725"), "0xC00000FD");
    }

    #[test]
    fn the_old_start_again_hint_is_never_the_reason() {
        assert_eq!(last_useful(&lines("stopping soon. Start Hokma again with: hokma start")), None);
    }
}
