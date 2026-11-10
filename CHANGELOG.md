# Changelog

## 0.0.17

The Windows installer builds again.

- The 0.0.16 Windows installer could not be built because of a wrong type name in the installer script. It is
  fixed, and every push now compiles the installer script on Windows before anything is published.

## 0.0.16

Updating from the panel on Windows brings Hokma back by itself again, with no window left behind, the Hokma
window only opens web and email links, and remote MCP servers only show Connected when they really answer.

- After an update, Hokma starts again with no window at all. Until now an empty black window could appear on
  Windows and the panel did not come back; the only way out was to restart the computer.
- The updater starts Hokma on its own, so nothing that ends with the update takes it down, and it checks that the
  panel answers. If it does not, it tries again (three times in all); when it still cannot, the panel says that
  Hokma was updated but did not open by itself, and the reason is kept for support.
- Every step of that start is written to the update log (`logs/updater.log`): what was started, its process
  number, how it ended and whether the panel answered.
- The update from 0.0.15 to 0.0.16 still runs the 0.0.15 updater: if the empty window shows up one last time, it
  closes by itself in a few seconds and Hokma keeps running.
- The Hokma window only sends links to web pages (http, https) and email addresses (mailto) to your browser or
  mail app. Any other kind of link, such as one to a file on your computer, is ignored, so a link in an answer can
  never open a file or start a program.
- The Hokma window for Windows and Mac builds again. The 0.0.15 installers could not be built because of an error
  in how the window reads its own state; it is fixed, and every change to the window is now compiled and tested
  on Windows and Mac before a release is published.
- Settings > Extensions > MCP: every remote server has Remove, Revoke and Edit URL. Remove asks first, revokes
  the access and stops offering the server to every engine; Edit URL checks the new address and starts without
  the old sign-in.
