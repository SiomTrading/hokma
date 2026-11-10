//! Hokma desktop window (Tauri 2). SPEC-001 addendum 0.0.13, section 11, task 3138.
//!
//! One window, one engine: opening the window starts the engine hidden and shows the start screen until /health
//! answers; closing the window stops the engine (asking first when an agent is working). A second launch only
//! brings the open window to the front. The panel itself is served by the engine (http://127.0.0.1:<port>), so
//! every engine version brings its own panel; the window only adds the start screen, the Update menu and the
//! link rules (external links open in the system browser).

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod cache;
mod engine;
mod http;
mod i18n;
#[cfg(target_os = "macos")]
mod macos_install;
mod paths;
mod update;

use engine::{shared, Shared, Status};
use i18n::{t, texts};
use serde::Serialize;
use std::thread;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::{AppHandle, Manager, RunEvent, Url, WebviewUrl, WebviewWindowBuilder, WindowEvent, Wry};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

/// Links with target="_blank" and window.open() become plain navigations, so the navigation rule below decides:
/// the panel stays in the window, everything else opens in the system browser.
const LINKS_SCRIPT: &str = r#"
(function () {
  if (window.__hokmaLinks) { return; }
  window.__hokmaLinks = true;
  document.addEventListener("click", function (event) {
    var link = event.target && event.target.closest ? event.target.closest("a[target=_blank]") : null;
    if (!link || !link.href) { return; }
    event.preventDefault();
    window.location.assign(link.href);
  }, true);
  window.open = function (url) {
    if (url) { try { window.location.assign(new URL(url, window.location.href).href); } catch (e) {} }
    return null;
  };
})();
"#;

/// Addresses that stay in the window: the start screen and the engine's panel on its ports.
fn stays_in_window(url: &Url) -> bool {
    match url.scheme() {
        "tauri" | "about" | "data" | "blob" => return true,
        "http" | "https" => {}
        _ => return false,
    }
    match url.host_str() {
        Some("tauri.localhost") => true,
        Some("127.0.0.1") | Some("localhost") | Some("hokma.localhost") => url
            .port()
            .map(|p| (http::FIRST_PORT..=http::LAST_PORT).contains(&p))
            .unwrap_or(false),
        _ => false,
    }
}

/// Links that leave the window and may go to the system: web pages and an email address (task 3150). Any other
/// scheme (`file:`, an app's own scheme, `javascript:`, a `ms-` handler) is blocked without a message, so a link in
/// an agent's answer can never open a local file or start a program.
fn opens_outside(url: &Url) -> bool {
    matches!(url.scheme(), "http" | "https" | "mailto")
}

#[derive(Serialize)]
struct Messages {
    lang: &'static str,
    texts: serde_json::Map<String, serde_json::Value>,
}

#[tauri::command]
fn messages() -> Messages {
    Messages { lang: texts().lang, texts: texts().map.clone() }
}

#[tauri::command]
fn current_status(app: AppHandle) -> Status {
    shared(&app).0.lock().unwrap().status.clone()
}

#[tauri::command]
fn retry(app: AppHandle) {
    engine::start(app);
}

/// "Close it and retry": the Hokma running in the background (without this window's session) is stopped first.
#[tauri::command]
fn kill_and_retry(app: AppHandle) {
    thread::spawn(move || {
        engine::set_status(&app, "closing", t("closing", &[]), "");
        engine::stop_engine(&app);
        engine::start(app);
    });
}

#[tauri::command]
fn engine_log(app: AppHandle) -> String {
    engine::detail(&app)
}

fn build_menu(app: &AppHandle) -> tauri::Result<Menu<Wry>> {
    let update = MenuItem::with_id(app, "update", t("menu_update", &[]), true, None::<&str>)?;
    let reload = MenuItem::with_id(app, "reload", t("menu_reload", &[]), true, Some("CmdOrCtrl+R"))?;
    let quit = MenuItem::with_id(app, "quit", t("menu_quit", &[]), true, Some("CmdOrCtrl+Q"))?;
    app.manage(update::UpdateItem(update.clone()));
    let hokma = Submenu::with_items(
        app,
        t("menu_hokma", &[]),
        true,
        &[&update, &reload, &PredefinedMenuItem::separator(app)?, &quit],
    )?;
    // macOS needs an Edit menu for copy and paste shortcuts to reach the page.
    #[cfg(target_os = "macos")]
    {
        let edit = Submenu::with_items(
            app,
            t("menu_edit", &[]),
            true,
            &[
                &PredefinedMenuItem::undo(app, None)?,
                &PredefinedMenuItem::redo(app, None)?,
                &PredefinedMenuItem::separator(app)?,
                &PredefinedMenuItem::cut(app, None)?,
                &PredefinedMenuItem::copy(app, None)?,
                &PredefinedMenuItem::paste(app, None)?,
                &PredefinedMenuItem::select_all(app, None)?,
            ],
        )?;
        return Menu::with_items(app, &[&hokma, &edit]);
    }
    #[cfg(not(target_os = "macos"))]
    Menu::with_items(app, &[&hokma])
}

