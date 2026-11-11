# Permissions, always-on protections, trash and restore points

Hokma has two permission modes, `ask` and `auto` ("Do everything without asking"). Four locks apply in both, with no
switch in the app, plus the protection that keeps them on (task 3169; before 0.0.22 there were seven rules). What is
not listed below is the owner's and the agent's to do: install dependencies (pip, npm, uv, bun), software (Node, npm,
npx, pip, winget, brew), plugins, skills and MCP servers from the chat, change Hokma's configuration and permission
settings, Git without limits inside the working folder.

| Rule | What it refuses |
|---|---|
| `real_money` | MCP tools named like `place_order` / `submit_order`, and hosts that place real orders (`hardblock_brokers.json`) |
| `secrets` | The keys stored for Connections (service `hokma-market`) and the remembered session (service `hokma-session`, read or write), Hokma's database (`database.json`, `hokma.db`), `identity.key`, `codex-home`, `acp-home`, `backups` and the Claude sign-in files in `claude-config` (`.credentials*`, `.claude.json`, `backups`), `~/.ssh`, `~/.aws`, `~/.config/gh`, `~/.git-credentials`, `keyring`, `cmdkey /list`, `security find-*-password`, `git credential`, `gh auth token`; the session token header and the owner and session calls of Hokma's API (`X-Hokma-Token`, `X-Hokma-Owner`, `/api/session/owner-nonce`, `resume`, `enter`, ...) and the control channel and lock of the engine; the profile folders of Chrome, Chromium, Edge, Brave, Firefox and Safari (also snap and flatpak) and their `Local Storage`, `Session Storage`, `IndexedDB`, `Local State`, and the cookie stores by name or by search; `/proc/<pid>/mem`, `environ`, `cmdline` of other processes (`/proc/self` stays free). Also a recursive read (`grep -r`, `rg`, `tar`, `cp -r`, `zip`, `find -exec`) over a folder that contains Hokma's data folder. The rest of Hokma's data folder (plugins, settings, logs, tools) is readable |
| `delete` | Deleting files or folders, including quoted, encoded and wrapped forms (`eval`, `base64 -d \| sh`, `bash -c`, `python -c`, `node -e`, `-EncodedCommand`, scripts of the working folder that call delete APIs). The installer idiom that feeds a shell with a download (`curl url \| sh`, `bash -c "$(curl url)"`, `iex (irm url)`) is allowed: it is how software installers run |
| `outside_workdir` | Writing outside the working folder and Hokma's data folder (with its `claude-config`, plugins and tools); formatting or touching disks. What a software installer writes on its own (npm, pip, winget, brew) is not seen by the hook and is not refused |
| `hokma_internal` | Changing the files of the hooks (the `hokma` package they run from, the log folder `<data>/guard`) and the settings files that register them (`settings.json`, `settings.engine.json`). `settings.json` may be edited with Edit or Write as long as the result still registers both hooks (`guard.registration_intact`) |

There is no sandbox and no `permissions.deny` list: a sandbox would stop the installers and the first-start downloads of
plugins from writing where they must.

What an agent does instead of deleting: `trash_move(paths, reason)` from the `hokma` MCP server. The owner restores,
deletes for good or empties the trash from Settings > Trash (session token plus confirmation). Before each turn in
`auto` mode a restore point is taken (`refs/hokma/checkpoints/*`), so what the list cannot see can still be brought back.

## How it is wired

- `backend/hokma/kit/hardblock.py` is a PreToolUse hook with matcher `.*`, launched like `guard.py`: absolute path,
  `python -I`, from the data folder, `|| exit 2`. It fails closed. A refusal prints `HOKMA_BLOCKED <rule>` on stderr and
  appends a line to `<data>/guard/blocks.jsonl`; the engine stores it in `guard_events` and sends `guard_block`.
- The settings template pins `"disableAllHooks": false`; `claude` is launched with `--settings <generated file>` and
  `--setting-sources user`. The generated file is rebuilt at every launch.
- The engine process and the terminal get a PATH that starts with Hokma's tools (`hokma.core.tools`): `<data>/bin` (uv),
  `<data>/tools/node`, `<data>/tools/bun`, the Python uv installed, then `~/.bun/bin` and `~/.local/bin`. Node and Bun
  come from `installer/lock.json` (sha256 checked before unpacking) through `hokma install-tools`, which the three
  installers and `hokma update` run. The environment filter keeps the variables installers need (`BUN_INSTALL`, the npm
  prefix and cache, `CARGO_HOME`, `GOPATH`, ...).
