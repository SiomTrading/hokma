# Updates and automatic restart

How Hokma finds a new version, installs it and starts it again without the owner touching a terminal.

## The processes

`hokma start` is a light **supervisor**. It does three things:

1. Creates the session token once.
2. Starts the **engine** (`hokma engine`, an internal command) as a child process and writes the token to the
   engine's standard input. The token is never an argument, never in the environment and never in a file.
3. Starts the engine again when it ends: at once after an update, after a short wait after a crash.

The engine is the web server the browser talks to. It adopts the token from standard input before the application
is imported, so a restarted engine accepts the same token and the open browser tab keeps working. The only place
the token is printed is `hokma start --no-browser`, which prints the address with `#t=<token>&o=<one-time code>` so the owner can open it.
The one-time code is a second secret of the start (see `docs/permissions.md`): the supervisor keeps only its SHA-256 and
writes that hash to every engine it launches, on the third line of standard input, until a registration consumes it, and
through the hand over to a new slot. The fourth line is a key made for that engine alone, which signs what the engine
tells the supervisor on standard error.

## Exit codes the supervisor understands

| Engine ends with | Supervisor does |
|---|---|
| 75 | An update was installed: start the engine again at once, from the slot `current` names. Not counted as a crash. |
| 0, or a stop signal from the terminal | Stop. |
| 3 (integrity check) or 4 (database migration) | Stop and return that code. Running again cannot fix it. |
| anything else | A crash. Wait 1 s, then 2 s, then 4 s before starting it again. The fourth crash inside 60 s stops the supervisor. |

When it stops for crashes, the reason (the last lines the engine wrote to its error output, with the token masked)
goes to `<data>/logs/engine.log`, and the open tab shows "Hokma could not start" with that path. The log is capped
at 256 KiB and rotated to `engine.log.1`.

## Update flow

