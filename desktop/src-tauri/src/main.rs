//! Hokma desktop window (Tauri 2). SPEC-001 addendum 0.0.13, section 11, task 3138.
//!
//! One window, one engine: opening the app starts the engine hidden. The window itself stays hidden (with Hokma's dark
//! background, so it can never flash white) until the panel has loaded, or until there is something to say (an error,
//! a slow start); the tray icon is there from the first moment and says "opening". Closing the window only hides it
//! (the engine keeps running, so routines fire at their hour); the tray icon reopens it, and "Quit" stops the engine
//! and exits (asking first when an agent is working). A second launch only brings the window to the front. The panel
//! itself is served by the engine (http://127.0.0.1:<port>), so every engine version brings its own panel; the window
//! only adds the start screen, the Update menu and the link rules (external links open in the system browser).

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
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::webview::PageLoadEvent;
use tauri::window::Color;
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

/// The engine's panel itself finished loading (what the window waits for before it opens). Not the start screen, and
/// not the start link on 127.0.0.1 that only hands the session over to the engine's own host a moment later.
fn is_panel_page(url: &Url) -> bool {
    if !matches!(url.scheme(), "http" | "https") || !stays_in_window(url) || url.host_str() == Some("tauri.localhost") {
        return false;
    }
    let start_link = url.host_str() == Some("127.0.0.1")
        && url.fragment().map_or(false, |f| f.split('&').any(|part| part.starts_with("t=")));
    !start_link
}

/// The page of the app itself (the start screen), as the webview loaded it.
fn is_start_screen(url: &Url) -> bool {
    url.scheme() == "tauri" || url.host_str() == Some("tauri.localhost")
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

/// What closing the window does: hide it and leave the engine running. Only a real quit (already `exiting`) lets
/// the window close.
fn close_hides_window(exiting: bool) -> bool {
    !exiting
}

/// Quitting asks first only when an agent is working.
fn quit_needs_confirmation(busy: bool) -> bool {
    busy
}

fn show_main(app: &AppHandle) {
    engine::reveal(app);
}

fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "tray_open", t("tray_open", &[]), true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "tray_quit", t("menu_quit", &[]), true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &PredefinedMenuItem::separator(app)?, &quit])?;
    let mut builder = TrayIconBuilder::with_id("main")
        .tooltip(t("tray_opening", &[]))
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "tray_open" => show_main(app),
            "tray_quit" => begin_close(app.clone()),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = event {
                show_main(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }
    builder.build(app)?;
    Ok(())
}

/// Quit: ask when an agent is working, then stop the engine and exit.
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
        if quit_needs_confirmation(busy) {
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
            show_main(app);
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
            // The owner prefers the browser: the icon runs `hokma start` (opens the browser) and this window closes.
            if paths::browser_preferred() && paths::engine_installed() {
                if engine::launcher_command(&["start"]).spawn().is_ok() {
                    std::process::exit(0);
                }
            }
            // Before the webview opens its cache: drop it when the engine version changed.
            cache::prune_if_version_changed();
            // The icon first: while the window is still hidden it is the sign that Hokma is opening.
            build_tray(&handle)?;
            let page_handle = handle.clone();
            #[allow(unused_mut)]
            let mut builder = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("Hokma")
                .inner_size(1280.0, 800.0)
                .min_inner_size(960.0, 600.0)
                .maximized(true)
                // Hidden and dark until the panel has loaded: the webview paints white before its first page.
                .visible(false)
                .background_color(Color(16, 21, 22, 255))
                // The panel receives dropped files itself, as in a browser.
                .disable_drag_drop_handler()
                .initialization_script(LINKS_SCRIPT)
                .on_page_load(move |_window, payload| {
                    if let PageLoadEvent::Finished = payload.event() {
                        if is_panel_page(payload.url()) {
                            engine::reveal_once(&page_handle);
                        }
                    } else if is_start_screen(payload.url()) {
                        engine::remember_splash(&page_handle, payload.url());
                    }
                })
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
            builder.build()?;
            engine::start(handle.clone());
            engine::monitor(handle);
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let app = window.app_handle().clone();
                let exiting = shared(&app).0.lock().unwrap().exiting;
                if close_hides_window(exiting) {
                    api.prevent_close();
                    let _ = window.hide();
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
    fn closing_the_window_hides_it_until_a_real_quit() {
        assert!(close_hides_window(false));
        assert!(!close_hides_window(true));
    }

    #[test]
    fn quit_asks_only_when_an_agent_is_working() {
        assert!(quit_needs_confirmation(true));
        assert!(!quit_needs_confirmation(false));
    }

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
    fn the_window_opens_for_the_panel_and_not_for_the_start_link_or_the_start_screen() {
        for url in [
            "http://hokma.localhost:8765/",
            "http://127.0.0.1:8765/",
            "http://localhost:8766/#c=abc",
            "http://127.0.0.1:8765/#next=chat",
        ] {
            assert!(is_panel_page(&Url::parse(url).unwrap()), "{url}");
        }
        for url in [
            "http://127.0.0.1:8765/#t=abc&o=xyz",
            "http://tauri.localhost/index.html",
            "tauri://localhost/index.html",
            "https://github.com/SiomTrading/hokma",
            "about:blank",
        ] {
            assert!(!is_panel_page(&Url::parse(url).unwrap()), "{url}");
        }
    }

    #[test]
    fn the_start_screen_is_known_by_its_own_address() {
        assert!(is_start_screen(&Url::parse("http://tauri.localhost/index.html").unwrap()));
        assert!(is_start_screen(&Url::parse("tauri://localhost/index.html").unwrap()));
        assert!(!is_start_screen(&Url::parse("about:blank").unwrap()));
        assert!(!is_start_screen(&Url::parse("http://127.0.0.1:8765/").unwrap()));
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