- All the files are in the sha256 manifest, so changing `hardblock.py` or `hardblock_brokers.json` in an installed copy
  stops Hokma from starting.
- The permission mode is `profile.permission_mode`; only `PUT /api/profile/permission-mode` (session token plus the
  owner confirmation, below) writes it.

## The session token and the owner confirmation (bug 1035)

The token an agent must not have is the session token. Before, the page kept it in `localStorage`, a file in the
browser profile that any process of the same user can read. Three layers now:

1. **Where the token lives.** The page keeps it in memory and in `sessionStorage` (one tab, gone when the tab closes).
   Never `localStorage`, never a cookie. An older copy in `localStorage` is removed at load. The `#t=` start link and
   the automatic restart after an update work as before (the same token, the reload of the same tab keeps its
   `sessionStorage`). `dashboard/e2e/session.e2e.mjs` opens Hokma in a real Chromium with a persistent profile, uses
   it, reloads, closes, and scans every file of the profile for the token (a control value in `localStorage` proves the
   scan sees what is there). Some browsers write `sessionStorage` to the profile for session restore, so this layer
   alone is not the defense; it is the first of three.
2. **Reading the profile is refused.** The `secrets` rule refuses commands and tools that read, list or search the
   profile folders of the main browsers, and other processes' `/proc/<pid>/mem`, `environ`, `cmdline`.
3. **The owner confirmation.** These calls need `X-Hokma-Owner` besides the token: `PUT /api/profile/permission-mode`,
   `POST /api/trash/{id}/restore`, `POST /api/trash/{id}/purge`, `POST /api/trash/empty`,
   `POST /api/checkpoints/{point}/restore-file` and `restore-deleted`. Without it, or with a wrong one, the answer is
   `403` with `{"detail": {"code": "owner_confirmation_required"}}`. A token read from disk is not enough.

How the confirmation works:

- The tab makes 128 random bits at load (`crypto.getRandomValues`) and registers them once with
  `POST /api/session/owner-nonce` (token and one-time code required, below). It keeps them in a variable of the page. They are never in
  `localStorage`, a cookie, a URL, a file, an argument, the environment or a log. Only when the page is leaving
  (reload, update restart) `pagehide` copies them to `sessionStorage` and the next load of that tab takes them back and
  deletes them at once.
- **Who may register (task 3045).** Holding the token is not enough: a first-come registration could be taken by an
  agent that read the token. `hokma start` makes a second secret of 128 bits, the one-time code, and prints it next to
  the token in the start link: `#t=<token>&o=<code>`. The tab takes both from the address at load, removes them from
  the bar (`history.replaceState`), keeps the code in a variable of the page only (never `sessionStorage`,
  `localStorage`, a cookie, a file, an argument or the environment), sends it once in the body of the registration
  (`{nonce, once}`) and drops it when the engine accepts. A registration without the code, or with a wrong one, gets
  `403 owner_secret_required` and records nothing.
- The supervisor keeps only the SHA-256 of the code and hands that hash to each engine (third line of its standard
  input, and in the hand over to a new slot) for as long as no registration has consumed it. The engine compares with
  `secrets.compare_digest` and forgets the hash at the first use, so the code works once. An engine that was never
  given a hash refuses every first registration.
- The engine keeps only the SHA-256 of the confirmation, in memory, and compares with `secrets.compare_digest`.
  Registration is accepted once per start of Hokma: the tab with the code wins and the same value is accepted again
  without the code (a reload); any other value gets `409 owner_already_registered`.
- **Restarts.** The engine tells the supervisor the hash on its standard error, and the supervisor writes it as the
  second line of the next engine's standard input, next to the token. A child process of the engine (a command of an
  agent) writes to the same standard error, so the note is signed: each engine launch gets a fresh key from the
  supervisor (fourth line of its standard input, memory only) and the note is
  `HOKMA_OWNER_HASH <hash> <HMAC-SHA256 of the hash under that key>`. The supervisor takes ONE valid note per engine
  and only while it holds no hash; it never overwrites a hash it has, and every other line that carries the marker is
  dropped (never shown, never taken). The same hash goes along in the hand over to a new slot after an update. So
  after a restart the engine already knows the hash, the open tab keeps working with no new registration, and there
  is no moment in which the place is free for someone else. Closing the terminal and running `hokma start` again gives
  a new token, a new code and a new start (the tab that opens with the new link registers first).
- A tab opened with only the token can read, but its first registration gets `owner_secret_required`: open Hokma with
  the link that `hokma start` printed.