/// Close the window: ask when an agent is working, then stop the engine and quit.
fn begin_close(app: AppHandle) {
    {
        let mut inner = shared(&app).0.lock().unwrap();
        if inner.closing || inner.exiting {
            return;
        }
        inner.closing = true;
    }
    thread::spawn(move || {
        let port = shared(&app).0.lock().unwrap().port;
        let busy = port.and_then(http::health).map(|h| h.busy).unwrap_or(false);
        if busy {
            let close = app
                .dialog()
                .message(t("busy_question", &[]))
                .title("Hokma")
                .kind(MessageDialogKind::Warning)
                .buttons(MessageDialogButtons::OkCancelCustom(t("busy_close", &[]), t("busy_keep", &[])))
                .blocking_show();
            if !close {
                shared(&app).0.lock().unwrap().closing = false;
                return;
            }
        }
        engine::show_splash(&app);
        engine::set_status(&app, "closing", t("closing", &[]), "");
        engine::stop_engine(&app);
        shared(&app).0.lock().unwrap().exiting = true;
        app.exit(0);
    });
}

fn main() {
    let app = tauri::Builder::default()
        // First: a second launch hands over to this one and only brings the window to the front.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(Shared::default())
        .menu(build_menu)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "update" => update::update_now(app.clone()),
            "reload" => {
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.eval("window.location.reload()");
                }
            }
            "quit" => begin_close(app.clone()),
            _ => {}
        })
        .invoke_handler(tauri::generate_handler![messages, current_status, retry, kill_and_retry, engine_log])
        .setup(|app| {
            let handle = app.handle().clone();
            // Before the webview opens its cache: drop it when the engine version changed.
            cache::prune_if_version_changed();
            #[allow(unused_mut)]
            let mut builder = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("Hokma")
                .inner_size(1280.0, 800.0)
                .min_inner_size(960.0, 600.0)
                .maximized(true)
                // The panel receives dropped files itself, as in a browser.
                .disable_drag_drop_handler()
                .initialization_script(LINKS_SCRIPT)
                .on_navigation(|url| {
                    if stays_in_window(url) {
                        return true;
                    }
                    if opens_outside(url) {
                        let _ = tauri_plugin_opener::open_url(url.as_str(), None::<&str>);
                    }
                    false
                });
            #[cfg(windows)]
            {
                // Cache and storage of the window live apart from the student's browser.
                builder = builder.data_directory(paths::webview_data_dir());
            }
            let window = builder.build()?;
            if let Ok(url) = window.url() {
                shared(&handle).0.lock().unwrap().splash = Some(url);
            }
            engine::start(handle.clone());
            engine::monitor(handle);
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let app = window.app_handle().clone();
                if !shared(&app).0.lock().unwrap().exiting {
                    api.prevent_close();
                    begin_close(app);
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("the Hokma window could not start");

    app.run(|app, event| {
        // macOS Cmd+Q and the Dock's Quit go through the same close.
        if let RunEvent::ExitRequested { api, .. } = event {
            if !shared(app).0.lock().unwrap().exiting {
                api.prevent_exit();
                begin_close(app.clone());
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_panel_and_the_start_screen_stay_in_the_window() {
        for url in [
            "http://127.0.0.1:8765/#t=a&o=b",
            "http://hokma.localhost:8770/",
            "http://tauri.localhost/index.html",
            "tauri://localhost/index.html",
        ] {
            assert!(stays_in_window(&Url::parse(url).unwrap()), "{url}");
        }
    }

    #[test]
    fn everything_else_opens_in_the_browser() {
        for url in [
            "https://github.com/SiomTrading/hokma",
            "http://127.0.0.1:9000/",
            "http://127.0.0.1/",
            "https://claude.ai/oauth/authorize?x=1",
            "mailto:support@example.com",
        ] {
            assert!(!stays_in_window(&Url::parse(url).unwrap()), "{url}");
        }
    }

    #[test]
    fn only_web_pages_and_email_go_to_the_system() {
        for url in [
            "https://github.com/SiomTrading/hokma",
            "http://example.com/",
            "mailto:support@example.com",
            "HTTPS://EXAMPLE.COM/",
        ] {
            assert!(opens_outside(&Url::parse(url).unwrap()), "{url}");
        }
    }

    #[test]
    fn other_schemes_are_blocked() {
        for url in [
            "file:///C:/Windows/System32/calc.exe",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "ms-settings:privacy",
            "ms-msdt:/id",
            "search-ms:query=x",
            "vscode://file/c:/x",
            "smb://server/share",
            "ftp://example.com/",
            "tel:+1555",
            "data:text/html,hi",
        ] {
            assert!(!opens_outside(&Url::parse(url).unwrap()), "{url}");
        }
    }
}
