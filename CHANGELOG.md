# Changelog

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
