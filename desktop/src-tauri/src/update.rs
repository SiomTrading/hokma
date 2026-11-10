//! The Update button of the window (menu "Hokma > Update Hokma"). It asks the engine for the same detached update
//! the panel's button runs (0.0.14): the engine stops, the updater replaces the runtime outside the slots and starts
//! Hokma again with the same session; the panel shows "Updating Hokma" and reconnects by itself.

use crate::engine::shared;
use crate::http::{self, UpdateOutcome};
use crate::i18n::t;
use std::thread;
use tauri::menu::MenuItem;
use tauri::{AppHandle, Manager, Wry};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

/// The menu item, kept to show "Update to version X" when one is available.
pub struct UpdateItem(pub MenuItem<Wry>);

fn session(app: &AppHandle) -> Option<(u16, String)> {
    let inner = shared(app).0.lock().unwrap();
    match (inner.ready, inner.port, inner.token.clone()) {
        (true, Some(port), Some(token)) => Some((port, token)),
        _ => None,
    }
}

fn info(app: &AppHandle, text: String, kind: MessageDialogKind) {
    app.dialog().message(text).title("Hokma").kind(kind).blocking_show();
}

fn run_update(app: &AppHandle, port: u16, token: &str) {
    match http::start_update(port, token) {
        UpdateOutcome::Launched => {}
        UpdateOutcome::UpToDate(version) => info(app, t("up_to_date", &[("version", &version)]), MessageDialogKind::Info),
        UpdateOutcome::Failed(why) => info(app, t("update_failed", &[("error", &why)]), MessageDialogKind::Error),
    }
}

/// Menu click: update now (the engine itself checks the latest release first).
pub fn update_now(app: AppHandle) {
    thread::spawn(move || match session(&app) {
        Some((port, token)) => run_update(&app, port, &token),
        None => info(&app, t("update_not_ready", &[]), MessageDialogKind::Info),
    });
}

/// At the start and every 6 hours: when the engine knows a newer version, the menu says so and the window asks
/// once per version.
pub fn offer_if_newer(app: &AppHandle) {
    let Some((port, token)) = session(app) else { return };
    let Some(state) = http::update_state(port, &token) else { return };
    let (true, Some(latest), false) = (state.newer, state.latest, state.updating) else { return };
    if let Some(item) = app.try_state::<UpdateItem>() {
        let _ = item.0.set_text(t("menu_update_to", &[("version", &latest)]));
    }
    {
        let mut inner = shared(app).0.lock().unwrap();
        if inner.offered_version.as_deref() == Some(latest.as_str()) {
            return;
        }
        inner.offered_version = Some(latest.clone());
    }
    let accepted = app
        .dialog()
        .message(t("update_available", &[("version", &latest)]))
        .title("Hokma")
        .kind(MessageDialogKind::Info)
        .buttons(MessageDialogButtons::OkCancelCustom(t("update_now", &[]), t("update_later", &[])))
        .blocking_show();
    if accepted {
        run_update(app, port, &token);
    }
}