- Before an address is saved, Hokma asks it to start an MCP session. Only a real MCP answer, or a request to sign
  in, is accepted; a web page (for example https://lauz.io/app/mcp instead of https://mcp.lauz.io/mcp), a redirect
  or an error is refused with the reason, and nothing is saved.
- A server shows Connected only when it answered as an MCP server at its current address: open, or with a valid
  sign-in. A server that 0.0.15 marked Connected by mistake now shows the error with its reason, and is not handed
  to any engine until its address is fixed.
- Adding a server with a name already in the list says the name is taken, instead of asking for a name.
- Servers that Hokma 0.0.10 to 0.0.12 also registered in Claude Code's own settings are removed from there when
  Hokma starts, so Claude Code always uses the address and sign-in you see in the panel.

## 0.0.15

Connect an MCP server from the panel and sign in with the browser, whatever engine you use. Hokma also gets a
one-click installer and its own window on Windows and Mac.

- Settings > Extensions > MCP: add a server with a name and its address. If it asks for a sign-in, Connect opens your
  browser on the provider's page; when you accept, the tab says "Connected, you can close this tab" and the panel
  turns to Connected by itself. No terminal, no pasted address, and no Claude window opens when you use ChatGPT
  (Codex) or Gemini. Revoke forgets the access.
- Hokma signs in by itself (OAuth 2.1): it finds the authorization server from the MCP address, registers itself,
  uses PKCE and comes back to its own server on this computer with a one-use code that expires in ten minutes.
  Tested against Lauz (https://mcp.lauz.io/mcp).
- The access and refresh tokens live only in the system credential store (Windows Credential Manager, macOS
  Keychain, Secret Service on Linux), never in the database, the logs or a settings file, and are renewed before
  they end.
- Every engine gets the connected servers: Claude Code through a header helper that reads the store (the MCP file
  holds no token), Codex through its own process environment (hidden from the commands the agent runs), Gemini
  through a header when the session starts, and API key engines through Hokma's own MCP client. A server that still
  needs a sign-in is not passed to any engine, so none of them starts its own sign-in.
- Replaces the `claude mcp login` flow and the "paste the final address" window of 0.0.10 to 0.0.14. Servers added
  back then show Needs to connect once: press Connect.
- Windows: `Hokma-Setup.exe`, an Inno Setup installer. Double click, no terminal and no administrator rights; it
  installs Hokma for the current user and opens the Hokma window when it ends.
- Mac: `Hokma.dmg` with the `Hokma.app` window; on its first start it installs the engine from the package it
  carries.
- The Hokma window is a Tauri app that starts the engine, waits for it and shows the panel. It cleans its cache
  whenever the engine version changes, without losing the panel's saved settings, and updates Hokma with the same
  detached updater as the panel's Update button.
- The installers are built by the `release-installers.yml` workflow when a release tag is pushed; it reads the
  version from `backend/hokma/__init__.py` and refuses a tag that does not match it.

## 0.0.14

Updating from the panel works on Windows.

- The Update button no longer updates inside the running server. Hokma starts a separate updater that lives outside
  its own folders (the shared Python of the data folder and a temporary copy of the update code), shuts itself down
  the same way Shut down Hokma does, and the updater closes whatever was left running from Hokma's folders, updates
  and opens Hokma again. Before, the server kept its own files open and Windows refused to replace them
  (`PermissionError [WinError 5]` on a `.pyd` of the old version).
- While it updates, the panel says "Updating Hokma, it will restart by itself" and reconnects on its own when Hokma
  is back, with the same session. If the update fails, Hokma comes back on the previous version and the panel shows
  the reason in plain words with a Retry button; the technical detail goes to `logs/updater.log` in the data folder.
- `hokma update` from PowerShell uses the same updater when it is run by Hokma's own command, and says where the
  result will be written.

## 0.0.13

Pictures reach every engine, plugins you install stay, and the panel menu fits small screens.

- Pictures in chat with engines other than Claude Code. With an API key engine (Anthropic or OpenAI compatible) the
  picture now travels to the model with the message. Before, only Claude Code got it: the other engines kept the
  text and dropped the picture, so the model answered that it had no way to read images. Engines that cannot take a
  picture in the message (Codex, Gemini) get it as a file in the project folder, named in the message, so they can
  open it with their own tools.
- Plugins and marketplaces installed from inside Claude Code (`/plugin install`, `/plugin marketplace add`) are no
  longer lost when Hokma starts Claude or updates: they are merged with the student's extensions instead of being
  replaced. Permissions, sandbox and hooks are still always imposed by Hokma.
- The panel page is served with `Cache-Control: no-store` (hashed assets stay cached), and a tab that sees the
  engine come back on another version reloads, so an update no longer leaves a blank page.
- The left menu fits any window height (it compacts instead of cutting items) and the New conversation entry shows
  the real shortcut instead of a fixed label.

## 0.0.12

Updating on Windows no longer fails when a file is in use or the working folder is synced.

- `hokma update` refuses, with a plain message, to rebuild the slot that its own process is running from, and waits
  and retries when a file of the old slot is locked, instead of ending in `PermissionError [WinError 32]`.
- Writing a kit file retries with a new temporary name when OneDrive, an antivirus or the indexer touches the file
  or the folder at the same instant.

## 0.0.11

Settings > Extensions is tidy again, and installing a plugin takes one click.

- Two tabs, Plugins and MCP, in the same glass style as the rest of the panel. Hokma remembers the tab you chose in
  this browser. Plugins holds the marketplace, installed plugins and "Add by link"; MCP holds remote servers
  (Authorize and Revoke), local servers and agent requests for MCP servers.
- The Anthropic marketplace with its search box is always visible in Plugins; no button to open it first.
- Not installed: one Install button. It adds the marketplace to your list when needed, asks for your confirmation
  once, and the plugin ends up installed and on. Installed: On/Off switch and Uninstall (also asks first).
- New `POST /api/extensions/uninstall`, with the same confirmation and the same recorded approval as every change.

## 0.0.10

Add and authorize a remote MCP server (for example Lauz) without leaving Hokma.

- Settings > Extensions > Remote MCP servers: type a name and an https address and press Add. The server appears as
  "Needs authorization".
- Authorize opens a window with a sign-in link. Sign in, accept, copy the full address of the final tab (it does not
  load, that is expected), paste it and press Connect. Claude Code keeps and refreshes the token in Hokma's own
  folder; Hokma never reads it, stores it or writes it to logs.
- States: Needs authorization, Connected, Error (with Retry). Revoke clears the access and keeps the server listed.
- The left menu now has a collapse button next to the logo (Ctrl+B also works); the choice is remembered.

## 0.0.9

Hokma works with any model, keeps its protections with every engine, and the tools it promised now exist.

- Connect the AI you already have. Engines: Claude Code (still the recommended one), Codex with a ChatGPT account or
  an OpenAI key, Gemini with a Google account (free quota) or a key, and a direct API key for OpenAI, Anthropic,
  Google, OpenRouter, DeepSeek, Ollama, LM Studio or any OpenAI compatible address. Nothing is required: you can
  choose "later" and the panel opens complete. Connections are listed, tested with a real short turn, renamed,
  made default and removed (API under `/api/engines`). Keys live only in the secure store of your computer.
- Levels: Fast, Balanced and Deep for every connection, each mapped to a model you can change. A conversation can
  change engine; the new engine receives the last 20 messages.
- Your Claude outside Hokma: Hokma can reuse the access you already have (variables of your Claude settings,
  gateways, keys). It shows names and the host, never a secret value. Secrets stay in the secure store and reach
  Claude Code through `apiKeyHelper`, not through the environment of the agent's commands.
- Region blocks are explained, not worked around. When a provider refuses the location Hokma sees, the panel says so
  and offers to connect another engine.
- The protections are the same with every engine. A broker inside Hokma checks every command, file change, fetch and
  MCP call of Codex, Gemini and the direct engine with the same rules as the Claude Code hooks (delete, truncate,
  Hokma's own folders, force push, outside the work folder, secrets, real money). It denies unknown tools. In
  "automatic" mode the engine's own approvals stay on and Hokma answers after the broker.
- Engine processes receive an allow listed environment: no provider keys of other connections, no market keys, no
  Hokma token.
- The Hokma tools are real. `task_*`, `project_*`, `journal_*`, `ips_read`, `decision_propose`, `decision_list`,
  `schedule_create`, `schedule_list`, `extension_request`, `agents_list` and `bug_*` run on the same rules the
  panel shows, with Claude Code, Codex, Gemini and the direct engine. Decisions, routines and extensions wait for
  you. `agents/hokma.md`, `HOKMA.md`, Hugo and Axel stop naming robot and data tools that have no module yet.
- Database: migration 0006 adds `engine_connections` and `conversations.connection_id`. Nothing is removed. If you
  had Claude connected, it becomes your default connection and your conversations keep their history and resume.

## 0.0.8

Fixes reported by the community.

- A work folder on another drive (for example `D:`) is no longer blocked by the protection of Hokma's own folders on
  `C:`. The guard compares drive letters when the path comparison cannot decide: two different drives never nest, so
  Read and Edit work in `D:`. Anything ambiguous (mixed absolute and relative paths, a path with no drive) stays
  protected, and aliases and `subst` drives are still caught by the realpath and identity checks.
- The Hokma agent no longer promises tools that do not exist (`agents_list`, the task, journal, decision, schedule and
  IPS tools). `agents/hokma.md` and `HOKMA.md` tell it to read `agents/*.md` and the agents table, and to keep tasks,
  journal and decisions as Markdown notes in the fund folder until the real tools arrive. Only `trash_move` exists.
  The update brings the new text to an existing fund when you never edited those two files; if you did, your file
  stays and the new text is written beside it as `<name>.hokma-new`.
- When the Claude sign-in fails, the panel shows what `claude auth login` actually printed (the last lines, without
  terminal codes, link parameters or token-like strings) in Spanish or English, instead of staying silent.
- `Instalar-Hokma.cmd` no longer stops when Claude Code is missing. It installs Hokma, opens it, and then recommends
  Claude Code for chatting, which you can install later, like `install.ps1` and `install.sh` already did.

## 0.0.7

New conversation means a new conversation, one per browser tab, and a new conversation starts in General.

- New conversation opens a clean conversation in its own tab of the panel. The one you had stays where it was, with
  its history. The Chat entry of the menu goes back to the chat tab you used last.
- Every conversation has an address, `#/chat/<number>`, and New conversation is `#/chat/new`. New conversation and the
  rows of the list are real links: the wheel, Ctrl+click or Cmd+click open them in another browser tab. In Firefox and
  in private mode Hokma does not remember the session, so the new tab asks you to enter with the Hokma icon.
- Each browser tab keeps its own panel tabs and survives F5 without touching the others. The menu, the right panel
  and the terminal stay shared.
- The shortcut for a new conversation is Ctrl+Shift+O (Cmd+Shift+O on macOS). Ctrl+N belongs to the browser.
- A new conversation is General (no project, your working folder) and never inherits the last project you used. A
  picker above the first message lets you choose General or a project. Conversations you already had keep following
  the active project, as before.
- The projects panel has a General entry. Choosing it leaves no active project: the terminal and the conversations that
  follow the active project work in your working folder.
- Database: migration 0005 adds `conversations.project_mode` and `profile.no_active_project`. It changes no row.

## 0.0.6

Hokma works without asking, keeps a way back for everything it touches, updates from a button, and the chart gets its
prices over a live connection.

- One shortcut: Hokma. The Shut down Hokma and Restart Hokma shortcuts are removed on install, reinstall and update, and
  a Shut down Hokma button sits below Settings in the panel (it asks first and leaves a "Hokma is shut down" screen in
  every open tab). The entry screen has the same button for a browser without a session.
- Permissions: a "Do everything without asking" mode. Seven protections stay on in both modes with no switch:
  deleting, emptying files, Hokma's own data, force push, writing outside the working folder, stored secrets and real
  money orders. A refusal is logged and shown in the app.
- Trash and restore points: agents move files to a trash instead of deleting them, and the owner restores, deletes
  for good or empties it from Settings. Before each turn in the no-questions mode Hokma takes a restore point, so what
  the protections cannot see can still be brought back.
- Updates: Check for updates is always visible in News. Updating shows the real stage, then Hokma restarts itself and
  the open tabs reload on the new version. If it is not running under its supervisor, the app says to restart it.
  The supervisor restarts a crashed engine with growing waits and stops after repeated crashes, writing the reason to
  a log.
- Connections: one screen, `#/connections`, holds every data connection, with one card per provider, its state, its
  data type (live or delayed), the symbols in use, an on/off switch, "Test connection" and "Remove key". Keys live in
  the system credential store, never in a file, a log or the panel. Providers of the same asset class are ordered by
  priority. Nothing about providers or keys is left in Settings, first run, chat or the command line.
- Live prices: the chart receives bars and the last price over `WS /ws/market` instead of polling. Providers:
  Coinbase (crypto, no key), Alpaca (US stocks and ETFs), Finnhub (US stocks, forex, crypto), Databento and Massive
  (CME futures), each with the data state it declares. A symbol with no connected provider is served by a delayed
  fallback, labelled "Delayed", with a link to Connections.
- SiomTrading Futures: a futures provider with its own stream. The student pastes an access token on its card; the
  token travels only in the first message of the connection. It reconnects after a drop, stops with a clear message
  when the token is invalid, revoked or used from another device, follows contract rollovers, and shows the data type
  the server announces. It is tested against a fake server; the public address of the stream is set in
  `backend/hokma/market/providers.json`.
- Database: a migration adds the permission mode, the trash and the restore points; databases from 0.0.5 migrate on
  the first start.
- Session: the page no longer keeps the session token in the browser profile (only in the tab). The calls only the
  owner makes (permission mode, Trash, restore points) also need a confirmation the open tab registers once, held in
  memory by the page and the engine; a token read from disk is not enough. The protections also refuse reading the
  browser profiles and other processes' memory, environment and command line, renaming or linking `.git`, `.claude`
  and `.hokma`, and Git commands that delete refs or set a program to run.

## 0.0.5

The dashboard is now the full Hokma app, in a glass design with dark and light themes, in Spanish and English.

- Database: the app keeps its state in SQLite (`hokma.db` in the protected data folder) with versioned migrations, and
  can move to PostgreSQL and back with row counts verified. `owner.json` from 0.0.4 is imported on the first start.
- First run in five steps: owner and fund names, working folder, Claude connection, database and a final review.
- Chat: streaming conversations with the student's own `claude`, permission prompts, image and file attachments,
  voice notes transcribed locally, slash commands, projects, archive and unarchive, and a history that restores
  attachments and tool lines. A running turn keeps its text when the connection drops and comes back.
- Projects: create, switch and drop files into the active project. The active project lives in the database.
- Agents: list, create, edit, switch model and turn on or off. Choosing Opus asks for confirmation, and nothing
  uses Opus by default. A bar shows the weekly usage limit and turns orange above 80 percent.
- Routines: scheduled runs of an agent, created from the panel with human confirmation recorded in approvals.
- Chart: market charts from a public data source, fetched by the backend, with a question box that opens a chat
  with the chart image attached.
- Integrated terminal: a real pseudo-terminal per tab, protected by the session token.
- Learn, extensions, status and settings screens.
- Onboarding progress (first conversation, first project, first image) is stored in the database.
- One command runs every end-to-end suite: `npm run e2e` in `dashboard/`.
- Message catalogs for the dashboard live in `dashboard/src/i18n/messages/`.

## 0.0.4

- Updates work without a GitHub account; the repository is public. A GitHub login found on the PC is optional and a rejected one is ignored.

## 0.0.3

- Removed: license step from first run. The first start asks the owner name and the fund name, nothing else.

## 0.0.2

- The owner and the fund have names. The first start in a terminal asks for the owner name and the fund name
  (a license line says coming soon); the new command owner asks again. Answers are stored in owner.json in the
  protected data folder, never in the working folder. Without answers the agents say the owner and the fund.
- Kit sync fills the name markers before it compares files: untouched files are replaced with the filled
  version (including the first HOKMA.md with raw markers), files the owner edited stay and the new version
  is written beside them with the hokma-new suffix.
- Kit text calls the user the owner; the three messages that used the old word now speak to you.
- The kit brings docs/learn/README.md and HOKMA.md has no leftover notes.

## 0.0.1

- English-only code, comments and docs, checked in CI.
- Student-facing text moved to message catalogs (`backend/hokma/catalog`, `installer/messages`), Spanish by
  default and English available (`HOKMA_LANG`, `--lang`); docs folders renamed to English.

- Repository skeleton: FastAPI backend with `/health` and echo `/rpc`, empty Hokma dashboard,
  `hokma` CLI (start, doctor, version).
- Installer for macOS, Linux and Windows with sha256-pinned uv (`installer/lock.json`) and
  uninstallers; extended `hokma doctor`; installer tests; CI checks that `dashboard/dist` matches a fresh build.
- Security: runtime and Claude config live outside the working folder; generated `settings.json`
  denies Edit/Write/Bash on them; `hokma doctor` fails if they are inside the working folder.
- Security hardening: Claude sandbox (denyWrite), fail-closed PreToolUse path guard, settings.json
  regenerated at every Claude launch, sha256 manifest of app/ checked at start and by doctor.
- hokma update (blue-green runtime, sha256 + manifest checks, atomic switch, rollback), student extensions file that cannot disable the locks, kit updates that never overwrite student edits.
- Windows installer: `hokma.cmd` written as UTF-8 (supports accents and enye in user names); git calls
  no longer abort under PowerShell 5.1 on CRLF warnings.