- If the engine runs without the supervisor (a dev checkout) and restarts, it forgets the hash: the tab gets
  `owner_confirmation_required`, registers again by itself and retries the call once.
- A tab that was closed has lost both values and the token with them: that is the cost of keeping nothing at rest in
  the page. The remembered session (next section) gives them back without the page storing anything.
- The hard-block rule `hokma_internal` also refuses commands that name these calls, so an agent that learns the route
  is stopped before it tries.

## The remembered session

Closing the tab or the browser, or pressing F5, and going back to the address of the panel opens Hokma with its
session. It does not bring back bug 1038: the page still writes the token, the confirmation and the code in no storage.

**What is kept, and where**

| What | Where | Who can read it |
|---|---|---|
| Cookie `hokma_resume` (256 bits, base64url) | The browser, as an `HttpOnly; SameSite=Strict; Secure` cookie with `Path=/api/session`, no `Domain`, 30 days from the last use, set for the name `hokma.localhost` | Only the browser. The page cannot read it (`document.cookie` does not show it). The browser keeps it encrypted with the key of the system, except Firefox |
| Name of the panel | Fixed: the browser reaches Hokma as `hokma.localhost:<port>` (usually `http://hokma.localhost:8765/`). Nothing is stored for it | An older install used a random `h-<hex>.localhost` name; a browser that still has a session there simply enters again with the one-time code, with no error |
| Private key (ECDSA P-256) | The browser: WebCrypto, not extractable, in IndexedDB, which the browser isolates by origin (port included) | Only that origin of that browser. It never reaches the engine |
| Verifier `{lineage, gen, hash, prev_hash, prev_until, created_at, last_used_at, pub_x, pub_y}` | The credential store of the system (`keyring`, service `hokma-session`, entry `resume`). Without a usable store, the memory of the supervisor only | The engine. It keeps the SHA-256 of the cookie, never its value, and the PUBLIC key of the browser |
| Token and owner confirmation | Variables of the page, as always | The page |

**How it works**

Why a name and a key. Cookies ignore the port: a server an agent starts on the same name and another port receives the
same cookie. So the cookie never works alone. The name `hokma.localhost` is the same for everyone, so it does not protect anything by itself: every
`resume` needs a signature made with a private key that the browser keeps per origin, so the other server has the
cookie and not the key (the key is bound to the origin, port included, so `hokma.localhost:<other port>` cannot use it).

1. **First entry.** `hokma start` opens `http://127.0.0.1:<port>/#t=<token>&o=<one-time secret>`. This numeric address is
   the only place that carries the secrets of the link, and they are used once. The page registers its owner
   confirmation (`POST /api/session/owner-nonce`, which never issues a cookie), creates the ECDSA key pair in the
   browser, and asks `POST /api/session/handoff-code` (token and owner confirmation). The engine answers
   `{code, host}`: a code of 16 characters (80 bits, base32 in capitals), valid 60 seconds, one use, kept only as a hash,
   and the name `hokma.localhost:<port>`. A new code cancels the previous one.
2. **Enter.** The page goes to that name and calls `POST /api/session/enter {code, nonce, public_key{x, y}}` there (a
   call at 127.0.0.1 is refused). The engine spends the code, stores the public key, makes the lineage, sets the cookie and
   answers `{token, persistent}` (`persistent: false` means the verifier could not be saved in the credential store and
   lives in memory). A wrong, expired or used code is `401 session.resume_denied` and gives no token. Firefox does not use
   this route.
3. **Later entries.** A page that loads at `hokma.localhost` with no token in memory calls
   `POST /api/session/challenge` (32 bytes, one use, 30 seconds, at most 8 pending) and then
   `POST /api/session/resume {nonce, challenge, signature}` with the cookie. The signature is ECDSA P-256 SHA-256 over
   the text `hokma-resume:<origin>:<challenge>:<nonce>` (the origin is the exact one the page loaded from, with its port, for example `http://hokma.localhost:8765`), 64 bytes `r || s` in base64url. Both calls need the Host of this
   installation and `Sec-Fetch-Site: same-origin`. The engine spends the challenge in every case, checks the shape of the
   cookie, and checks the signature against the public key of the lineage the cookie belongs to, rebuilding the origin from the Host of the request. The public key is stored once, when the lineage is made with a one-time code, and a signature by any other key changes nothing. A signature made on another origin (another port) does not verify. Only then does it rotate.
   A cookie without the key gets `401 session.resume_denied`: nothing is rotated or revoked, the cookie is not cleared, and
   a `guard_events` row (`guard='session'`, `blocked`, at most one every 30 seconds) is left. A copy of the cookie cannot
   lock the owner out.
