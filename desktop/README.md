# Hokma desktop window

Tauri 2 app that gives Hokma its own window (SPEC-001 addendum 0.0.13, section 11).

- Opening it starts the engine hidden (`hokma start --no-browser`) and shows `ui/index.html` until `/health`
  answers, then the panel served by the engine. Closing it stops the engine (it asks first when an agent works).
- One instance only; external links open in the system browser; menu "Hokma > Update Hokma" runs the engine's
  detached update (the same one as the panel's Update button).
- Windows: `Hokma.exe`, installed by `Hokma-Setup.exe` (`installer/windows/hokma-setup.iss`) in
  `%LOCALAPPDATA%\Programs\Hokma`. WebView2 data lives in `%LOCALAPPDATA%\Hokma-App\WebView2`.
- macOS: `Hokma.app` in `Hokma.dmg`; on its first start it installs the engine from the source package inside
  the app (`installer/install.sh`).
- Texts: `messages/es.json` and `messages/en.json`.

Built only by `.github/workflows/release-installers.yml` (`npx @tauri-apps/cli@^2 build`). Unit tests:
`cargo test` in `src-tauri`.