Since 0.0.14 (task 3146) the update never runs inside the server, nor in any process started from a runtime slot.
On Windows a file that a running process loaded (a `.pyd` of the server, the venv's Python) cannot be deleted, so an
update run from inside Hokma could not rebuild the inactive slot (`shutil.rmtree` failed with WinError 5).

1. Settings > News calls `POST /api/update`. The engine asks the update source whether there is something newer; an
   up-to-date Hokma answers so and keeps running.
2. Otherwise it starts the **detached updater** (`hokma.update.detached`): the `hokma` package and a small entry
   script are copied to a new temporary folder outside `runtime/a` and `runtime/b`, and run with the shared Python
   of `<data>/python`. Windows: DETACHED_PROCESS, CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW and
   CREATE_BREAKAWAY_FROM_JOB (the supervisor's job allows exactly that, `JOB_OBJECT_LIMIT_BREAKAWAY_OK`, so the updater
   outlives it); POSIX: a new session. The token and the owner hashes go on its standard input only.
3. The engine answers `{"status": "launched", "restarting": true}` and shuts down the way "Shut down Hokma" does,
   closing every WebSocket with code 1012 and the reason `updating`. Each tab shows "Updating Hokma, it will restart
   by itself" and asks `/health` once a second for up to 30 minutes.
4. The updater, in order: waits for the engine and the supervisor to end (asks the supervisor to stop if it is still
   there after 5 s), kills whatever still runs from `runtime/a`, `runtime/b` or `python` (as Instalar-Hokma.cmd
   does), removes a stale `update.lock`, runs `run_update` (download, unpack, build the inactive slot, verify,
   switch `current`, with the retries of 0.0.12), writes the outcome to `<data>/update-status.json` and starts
   Hokma again: a supervisor of the slot `current` names, on the same port, with the same session handed over on
   its standard input. When the update failed, `current` was not switched and the previous version comes back.
   The restart (0.0.16, task 3151) runs the slot's `pythonw.exe -m hokma_cli.main` on Windows, never `hokma.exe`:
   that is a console launcher, and started detached its Python child got a new, visible, empty console window.
   It is started with DETACHED_PROCESS, CREATE_NEW_PROCESS_GROUP and CREATE_BREAKAWAY_FROM_JOB (inside the job only
   when the job forbids leaving), its standard input is closed after the three session lines, and the updater then
   checks `/health`: an attempt that ends or does not answer in 60 s is cleaned up and tried again, 3 attempts in
   all. `updater.log` gets each command, pid, exit code, health answer and the last lines of `supervisor.out`; when
   no attempt answers, `update-status.json` gets `started: false` and the reason in `start_error`. A supervisor of
   0.0.16 that still finds itself in a console window (started by the updater of 0.0.15) hands the session to a
   `pythonw.exe` copy and ends, so the update to 0.0.16 closes that window too.
5. The tab sees `/health` again. A new version reloads the page on Settings > News with "Hokma updated to X". The
   same version means the update failed: the tab reads `GET /api/update/state`, which reports the message of the
   status file (phase `error`, for one day), and shows it with "Retry". A traceback only goes to
   `<data>/logs/updater.log`.

`hokma update` from a terminal: on Windows, when it is the `hokma` of a runtime slot, it uses the same detached
updater (it stops and restarts Hokma when it was running, with a fresh session) and returns at once, naming the
status file. Elsewhere, and in a development checkout, it updates in the same process as before.

## Decision: where the supervisor runs from (two updates in a row)

**Finding.** The supervisor is the `hokma` of the slot that was current when it started (the launcher in the
user's `bin` folder reads `current` and runs `<data>/runtime/<slot>/venv/.../hokma`). Python and its libraries for
that process live inside the slot: the slot's `venv` (its interpreter launcher, `hokma` launcher, site-packages)
and `app/` (the code). The base interpreter that uv downloads lives in `<data>/python`, outside both slots, and is
shared.

An update always builds the **other** slot and deletes it first if it exists. After one update the supervisor is
still the old slot's process while `current` points at the new one, so the next update would delete the slot the
supervisor is running from. On Windows that fails because the launcher executables and loaded files of a running
process cannot be deleted.

**Options.**

1. Run the supervisor from a stable place outside the slots. Rejected: the launcher that users already have (and
   that the 0.0.5 installer wrote) runs the slot's `hokma`. Running from a stable place would need a new launcher
   and a copy of the interpreter outside the slots, and a process that is not a child of the terminal's command
   on Windows (the command would return at once). It also adds files the installers must keep in step.
2. **Chosen.** The supervisor re-executes itself from the slot that `current` names, passing the token.

**How option 2 works.** Before every engine launch the supervisor compares the slot it runs from
(`hokma.core.config.running_slot()`, derived from the location of its own code) with `current`. When they differ
it hands over:

- POSIX: it creates a pipe, writes the token into it, closes the write end and `exec`s the `hokma` of the current
  slot with `start --port N --no-browser --handoff-fd FD`. The process keeps its id; only the number of the
  pipe is in the arguments. The new supervisor reads the token from the pipe and starts the engine at once.
- Windows: it starts the `hokma` of the current slot with `--handoff-stdin`, writes the token to that process's
  standard input and ends. The old slot is then free. One visible effect: the terminal prompt returns after the
  first update while the new supervisor keeps running in the same console window.

Invariant: at any moment the supervisor and the engine run from `current`, and an update only touches the other
slot. The handover happens in the seconds between the engine exiting with 75 and the next launch, long before
another update can start. If the hand over is not possible the supervisor logs it and keeps running (the next
update would then fail at the delete step on Windows with a readable error and the current version intact).

## Closing the terminal

- Windows: the supervisor creates a Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` (through `ctypes`, no
  dependency) and puts each engine in it. When the supervisor ends, however it ends, Windows closes the last
  handle and kills the engine and everything it started.
- POSIX: the engine watches its standard input, which is a pipe from the supervisor. When the supervisor dies the
  pipe closes, the engine stops its server and exits (and forces the exit after 5 s). A closed terminal also sends
  hang-up to the whole process group.

## Searching for updates

The engine searches for a new version about 5 s after it starts and every 24 h, and remembers the time of the last
successful search in `profile.update_checked_at` (column added by migration `0003`). The screen shows "checked N
min ago" from that value, and Settings shows a dot next to News while a newer version is known. The search itself
asks the same source as before (`HOKMA_UPDATE_SOURCE` or the latest GitHub Release). Nothing is installed without
the owner pressing "Update Hokma".

## What is covered by automatic tests

Linux, with a fake update source and a stub of `uv`: restart after update with the same port and token, two
updates in a row, sha mismatch, one crash, four crashes in 60 s, closing a terminal (pseudo-terminal) and killing
the supervisor, token absent from arguments, environment and files. The Windows and macOS variants of the same
cases (Job Object behavior, hand over through standard input, the "file in use" error on delete, terminal close)
are written as unit tests that skip outside their system and need a run on those systems.

## Shortcuts

Hokma creates one shortcut and nothing else: **Hokma** (`hokma start`). Shutting down is a button inside the panel,
**Shut down Hokma**, below Settings in the left menu (in Spanish: Apagar Hokma). It asks first, then ends the engine the
way `hokma stop` does and every open tab shows "Hokma is shut down". "Hokma" opens
`http://<own host>:{port}/` in the browser (the fixed `hokma.localhost` name described in `permissions.md`). There is no
start at login, no console window and no tray icon, and the shortcut points at the stable launcher
(`~/.local/bin/hokma[.cmd]`), never at a runtime slot that an update replaces.
The code is `hokma.core.shortcuts`; the names come from `installer/messages/*.properties`. The old **Shut down Hokma**
and **Restart Hokma** shortcuts of earlier versions (in Spanish: Apagar Hokma, Reiniciar Hokma) are deleted by every
install, reinstall and `hokma update`; the keys `shortcut.legacy.stop` and `shortcut.legacy.restart` exist only for that.

| System | What is created | Where |
|---|---|---|
| Windows | `.lnk` that runs `wscript.exe` on `hokma-hidden.vbs` (in the `bin` folder of the data folder), which starts `hokma.cmd` with a hidden window | Start menu and Desktop |
| macOS | AppleScript applications built with `osacompile` (`do shell script`, no Terminal) | `~/Applications/Hokma/` |
| Linux | `.desktop` files with `Terminal=false` | `$XDG_DATA_HOME/applications/` |

The installers run `hokma shortcuts --force`. `hokma post-update` runs the same code for the missing ones only, so an
owner coming from an older version gets the new one and loses the old ones; a failure there prints a warning and never
fails the update. An old shortcut that cannot be deleted is listed in a warning and the exit code does not change.
`hokma shortcuts [--force]` can be run by hand. The uninstallers remove the shortcuts in both languages and leave the
working folder alone. A failure to create a shortcut is never fatal for an install.

### Shutting down from the panel

`POST /api/system/shutdown` (`hokma.core.shutdown_api`) answers `202 {"stopping": true}` and then runs
`Lifecycle.shutdown()`: every WebSocket closes with code 1001 and the reason `shutting_down`, and the engine asks its
supervisor to stop through the control channel (the path of `hokma stop`). Two forms:

- With a session: `X-Hokma-Token`, Host and Origin of the real port, `Sec-Fetch-Site: same-origin`. It does not need
  the owner confirmation, so a tab that lost it can still shut down and come back with the icon.
- Without a session (the entry screen of a browser that has none): Origin of the engine, a single-use nonce from
  `GET /api/system/shutdown-nonce` (60 seconds, bound to the client address and User-Agent), no WebSocket open
  anywhere, no running turn, and one accepted call every 30 seconds. If someone has Hokma open with a session the answer
  is `409 shutdown.in_use`.

An agent cannot name the route: `hardblock` refuses it, including percent-encoded and dot-segment forms.

## Running in the background

`hokma start` launches the supervisor detached from the terminal and returns once `/health` answers; the terminal,
the tab and the browser can be closed and Hokma keeps running. There is no start at login: Hokma starts when the
owner opens it.

| Command | What it does |
|---|---|
| `hokma start` | If `<data>/engine.lock` names a live supervisor, checks that the listener on the port is this engine (`/api/engine-proof`; if not, it prints a warning and opens nothing), opens `http://hokma.localhost:{port}/` (no secrets) and ends. Otherwise starts a detached supervisor (Windows: `pythonw.exe` of the slot; POSIX: new session, standard input from `/dev/null`, output to `<data>/logs/supervisor.out`). The supervisor opens the browser with the one-time address (on `127.0.0.1`) itself. |
| `hokma start --foreground` | The supervisor in this terminal. `--no-browser` and the hand over options imply it. |
| `hokma stop` | Asks the supervisor, on the local control channel, to stop. It stops accepting turns, saves what turns in progress received, ends the planner and the terminals, runs `PRAGMA wal_checkpoint(TRUNCATE)` and exits 0. After 10 seconds the supervisor closes the job (Windows) or the process group (POSIX). |
| `hokma restart` | `stop`, then `start`: a new session and a new one-time code. |

**Ports.** Without `--port` the first free port from 8765 to 8775 is used (exit code 5 when none is free); Host and
Origin accept only that port and `engine.lock` names it. An explicit `--port` is used as given.

**Control channel.** POSIX: `<data>/control.sock`, mode 0600, peers of another user are dropped. Windows: the named
pipe `\\.\pipe\hokma-control-{user}` with a DACL for the current user only. The supervisor understands `stop` and
`status`; its answers hold a pid, a port, a start time and the engine state, never the token or the code.

**Killed supervisor.** Windows: the job ends the engine and its children at once. POSIX: the engine watches its
standard input and ends within 5 seconds.