4. **Rotation.** Every successful `resume` gives a new cookie value. The previous value is accepted once for 10 seconds
   (two tabs that load at the same time). The page calls `resume` again every 10 minutes with the same confirmation, and
   again when it finds the engine back after a lost connection (an update restart), which gives the confirmation of the
   owner back to the lineage.
5. **Revocation.** An old value used outside that window, or a second time, WITH a valid signature, means a copy of the
   cookie and the key was used. The engine deletes the verifier, forgets the owner confirmation of the lineage, writes a
   `guard_events` row with `guard='session'` and answers `401 session.revoked`. The next time the owner enters, the entry
   screen says: "Hokma closed the saved session because an old copy was used. Enter again with the Hokma shortcut". An open
   tab that finds the revocation in its 10 minute refresh goes back to the entry screen.
6. **Close the session of this browser.** Settings, General, "Close the session of this browser" calls
   `POST /api/session/logout` (an owner call: token and `X-Hokma-Owner`). The verifier is deleted, the cookie expires and
   the page goes to the entry screen.

`hokma start` on an engine that is already running (the "Hokma" shortcut does this) first checks who listens on the port:
it sends a fresh question to `GET /api/engine-proof?nonce=...` on `127.0.0.1:<port>` and the engine answers with an HMAC
made with `<data>/identity.key` (32 random bytes, mode 0600) over the port and the nonce. If the listener does not
answer correctly, Hokma prints "Another program is using the Hokma address" and does not open the browser. Only then it
opens `http://hokma.localhost:<port>/` with no secrets: the saved session enters by itself, or the page shows the entry
screen. The name `hokma.localhost` is accepted only
on a connection whose peer and local address are both the loopback; any other connection gets `403` (HTTP and
WebSocket). `127.0.0.1` and `localhost` keep working as before.

**Firefox.** Firefox keeps cookies unencrypted in `cookies.sqlite`, so Hokma does not remember the session there: the page
does not call `enter`, no persistent cookie is issued, and Settings > General says to use Chrome, Edge or Safari. The
protection in that browser is the entry screen, not a stored credential.

**No session in this browser while Hokma runs** (a new browser, cleared cookies, a revoked session). The entry screen has
a **Shut down Hokma** button: shut Hokma down there and open it again with the Hokma icon. The engine accepts it only
when nobody has Hokma open with a session (see `updates-and-restart.md`, "Shutting down from the panel"). There is no
"Restart Hokma" shortcut any more.

**Without a credential store** (a Linux computer with no secret service): the verifier lives in the memory of the
supervisor. Closing and reopening the browser works; restarting the computer asks to enter again with the Hokma
shortcut. Settings > General says so when the engine reported `persistent: false` at the entry of this page
load. The engine exposes nothing else about it, so a page that resumed from the cookie does not show that notice.

**Residual risk of the fixed name.** `hokma.localhost:8765` is predictable. If Hokma is off, another program that runs
on this computer outside Hokma can take port 8765 and so the same origin, and the key of the browser for it. Three
things limit it: the "Hokma" shortcut and `hokma start` refuse to open the browser on a listener that is not this
engine; the signature includes the origin with its port, so a page served from 8765 cannot sign for an engine that
later runs on 8766; and the Shut down Hokma button ends the supervisor, the engine and every agent (tested: no process of the
session is left and the port can be bound). A page opened by hand on a bookmark, without the shortcut, while an
impostor holds the port, still shows the impostor's page. Residual and accepted: it needs a hostile program already running
on the computer while Hokma is off, outside the model of what Hokma agents can do.

**Residual risk of the shutdown route (class B, defense in depth).** `hardblock` refuses an agent command, script or path
that names `/api/system/shutdown`, also after percent-decoding until it stops changing, collapsing slashes, removing dot
segments as RFC 3986 does (any non-empty segment counts) and lowercasing; as a backstop it refuses text where `api`,
`system` and `shutdown` appear in that order within a short distance once whitespace is removed. Any other way to build
the address (string concatenation or encodings assembled at run time inside a script, another tool that the parser does
not read) is a known residual variant and is recorded in task 3030; it is not chased one by one. Two facts bound the
damage: the call without a session needs a single-use nonce (60 seconds, bound to the client address and User-Agent)
and is refused while anyone has Hokma open with a session or a turn is running, and a local process that can read the
entry page can already end processes of the same user on the computer. Shutting down deletes nothing and reveals no
secret; the routines stop until the owner opens the Hokma icon.

**Residual risk.** A process of the same user that decrypts the cookie of the browser and uses it before the owner does
enters with the powers of the owner until the next legitimate refresh (10 minutes at most while a tab is open). At that
moment the lineage is revoked and the `guard_events` row is left. On Windows, Chrome and Edge bind the cookie to the
application; on macOS the key is in the Keychain, which asks permission for another application. Firefox has no such
protection. The `secrets` rule refuses commands that read the `hokma-session` service or the cookie files of the
browsers, and `hokma_internal` refuses commands that name `/api/session/resume`, `/api/session/logout`, `/api/session/challenge`,
`/api/session/enter` and `/api/session/handoff-code`; both are
defense in depth. The structural protection is the cookie that the page cannot read, its rotation and its revocation.

`dashboard/e2e/resume.e2e.mjs` runs these cases in a real browser with a persistent profile; `session.e2e.mjs` keeps
checking that nothing of the session reaches the profile on disk.

## R1: does exit code 2 block under `bypassPermissions`?

Measured with the real CLI by `python scripts/r1_probe.py` (also `backend/tests/test_r1_real_cli.py`, which skips itself
when the CLI is missing or not signed in). The probe asks the model to delete `victim.txt` with the Bash tool and checks
that the hook refused and the file is intact.

| Check | CLI | Linux | Windows | macOS |
|---|---|---|---|---|
| 1. Exit 2 blocks with `--permission-mode bypassPermissions` | 2.1.318 | pass | pending, run by QA/owner | pending, run by QA/owner |
| 2. Exit 2 wins over another PreToolUse hook that answers `allow` | 2.1.318 | pass | pending, run by QA/owner | pending, run by QA/owner |
| 3. A `disableAllHooks: true` in `<work>/.claude/settings.local.json` does not switch the hook off (`--settings` with `disableAllHooks: false`, `--setting-sources user`; also with `user,project,local`) | 2.1.318 | pass | pending, run by QA/owner | pending, run by QA/owner |

The probe also runs a fourth row: under the real CLI the `hokma` MCP server starts and `trash_move` runs in `default`
mode without a prompt (Linux, 2.1.318: pass).

The flag is `--setting-sources` (values `user`, `project`, `local`) next to `--settings`. No fallback design is needed.
Run on Windows and macOS: `python scripts/r1_probe.py` with the same CLI version and paste the table here.

## Hook speed (test P11)

`backend/tests/test_hardblock_hook.py::test_p11_the_hook_is_fast_enough` runs the hook as a separate process 30 times
and requires a median of 150 ms or less. Linux, Python 3.12: median about 36 ms, maximum about 44 ms. Windows is
pending, run by QA/owner (process start is slower there; run the same test).

## Git

Git is free inside the working folder. The hook only refuses `git credential` (a way to read stored credentials) and the
places Git could write outside the allowed folders: `-C <path>`, the destination of `clone` and `init`, the path of
`worktree add`.

## What the hard block is, and what it is not

The hard block is defense in depth. In no-prompt mode the agent can run arbitrary code, so the integrity of the harness
rests on the structural protections, not on parsing every command:

1. **The settings are rebuilt at every launch** and passed with `--settings`, so an edit of the registration does not
   survive a restart; `guard.py` also refuses, during the session, an edit that would take the hooks out of it.
2. **Owner-only endpoints.** The mode change, the Trash and the restore point restores need the session token and the
   owner confirmation, which one tab registers once per start with a one-time secret (above).
3. **The integrity manifest.** The sha256 manifest of the installed app is checked at every launch and by `hokma doctor`
   (`check_integrity`).
4. **Restore points.** Git refs of the project (`refs/hokma/checkpoints/*`), trusted only when the commit SHA matches the one
   in Hokma's own database.

Further parser findings are classified B unless they break one of these protections.

## Residual risk

The locks read the text of a command and of the scripts in the working folder. A script that builds the name of a
delete call at run time (for example `getattr(os, 're' + 'move')`) is not on the list, and with no sandbox a command can
empty or remove a file in ways the text rules do not see (`sed -i d f`, `awk '' > f`, `tar --remove-files`,
`rsync --delete`, `python -c "open('f','w')"`). The restore point taken before each turn in `auto` mode shows the file as
gone and brings it back (test P10), and the trash is the net for what the agent moves with `trash_move`.
