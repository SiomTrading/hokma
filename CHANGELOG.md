# Changelog

## 1.0.12

- New: Desk core: the signal contract (a conviction from -1 to +1 with an abstention that is never neutral), the graph and state of a desk run, common prompt rules, skills with a versioned format, safe handling of tickers and provider responses, and a hash of the content of a snapshot that ignores generation dates.
- New: Integrity checklist for this release: the lock resolves on the five supported platforms, no file outside a module imports a quantitative or data library, every blocked function raises and names its replacement, every quant call has size and time limits, and the repository rules check passes.
- New: Lessons: a lesson is saved only from a decision whose order executed, measured from the real entry fill, per desk and general after approval.
- New: Multi model layer: Hokma starts with any engine you already use and extracts structured answers from any of them.
- New: `hokma quant list` and `hokma quant run` print any registered function over a file of returns.
- New: Hokma has its own metrics, risk, validation and data modules. Agents ask for a Sharpe, a VaR, a probabilistic Sharpe or a point in time snapshot through registered functions and get the number with its method, parameters and source. The functions of the underlying libraries that give wrong numbers are blocked and name the function that replaces them.
- New: Full SWO desk (H1 to H3): core, execution with a simulated broker and the options desk; the chat tells the owner the impact when an order of theirs leaves the fund short.

## 1.0.11

- Fixed: with the market closed, the chart header shows the regular session as thinkorswim does (the close and its own change, for example 608,45 -5,67%), never the bid. The after-hours trade appears next to it as a second figure.
- Fixed: on a closed market the badge says "Market closed" while the connection stays alive; it goes back to live by itself when Schwab opens a session.
- Fixed: market hours of options and futures. When Schwab refuses the single-market route, Hokma asks the same market in the list route. "Check Schwab data" now saves both routes to compare them.
- Fixed: the chart shades pre-market and post-market with the pieces of the day that Schwab names, also for the days before today that are on screen. If Schwab gives no hours for a day, that day is not shaded.
- Fixed: Last, Bid and Ask of the option chain were N/A on every contract. Schwab sends them as bid, ask and last; Hokma looked for other names. Out of hours the chain shows the last values Schwab holds, and a stream message that lacks a field no longer erases it.
- Fixed: with the market closed, intrinsic value, in the money and Prob. ITM are measured against the bid of the underlying, as thinkorswim does (call 611: 6,50; put 618: 0,50). With the market open they use the last price.
- Checked: the expirations list has every date Schwab gives, 23 NOV included. "Check Schwab data" now also saves volatility, theta, delta and intrinsic value of each contract to compare Prob. ITM and theta with thinkorswim.
## 1.0.10

- Live data by streaming in every source. Schwab now uses its Streamer: price, candles and the option chain update by themselves, with the values Schwab sends. Nothing polls.
- One source of data per user. With Schwab connected, Hokma shows only Schwab; without a broker, Alpaca (now "Alpaca Trading"). If the broker stops, Hokma says so on screen and by Telegram, and never shows data from another source. Crypto is always Coinbase; Finnhub is for forex and news.
- The day candle, the sessions (pre, regular, post) and the market hours are the ones Schwab sends. Hokma keeps no schedule of its own.
- The option chain asks Schwab for every strike of an expiration once, shows the strikes around the price of the stream and moves each cell with every message.
- Connections: "Check Schwab data" in the Schwab menu saves what Schwab sends, with no key or account in it, so the chart and the chain can be compared with thinkorswim.
- Futures by Schwab when it is connected (SiomTrading Futures when it is not). Interactive Brokers is ready to be added. Alpaca option quotes arrive by stream. The chart is shaded by the pieces of the day the provider names.
- Alpaca: when you connect it, Hokma tells you once that its free plan has limited data and is not optimal for options.

## 1.0.9

- Connections: one Add button. Pick a provider, enter its details, Hokma tests it and adds it. Below, the connected sources and the order of use for each kind of data. Everything you had connected stays as it was.
- Schwab: sign in with your own Schwab app. Live stock and ETF prices, candles, option chain with Schwab greeks, and a read-only view of balances, positions and open orders. Hokma never sends, changes or cancels orders. The session lasts 7 days and Hokma tells you one day before.
- Agents: every read endpoint of the Schwab API as read-only tools (quotes, option chains, expirations, price history, movers, market hours, instruments and fundamentals, accounts, orders, transactions, user preferences), plus historical volatility, portfolio greeks, day exposure and result, and account activity (fees and average fill price). `options_chain` uses Schwab when it is connected.
- Connections: each provider has a short guide with what it gives, its cost, where to get the key and what to do if the test fails.
- Changed: Hokma's built-in broker is called "Hokma". If you had it as "Hokma Simulated", it is renamed and keeps its costs and accounts.

## 1.0.8

- Changed: Databento and Massive are no longer offered. Futures use SiomTrading Futures, with Yahoo as backup. A key saved for them is removed from your system credential store when Hokma starts.
- New: Option chain of US stocks and ETFs from the Alpaca IEX key you already saved, with strike, expiration, bid, ask, last, volume, open interest, greeks and IV. On the free Alpaca plan prices are indicative and greeks are estimated, and the screen says so.
- New: Agents get a read-only tool `options_chain` that filters contracts by days, delta, open interest and spread.

## 1.0.7

- New: Learn has 14 new step-by-step guides: engines, basic plugins, Telegram, Gmail, routines and premarket briefing, Fund and desks, broker and capital, troubleshooting, voice, library and documents, backup and trash, data connections, permissions and security, and GitHub.
- Fixed: The Desks guide describes desks as they work today.

## 1.0.6

- Fixed: On Mac, closing a Hokma terminal ends only its own processes; it no longer closes your browser or other apps.
- Fixed: Codex works with Codex 0.170 and newer, the first sign-in works, and Codex errors show their reason and go to the log.
- Fixed: Deleting a desk shows the name in quotes, accepts extra spaces and says what to close when something blocks it.
- New: Skills that come with a Python package (like graphify) install with Hokma's own tools.
- Fixed: The Windows installer closes a Hokma that is still open before replacing it, and installs where every program runs as administrator.

## 1.0.5

- New: Fund is your home: total Capital across your desks, the Today card (watchlist of all desks, economic calendar, market news, premarket briefing) and every desk with its account and broker. Ask Hokma in the chat to change what Today shows.
- New: Premarket briefing routine ready to turn on from Tasks, delivered by Telegram, and "Run now" for any approved routine.
- New: A routine missed while your computer was off runs once when Hokma opens; later than its window, Hokma asks you on Telegram.
- New: Broker tab: Hokma's broker on real prices with editable commission and slippage; create accounts, reset them and assign one per desk.
- New: Gmail with an app password: search and read mail as quoted data, send only with your permission, and routine notices by email.

## 1.0.4

- New: Desk agents read company data from your connections: profile, news, earnings calendar, EPS surprises and key financials.
- Changed: Charts and agents use your connected data providers for candles, in the order you set in Connections; Yahoo is only the last fallback, and the chart shows the real source.
- Fixed: Deleting a project or a desk keeps its journal archived instead of failing.
- New: Reorder the desk watchlist by dragging (or Alt+arrows); the order is saved.

## 1.0.3

- Fixed: Hokma checks updates and connects to GitHub, Telegram and web pages with your computer's certificate store, so antivirus or company networks that inspect secure connections no longer break updates.
- Fixed: If an optional part of Hokma crashes on your computer at start, Hokma starts without it, tells you which one, and never stops at a black screen.
- Fixed: Click an asset of the desk watchlist to see its chart (also with the keyboard).
- New: Delete a project: its folder goes to the Trash and can be restored; its conversations stay.

## 1.0.2

- Fixed: The desk Terminal always shows the desk chief chat; add assets from the Terminal and see their chart; desk agent names in Spanish.
- Fixed: Resize the desk chief chat, fold Positions, and add or remove assets from the watchlist.
- Fixed: Desk agents read live prices and candles of the desk assets, desk agents can always take the desk tasks, and the desk chief answers in your language.
- Fixed: The left menu has a chevron to compress it to icons and expand it again (it remembers your choice), and the dot next to Money desks is gone.

## 1.0.1

- New: Delete a money desk: its folder goes to the Trash and can be restored; your agents and conversations stay.
- New: Ask Hokma in the chat to create or change a desk; it asks for your permission first.

## 1.0.0 Money desks

- New: Money desks. The side menu has a "Money desks" entry with one row per desk (name, capital, today's result). The Fund view shows the simulated capital, the tokens of the week and one card per desk (focus, status, capital, today, drawdown, agents working). Everything is simulation: real money stays blocked.
- New: create a desk from a template (Minimal, Standard or With debate) in four steps: focus, agent team with the model of each one, rules (the weekly token cap) and assets. "Create it with Hokma" opens the chat with a first message that asks for the desk; Hokma proposes the values in text and the desk itself is created from the template.
- New: each desk has its own agents: a desk chief, signals, risk, bull case, bear case and judge. The Agents tab groups the team as Working, Waiting and Off, and shows what each agent can and cannot do, its tokens of the week and its latest activity. You can bring an existing agent, create a new one or remove one from the desk.
- New: the model of an agent is chosen per desk (Haiku, Sonnet or Opus, with a confirmation for Opus). Changing it in one desk changes nothing in another desk or in Settings.
- New: the desk Terminal shows your assets with last price and change, the chart of the asset in focus, a Positions panel and the chat with the desk chief. Tokens of every turn count on that desk only.
- New: weekly token budget per desk. At 80% a notice appears; at 100% the desk chat and the desk tasks stop and the screen says why. Raising the budget or a new week resumes the desk. Other desks and the general chat keep working.
- New: the Settings tab of a desk edits its name, focus, simulated capital, monthly objective and mandate, its assets and the connections it uses. Operations and Rules show what arrives next. The rules of a desk are written by talking with the desk chief and arrive in the next version.
- New: the learn guides have a new class, "Money desks" (English and Spanish), and the kit installs the six desk agents turned off until you bring them to a desk.
- Check: a database from 0.0.39 with projects, agents and conversations upgrades without losing or changing a row.

## 0.0.39

- New: Hokma keeps no model name of its own. When you connect a key (Google, OpenAI, Anthropic, DeepSeek, Kimi or any other provider), Hokma reads the models that key can use from the provider itself, keeps the list, and reads it again in the background at every start (without network it uses the last list). Fast, Balanced and Powerful are picked from that list: stable chat models only, the newest of each size. The connection test uses the same model the chat will use (bug 1173).
- New: when a model disappears or the provider refuses it, Hokma picks the replacement from the list and tells you in the chat: "The model X no longer exists; you now use Y". A model you chose yourself is never replaced: the chat says so and Settings > Engines > Models shows the list to pick another. Codex and the Gemini CLI use their own default model (bug 1173).
- New: Settings > Engines > Models lets you leave each level on Automatic or choose a model from your provider's list (bug 1173).
- Fix: connecting an engine with an API key says why it failed instead of "The engine answered with an error": invalid key, a model your key cannot use (with its name), quota used up, service not available in your country, or the provider is down. Google answers a bad key with 400 and Hokma now reads it as a rejected key (bug 1173).
- Fix: the provider's real answer is written to `logs/engine.log`, without the key (bug 1173).
- Fix: with a ChatGPT account managed by an admin, Codex says why it cannot connect: Codex is not enabled for the account (your ChatGPT admin must enable it), or signing in with a device code is not allowed. After signing in with ChatGPT, Hokma tests the connection before calling it connected, and Codex's real error goes to `logs/engine.log` (bug 1175).
- Fix: an engine error in the chat shows only its text in your language, wrapped to the width of the chat. The provider's raw answer no longer appears as one long line of English JSON, and the Claude disconnected notice no longer carries an English line under it (bug 1174).
- Fix: on Windows, the projects Hokma creates keep Git on. `git init` writes `symlinks = false` there, and Hokma's own check refused it (bug 1174).

## 0.0.38

- Security: the GitHub backup no longer reaches your whole GitHub. Hokma connects as a GitHub App that you install on ONE repository ("Only select repositories"), and it only sees that repository (bug 1169). Pick it in Settings > Backup; "Change repo" picks another one. Hokma no longer creates repositories: you choose an existing private one or create it from a button.
- Fix: a student who had GitHub connected the old way gets a notice to connect again and choose a repo; the old token is deleted from this computer, and a button opens GitHub to remove the old authorization.

## 0.0.37

- Fix: Telegram says why the Avisos topic could not be created. Before, any refusal from Telegram (no right to manage topics, group not found) was shown as "could not reach Telegram", even with the bot answering. Now the screen shows Telegram's code and a message in your language, Hokma writes the real error in `logs/engine.log` and, if the group became a supergroup after it was linked, Hokma keeps its new id and creates the topic (bug 1170).
- New: `/new Name` (or `/reset Name`) in a topic opens a conversation titled "Topic · Name" and the bot answers "New conversation started: Topic · Name." Without a name it works as before (task 3250).

## 0.0.36

- Fix: the window never shows white. It opens only after the panel painted its first dark frame, it does not open early on a slow start, and Quit hides the window at the first click while Hokma shuts down in the background (bug 1122). This changes Hokma.exe: run the latest Hokma-Setup once.
- Fix: Settings > Engines reads the real state of Claude: a working session shows Connected, and the saved connection that was missing is restored by itself (bug 1124).
- Check: no process launched by Hokma opens a console window on Windows. A test now fails when any launch (including launchers passed as parameters, the command line and the Mac keep-awake) lacks `spawn_kwargs()` (bug 1107; the backup's git calls already had it).
- Check: Telegram topic sessions are dropped when the group changes, and a permission left pending by `/new` can still be answered from Telegram (bugs 1109 and 1110, already in 0.0.35, kept under test).
- Fix: Settings > Engines: Connect Gemini is disabled when Gemini is not installed, and connecting the same engine again reuses its row instead of adding another (bug 1158).
- Fix: disconnecting and connecting Claude again no longer leaves old conversations broken; a conversation keeps working with Claude, and a disabled Claude says so in your language (bugs 1159 and 1160).
- Fix: a permission card says "in your working folder" when the conversation has no project, and Engines no longer says "Connect an engine" while Claude answers (bugs 1161 and 1162).
- Fix: Settings > Engines shows each state in your language, Test shows its result, and a failed attachment says why and can be removed (bugs 1162 and 1164).
- Fix: after the first-run wizard the Projects panel and the terminal use the folder you chose, without reloading (bug 1165).
- Fix: first run keeps what you typed when you go back, a path that cannot be created says why in your language, an empty file in the Library says it is empty, and Library names keep their brackets (bug 1166).
- Fix: forms and API calls show validation errors in your language instead of raw English, the agent names keep acronyms such as MCP, and Learn lists the commands of your language (bug 1167).
- Fix: updating, shutting down or restoring while an agent works asks you first, instead of cutting it (task 3248).
- Check: the start also verifies the loose files at the top of backend/ and cli/, and two unstable connection tests no longer depend on a SQLite file or on a leftover credential store.

## 0.0.35

- Fix: Hokma opens faster and the window no longer shows a white screen. The window stays hidden, with Hokma's dark background, until Hokma has loaded; the icon in the tray shows that it is opening. If something goes wrong the window opens and says what (bug 1122).
- Fix: opening Hokma no longer reads every file of the app twice to check it; it checks the code that runs, once, which is faster where an antivirus scans each file (bug 1122).
- Fix: shutting Hokma down from the app shows "Hokma was shut down" with an "Open again" button instead of a blank window (task 3165).
- Fix: Settings > Engines always lists Claude with its state (Connected, Session expired, Not installed or Disconnected) and a button to sign in or connect it again (bug 1123).

## 0.0.34

- Fix: the chat no longer stops when an antivirus blocks an installer file; the runtime no longer carries `Instalar-Hokma.cmd`, which no longer uses `Invoke-Expression` or a hidden window (bug 1113).
- Fix: a signed-out Claude shows the sign-in text and button in the chat, and Settings > Engines shows the Claude sign-in step and refreshes the session on Test (bug 1112).
- Fix: Telegram connect logs the real error, tells a rejected token apart from a blocked network and says so in Settings (bug 1114).
- Fix: Telegram requests use TLS 1.2, because some networks cut the TLS 1.3 handshake towards Telegram (bug 1114).

## 0.0.33

- Fix: the detached updater no longer imports keyring, so updating from 0.0.31 and later works (bug 1111).

## 0.0.32

- Telegram group with topics, one session per topic, auto close.

## 0.0.31

Hokma keeps the computer awake while it works, talks to the student through Telegram, and backs up everything to the
student's own GitHub (SPEC-011).

- Awake while there is work: the computer is not suspended while a turn or a routine runs; no wake-up is scheduled on macOS.
  After a sleep, the routines that were missed run once.
- Telegram: pair a bot from Settings, approve or reject permissions with buttons, get notices there, with an email as the
  fallback when Telegram fails. Oversized permission prompts are denied with an explanation.
- GitHub without git or gh: "Connect GitHub" uses the device flow (a code on screen, approved in the browser). Each project
  gets its own private repository and stays connected by itself.
- Cloud backup of everything: projects, Library originals, folders added to a project, the database and the memory. The
  encrypted `.hkb` goes to a private release of the student's repository, only when something changed; the 7 most recent are
  kept. The recovery key is shown once, can be generated again until it is confirmed, and is final after that.
- Restore on another computer from Settings: pick the backup, type the key. The engine restarts by itself and the screen lists
  what has to be reconnected. The previous database is kept in `backups/before-restore-*`.
- System tray icon on the desktop app.
- A third-party notice travels inside the app, in `.notices/THIRD_PARTY.txt`.

## 0.0.30

The chat answers with the engine that is really there, and the model button lists every connected engine (bugs 1095, 1104 and 1105).

- A student with Claude connected had every message sent to Gemini ("the gemini command is not installed"). A new connection
  became the default only by being the first row saved, so a failed "Connect Gemini" click took the chat from a signed-in
  Claude, and the model button had the word Claude and its three models written in the frontend.
- One rule for the chat and for the screen: the saved engine answers only when it is installed and connected (proved to
  work). If not, the connected engine the student chose last answers (Claude Code counts as one more, and goes first only
  when the student never chose another connected engine), and an engine that was never tested is tried only when nothing
  better exists. The last choice is saved apart (`chosen_at`, a new column), written only when the student picks an engine or
  a model, never by a connection test. It is resolved at start and before every message. The choice is never rewritten:
  when the saved engine works again it answers again. The chat says so once per conversation while the replacement lasts.
- A new connection becomes the default only if it is Claude Code or Claude Code cannot be used.
- With no engine installed and connected the chat says so in plain words and has a button that opens Settings > Engines; the
  model button says "No engine".
- The model button lists one group per connected engine with the models of the catalog Hokma already has, and shows
  "Engine · Model", always the engine that answers. Picking a model picks its engine for the conversation and the next ones.
  The list is read again every time the menu opens. The last line connects another engine.
- The checks of which engines are installed and the adapters look for programs in the same places (one function).

## 0.0.29

Three reports from students on Windows, and the tests that were red on main (bugs 1093 to 1097).

- WinError 448 (bug 1093): uv makes a junction `cpython-3.12-...` to the real Python folder, and Windows Redirection Guard
  can refuse to cross it ("untrusted mount point"). `python_dirs()` skips links and tolerates any OSError. The installer and
  the updater build the venv from the real `cpython-3.12.x-...` folder, remove the junction (only the link, never the folder)
  and ask uv to install Python only when no real one is there, because uv has no option to skip the link. If uv failed only
  on the link and the real Python exists, the update goes on. A job of `desktop-check` runs the whole installer on Windows
  Server and checks that the junction is gone and the real Python is intact.
- Voice model download (bug 1094): databento pulled `pip-system-certs` on Windows, which stacked a second truststore patch on
  `ssl` and looped until RecursionError. The lock leaves the package out (`override-dependencies`), the download undoes a
  stacked patch before it connects, and a RecursionError is reported as a certificate conflict, in plain words.
- Chat failed with "Algo fallo" while the connection said Connected (bug 1095): the chat shows the engine's message for every
  error code, not only four, with a text for each of the codes that had none. A failure while preparing the environment has its
  own code, an engine that does not open goes to `engine.log` with its traceback and marks the connection as an error, an
  engine error without a message shows its code and where to look, and the connection test writes the same instructions file
  as the chat.
- The changelog header `unreleased` made `test_every_version_place_says_the_new_version` fail (bug 1096): the header only
  appears with the version bump.
- Eight tests were red on main (bug 1097). The repository rules read the real folder `.claude/skills` as a forbidden phrase;
  a plugin notice was written in Spanish inside the backend and now comes from the catalog; two processes were launched
  without `spawn_kwargs()`; a catalog text named `hokma start`; three tests were stale (five locks since 0.0.22, the key of
  an AI engine is not the key of a data provider). One more test depended on the hash seed: the rule reported for a link to
  the data folder could be `secrets` or `hokma_internal`; the order is now fixed and the secrets come first.

## 0.0.28

Library fixes (SPEC-010).

- Uploads and links respect the 50,000 passage cap: a document that would go over it is kept as failed with the cap message.
- At the cap, a disk file that changed is reindexed (its passages are replaced); only new files wait.
- A hidden folder (name starting with a dot, such as `.git`) cannot be added as a read folder.

## 0.0.27

The Library (SPEC-010). One place for the student's reference files, searched by meaning and by words, on any engine.

- A Library screen with folders: create, rename, remove and drag files between them. Files are no longer tied to one
  project; a project can also index folders of the student's disk, which are scanned in the background.
- The agent tools `docs_search` and `docs_list` search the Library by meaning (a question in one language finds the
  passage in another) and by exact words, and say "nothing found" instead of returning weak matches.
- A small multilingual embedding model is downloaded once in the background and runs on the student's computer.
  Vectors live in `sqlite-vec` (SQLite) or `pgvector` (Postgres), with a plain fallback on Postgres.
- Hokma 0.0.26 that is still open while the update runs keeps working: its per-project document routes answer as
  aliases of the Library.
- CI: a `library-tests` job runs the Library tests on Windows, macOS (arm64 and x64) and Linux, with paths that carry
  spaces and accents, and a `vec-load-smoke` step that loads `sqlite-vec` with the Python Hokma installs. A manual
  `model-smoke` workflow downloads the real model on three platforms and measures the time.

## 0.0.26

Documents per project with local search (SPEC-009). A project keeps its reference files and the agent finds the
passages by words, on any engine, without embeddings.

- Add PDF, Word, Excel, PowerPoint, text, CSV, HTML and image files to a project, by dragging them or from a link.
  Each file is split into passages with its section (page, sheet, heading) so the agent cites file and section.
- New tools `docs_search` and `docs_list` in Hokma's MCP for every engine.
- The search ignores accents and case, and tries all the words first, then any of them.
- CI: the document tests (`test_docs_*`) also run on Windows and macOS, with paths that carry spaces and accents and
  with the removal of a file that was just read.

## 0.0.25

Two real cases on Windows (task 3175): pressing Stop left Hokma hung, and a `git clone` waited for credentials nobody could type.

- Stop never hangs: the engine's whole process tree and any child command are killed (job object or `taskkill /T /F` on
  Windows, process group on POSIX), and the turn, the conversation lock and the session are released in a `finally`.
  Other chats and deleting conversations keep working.
- Every command Hokma launches (`run_command` of the direct engines and what Claude Code's Bash runs) has a configurable
  time limit (default 10 minutes, `HOKMA_COMMAND_TIMEOUT`) and live output; when it expires it is cut and the chat says so.
- Git and downloads never ask for input: `GIT_TERMINAL_PROMPT=0`, `GCM_INTERACTIVE=never`, `PIP_NO_INPUT=1`,
  `GH_PROMPT_DISABLED=1`, stdin closed. Without credentials they fail at once with a clear message.
- New tool `skill_install(url)` in Hokma's MCP (every engine): a GitHub repository or folder, downloaded as a zip into a
  temporary folder, one or several `SKILL.md`, plugins and marketplaces are handed to the plugin install; the skill shows
  up at once. Settings > Extensions has a button to add a skill by link. The kit no longer says "only request".

### Added to 0.0.25 (task 3177): Codex starts on Windows

- `config.toml` of Codex wrote the environment variables of the MCP servers as bare keys; Windows has variables such as
  `ProgramFiles(x86)` and the parenthesis made the TOML invalid, so Codex did not start. Every key that is not
  `[A-Za-z0-9_-]+` (variables and `[mcp_servers.<name>]` tables) is now written quoted. Test parses the file with `tomllib`.

## 0.0.24

0.0.23 did not install on a Mac with an Intel processor (macOS 15, `macosx_15_0_x86_64`): `uv` found no wheel of
onnxruntime 1.31.1 for darwin x86_64. The voice (faster-whisper) pulls onnxruntime, and from 1.24 on it is only published
for Apple silicon, Linux and Windows.

- onnxruntime is pinned to 1.23.2 (the last release with a macOS Intel wheel) on that platform only; every other platform
  keeps 1.31.1, so the lock has two entries chosen by environment markers.
- `[tool.uv] required-environments` lists the five real platforms (Linux x86_64 and aarch64, macOS arm64 and x86_64,
  Windows AMD64). `uv lock` now fails if a dependency has no wheel for one of them, instead of failing on a student's machine.
- The voice reports itself as unavailable when faster-whisper or onnxruntime is not importable, and the app says
  the voice-unavailable notice, without affecting anything else.
- CI: new job `install-macos-intel` in `desktop-check.yml` installs Hokma with `installer/install.sh` in a temporary folder
  on an Intel runner (`macos-15-intel`, since `macos-13` no longer exists) and runs `hokma doctor`. It also runs when
  `pyproject.toml` or `uv.lock` change.

### Added to 0.0.24 (task 3172): the installer on Windows Server

The real log of Hokma-Setup 0.0.23 on a VPS with Windows Server (user Administrator) ended with
`uv.exe : Using CPython 3.12.13 ... NativeCommandError` and `install.ps1 failed (exit code 1)`. Cause: Windows PowerShell
5.1 with `$ErrorActionPreference = "Stop"` turns every line a native program writes to stderr into a terminating error,
even when the program succeeds, and uv writes its progress to stderr.

- New `installer/native.ps1` with `Invoke-Native`: lowers `ErrorActionPreference` to Continue only during the call,
  merges stderr into stdout, forwards each line to the log as plain text, keeps the output in `$script:NativeOutput` and
  decides by the exit code alone. `install.ps1` runs every native program through it: `uv python install`, `uv sync`,
  robocopy, `hokma write-manifest / verify-install / install-tools / kit-sync / init-config / shortcuts`, `doctor`, and
  git (`Invoke-Git` now calls it). `setup-helper.ps1` has the same function (it runs before the package is unpacked) and
  uses it for winget, `powershell.exe` and `cmd.exe`; `Invoke-Logged` is gone. `uninstall.ps1` and `download.ps1` make
  no native call. The updater has no `.ps1` scripts: it runs in Python.
- Windows Server: the install goes on. The Windows edition (product, edition, installation type, build) is written to the
  log (`Windows edition: ...` in install.ps1 and the setup log). On a Server edition an administrator is no longer
  refused (the usual user there is one); on Windows 10/11 the refusal stays.
- Tests: `backend/tests/test_installer_native_3172.py` fails if any `.ps1` calls a native program with `&` or by bare name
  outside the function, reads `$LASTEXITCODE` outside it, or if the Server handling or its messages disappear.
- CI: new job `install-windows-server` in `desktop-check.yml` on `windows-2022` (a Server edition): proves with a fake
  program that writes to stderr that `Invoke-Native` survives `Stop`, then runs `installer/install.ps1` for real in
  temporary folders and `hokma doctor`.

### Added to 0.0.24 (task 3174): the terminal and the voice on Windows

On a real Windows 0.0.23 the Terminal panel said it could not start the terminal and the voice said the model download failed,
both with no reason. The cause could not be reproduced without that machine, so the work covers the
likely ones and makes the real reason visible so a repeat names itself:

- Terminal: the socket caught only `OSError`, but pywinpty raises its own error types; they now reach the panel as
  `spawn_failed` with the real text. Every attempt (shell, environment, folder, PATH length, error) goes to engine.log
  under `terminal:` and is shown under the message.
- Terminal: if the first shell does not start (the Store alias of `pwsh.exe` is a reparse point a pseudo-terminal cannot
  start), the next is tried: PowerShell 7, Windows PowerShell, `%COMSPEC%`, `System32\cmd.exe`. If all fail with the full
  environment, a plain one (the app's own plus TERM) is tried. SYSTEMROOT, WINDIR and COMSPEC are always set.
- Terminal: since 0.0.22 the PATH of the shell got Hokma's tool folders and the installers' folders even when they did
  not exist; only existing folders are added now.
- Voice: the download keeps the chain of real causes ("RuntimeError <- SSLError: certificate verify failed"), shows it
  under the message and writes it to engine.log under `voice:` with the names (never the values) of the proxy and
  certificate variables that were set. The Hugging Face Xet transport is turned off for this single-file download.

## 0.0.23

0.0.22 broke the chat on real Windows for many students: every message, even a "hola", answered "Something failed
while answering", claude-mem did not work and calling an agent said "the MCP failed". 0.0.22 had been checked on
Linux only. What was found by reading the code and simulating Windows in the tests:

- Hooks and Hokma's MCP server run with `python -I`, which ignores `PYTHONUTF8`, so on Windows their pipes were cp1252.
  A payload from Claude Code with an accented name in a path arrived garbled (the working folder no longer matched, writes
  inside it looked "outside"), some accented capitals did not decode at all (the hook failed closed and refused every
  tool), and a reply of the MCP server with a character outside cp1252 crashed it ("the MCP failed"). Now the hooks read
  their input as UTF-8 bytes, write stderr as UTF-8, and the MCP server reads UTF-8 and answers in plain ASCII JSON;
  a bad message gets an error answer with its reason instead of ending the server.
- The copy of the student's skills and agents into the user source never copied anything on Windows:
  `DirEntry.stat()` there always reports `st_nlink == 0`, and the check asked for exactly one link. The link count now
  comes from `os.lstat`; a hard link is still refused.
- A failure while starting the engine (a Python error in Hokma) left an empty answer and then a bare "Something
  failed" from the socket handler. The turn now ends with `engine_error` and the real text (`ValueError: ...`), and the
  traceback goes to `<data>/logs/engine.log`. Every failed turn writes its code and message there. An error result of
  Claude Code (a hook that blocked the prompt, an API error) carries its text and the tail of the CLI's stderr.
- The chat shows the engine's own words under the short text for `engine_error`, `engine_exited`, `claude_not_found`
  and `integrity`. `claude_not_found` says which PATH was searched.
- Finding `claude` fails open where it is not a lock: a PATH entry the path comparison cannot judge (a disconnected
  network drive, a cloud folder) is kept and logged instead of stopping the conversation; quoted entries and entries
  written with a variable are read like the shell reads them; a `Path` key in any letter case is read. The writable
  part of the data folder is still left out.
- A hook that fails closed now also prints the exception text, and printing can never turn a block into an allow.
- "Sign in with Claude" (first run and Engines) ended on Windows with "Login failed: Request failed with status code
  403", by the browser and by pasting the code. Reading `git diff` of 0.0.21..0.0.23: the sign-in code
  (`backend/hokma/setup/claude.py`) did not change; the child still runs `claude auth login --claudeai` once, with
  Hokma's process environment plus `CLAUDE_CONFIG_DIR`, without `<data>/tools` on its PATH, and the pasted text goes
  whole (`code#state`) to that same child. Checked against the real Claude Code 2.1.322 (without finishing the OAuth):
  the 403 is the answer of Anthropic's server after the code is exchanged, not a wrong code (a code from another
  attempt says "That code wasn't accepted"). So the sign-in is pinned to the 0.0.21 environment on purpose (Hokma's
  tool folders `<data>/bin`, `<data>/tools`, `<data>/python` are taken off its PATH if present, the engine-only
  variables such as `ENABLE_CLAUDEAI_MCP_SERVERS` are dropped) and every attempt now leaves in
  `<data>/logs/engine.log`: the command, the Claude Code version, the environment (all names; values only for PATH,
  config, home, proxies without user and password, and similar), the pid, the length of the pasted code and whether it
  has its `#state` part (never the code), the exit code, the full output with secrets hidden, and the `[ERROR]` and
  `[WARN]` lines of Claude Code's own debug log (`--debug-file`, deleted afterwards; a Claude Code without the option
  signs in without it). The next refused sign-in says which request was refused.
- Tests: `backend/tests/test_windows_3171.py` (cp1252 pipes for hardblock, guard and the MCP server, a Windows
  `DirEntry`, PATH entries that fail the comparison, a broken engine start and an error result reaching the chat and
  engine.log); `backend/tests/test_setup_api.py` (the sign-in environment, one child for the code, what
  engine.log keeps and what it hides).

## 0.0.22

The agent is no longer fenced in. Cristian's decision: four locks stay, in every mode; everything else is allowed. A
real case: claude-mem could not be installed from the chat (the guard blocked changing plugins and there was no
Node), and on Windows it was installed but its worker never answered on port 37777 because the computer had neither
Bun nor Node.

- The four locks: (1) orders with real money to a broker, (2) reading or taking keys, tokens and credentials, by any
  route, (3) deleting files (the trash tool is the way), (4) writing outside the working folder, with two open places:
  Hokma's data folder with its claude-config (plugins, settings, tools) and what the software installers (npm, pip,
  winget, brew) write on their own. Plus the minimum protection that keeps the locks on: the files of the hooks and their
  registration in the settings. `settings.json` can still be edited while both hooks stay registered.
- Now allowed: installing dependencies (pip, npm, uv, bun) wherever they are needed, installing and removing plugins,
  skills and MCP servers from the chat (also with files outside the working folder), changing Hokma's base
  configuration and the permission settings, installing software (Node, npm, npx, pip, winget, brew), and Git without
  limits inside the working folder (force push, `reset --hard`, `clean`, `config`, `-c`, `GIT_*` variables). Emptying a
  file, moving or linking `.git`, `.claude` and `.hokma`, the `hokma` launcher and Hokma's owner endpoints are no
  longer special.
- Removed: the `truncate` and `force_push` rules (the app lists five protections now), the reading ban on most of
  Hokma's data folder (only its secrets stay hidden: database, `identity.key`, sign-ins, backups, `.claude.json`), the Bash
  sandbox, the `permissions.deny` list, the protection of the whole data folder and of `.hokma`, the check of odd
  characters in a path, the git argument white list, and about 600 lines of the command analyzer.
- Software installers work: `curl url | sh`, `bash -c "$(curl url)"`, `iex (irm url)` and `irm url | iex` are no longer
  refused as "a shell fed with built text" (the idiom Homebrew, Bun and Node managers use).
- Node (LTS 24) and Bun (1.4) come with Hokma, portable, in `<data>/tools/node` and `<data>/tools/bun`. Versions and
  sha256 are in `installer/lock.json`; the archive is hashed before anything is unpacked, and a wrong hash installs
  nothing. One implementation (`hokma install-tools`) serves the Windows installer, `install.sh` and `hokma update`
  (an update brings them to a computer that has 0.0.21). `uvx` is installed next to `uv`: claude-mem needs it for its
  vector search, and the installers had copied only `uv`. `hokma doctor` reports what is missing.
- The engine, scheduled runs and the terminal start with a PATH that holds Hokma's tools first (uv, Node, Bun, the Python
  uv installed), then `~/.bun/bin` and `~/.local/bin`, so the hooks of plugins find `node`, `npm`, `npx`, `bun`, `uv` and
  `uvx`, and what a plugin downloads on its first start is on the PATH too. The environment filter keeps `BUN_INSTALL`,
  the npm prefix and cache, `CARGO_HOME`, `GOPATH` and similar variables.
- Checked with the real command of Hokma (temporary data folder and config, haiku, a PATH without Node, Bun or uv): the
  plugin is installed from the chat (`claude plugin marketplace add thedotmack/claude-mem`, `claude plugin install`)
  through the hooks without a block, a short conversation starts the worker, and `/api/health` answers `ok` with the Bun
  of `<data>/tools/bun` (port 37790 on the test machine, where the owner's own worker uses 37777) and no degraded
  dependency.
- HOKMA.md, CLAUDE.md and the learn guides: the agent helps with everything (install, configure, repair Hokma) and stops
  only at the four locks; to delete it uses the trash.

## 0.0.21

Hokma answers as Hokma. In 0.0.20 the engine answered "I am Claude, Sonnet 5.5, in Claude Code" and listed the
owner's claude.ai connectors (Gmail, Drive, Slack), as if the fund's instructions did not exist.

- Cause, proven with a real run (a temporary folder whose instruction file imports another file with an invented
  identity): Hokma starts Claude Code with `--setting-sources user`, and that flag also decides which memory files
  Claude Code loads. With `user` the folder's instruction file never entered and the engine said it was Claude; with
  `user,project` it took the invented identity.
- Fix for every engine: Hokma now hands over its instructions itself instead of waiting for the engine to find a
  file. The text is built on every turn: an identity block, HOKMA.md with the owner and fund names filled in, your own
  instruction file (CLAUDE.md, with its `@file` lines included if the file is inside the folder) and the persona of the
  active agent. Claude Code receives it with `--append-system-prompt-file` (a file in Hokma's protected config
  folder, named after its content); Codex, ACP engines and API connections receive the same text through their own
  channel (AGENTS.md, first prompt block, system prompt). Scheduled runs get it too.
- `project` is not added to `--setting-sources`: it would load the settings of the working folder, which the agent can
  edit, and they could weaken the locks.
- Identity: asked who it is, the agent says it is Hokma, the agent team of the owner's fund. Asked which engine or
  model it runs on, it says so (Claude, ChatGPT, Gemini) without presenting itself as Claude Code or Codex.
- Hokma starts Claude Code with `ENABLE_CLAUDEAI_MCP_SERVERS=false`: the connectors of your claude.ai account no
  longer mix into Hokma. MCP servers you approved in Hokma or added yourself with `claude mcp add` keep working.
- Skills and agents of the working folder work again. The same `--setting-sources user` flag hid `.claude/skills` and
  `.claude/agents`: a skill with a SKILL.md was not seen, `--agent <name>` answered "not found" and an agent could not
  dispatch to another one (proven with the real command: "Agent type 'helper' not found"). On every launch Hokma now
  copies your active skills and agents into the user source inside its protected config folder (`skills/`,
  `agents/`), and removes what you deleted or turned off. Only the safe frontmatter keys cross (name, description,
  tools, model, color; for skills also when_to_use and the invocation switches): a file the agent wrote cannot bring
  hooks, a permission mode or MCP servers with it. Links and hard links are never read. A skill or agent you put in
  the user source by hand is not touched. `project` is still not in `--setting-sources`.
- The text handed to the engine has a cap: the `@file` imports together (60,000 characters) and the whole text
  (150,000), so a CLAUDE.md with many large imports cannot produce a multi-megabyte prompt. A named pipe called
  CLAUDE.md or HOKMA.md no longer hangs the start (files open without blocking). The instructions file is now private
  (0600, folder 0700).

## 0.0.20

Hokma can be used in the browser as an alternative to the app.

- The browser screen that sent you to the app now also has "Use Hokma here in the browser". It opens the panel in your
  default browser with a valid session, with the same safety as shutting down from that screen (a single-use code,
  nobody inside, one call every 30 seconds).
- In the app, Settings > General has "Open in the browser" and the preference "Always open Hokma in the browser". With
  it on, the Hokma icon opens the browser instead of the app window; `hokma start` always opens the browser. The
  session is never put in a file: the supervisor makes a new one-time code and the browser enters with it.

Plugins are really installed now. In 0.0.19 caveman, claude-mem and cmem-pro showed Error "The plugin files are not on
the disk": Hokma only wrote them to its list and nothing ever downloaded them.

- Installing a plugin runs Claude Code itself with Hokma's own config folder and the same filtered environment the
  engines get: `claude plugin marketplace add` (only when that marketplace is not known yet) and
  `claude plugin install <plugin>@<marketplace>`. No shell, arguments checked first, a 5 minute limit, and what
  Claude Code printed goes to Hokma's log (logs/engine.log). Uninstall runs `claude plugin uninstall`; removing a
  marketplace uninstalls its plugins and forgets it in Claude Code too.
- The result is confirmed in plugins/installed_plugins.json. If it fails (no network, plugin not found, timeout) the
  card shows Error with the real reason and a Retry button that installs again. A plugin that is on but not installed
  yet shows "To install" with the same button; an install in progress shows "Installing".
- On the first start of 0.0.20 every plugin you approved that is on in your list without files is installed for real,
  in the background and only once, with a line per plugin in the log. Entries that only appeared in Claude's own
  settings without your approval are not downloaded.
- Plugins work with Claude Code only. If your default engine is Codex, Gemini or an API connection, the card says
  "Claude Code only" and never reads Active.
- claude-mem needs its worker and its hooks: when the files are there but the worker does not answer (default port
  37777) or hooks/hooks.json is missing, the card says "Check" with the reason instead of Active.

The agent can read Hokma's own files.

- Reading is allowed in Hokma's data folder and its claude-config (plugins, installed_plugins.json, settings, logs):
  `ls`, `cat`, `grep`, `find`, `tail` and the Read, Grep and Glob tools. This lets the chat check things like whether
  claude-mem is installed.
- Still hidden (deny by default): only a list of known folders and files can be read inside the data folder (claude-config,
  logs, runtime, kit reports and similar). Credentials and tokens (`.credentials.json`, `.claude.json` and
  its backups, `extensions.json` (it can hold server environment values), `database.json`, `identity.key`, keys, the Codex and ACP sign-in folders), the database, its backups, the
  trash, the control channel and the lock file, and anything new that is not on the list, are refused. A search that
  would walk into them (`grep -r` over the data folder or claude-config, `cat claude-config/.*`, brace or variable
  tricks, `Get-ChildItem | Get-Content`) is refused too; name a subfolder such as plugins.
- Writing, deleting and moving there is still blocked, and the message now says the agent can read but that the owner
  changes Hokma's settings, plugins and data from Settings. The permission rule that denied any `Bash` command naming
  the data folder is gone; the hook decides.
- Hardened after QA: the database and the credentials can no longer be reached with a relative path after `cd`, with a
  recursive search over a folder that holds the data folder (`grep -r`, `rg`, `find`, `ls -R`, `tar`, `cp -r`, `zip` on
  `~`, `~/.local/share` or `%LOCALAPPDATA%`, unless it excludes the data folder), or through a link in the working
  folder. Every path is judged by its real location from the folder the line is in at that point. The conversation
  transcripts (`claude-config/projects`) and the guard log are no longer readable. The browser routes
  (`/api/system/browser`, `browser-pref`) are as untouchable for the agent as the shutdown, and opening the browser
  waits for running turns even with a session. The supervisor no longer restarts the engine twice when there was none.

Agents and skills made from the chat show up at once.

- New tools in Hokma's MCP: `agent_save` creates an agent (name, role, model, tools) with the same logic as the Create
  agent button, or edits one you created, and `skill_save` writes a skill to `.claude/skills/<name>/SKILL.md` in the
  working folder. The agent is on unless the chat is told to leave it off, and appears in Settings > Agents without
  restarting Hokma. The kit's own agents are not rewritten by the tool.
- An agent file written straight into `agents/` (by the chat, an editor or by hand) is picked up when the panel asks
  for the list: it is added, loaded and turned on like the ones made with the button. The Agents screen refreshes
  by itself every few seconds.
- Tests prove that no lock (guard.py, hardblock.py) stops writing agents, skills or the project instructions file
  (CLAUDE.md, HOKMA.md) in the working folder, with POSIX and Windows paths; the settings and the data folder stay
  locked.

## 0.0.19

Plugins by link: you see what got installed, whether it is on, and each repository appears once.

- Adding a repository by its address now looks at what it is. A marketplace with one plugin installs that plugin
  right away; one with several shows its plugins with an Install button; a repository that is a plugin on its own
  is installed. If the repository is neither, Hokma says so and saves nothing.
- Settings > Extensions > Plugins starts with "Installed": every plugin shows Active, Off or Error (with the
  reason), and a switch. The state comes from what the engine really has on disk, not only from your saved list.
  A plugin that is on but not unpacked yet says "To install" and is unpacked the next time you open a conversation.
- One repository is one marketplace, however it was written (owner/repo or a GitHub link) and whatever name it
  came with. The duplicates already saved, like claude-mem twice, are merged when you open the panel. Every
  marketplace has a Remove button (it also removes the plugins installed from it) and a button to see its plugins.
- In the plugin list of every marketplace each plugin has Install or Uninstall in that same menu (Uninstall asks
  first), and in "Installed" each plugin has Uninstall next to its switch. A plugin added by link ends installed
  and on.
- New quick command /mcp in the chat: it opens Hokma's own panel (not the interactive /mcp of Claude Code) with every
  remote and local MCP server and its state (Connected, Needs sign-in, Error with the reason). Connect or Reconnect
  starts Hokma's own sign-in, Revoke removes the access, and a link goes to Settings > Extensions > MCP. The text
  is never sent to the engine, so it works the same with Claude Code, Codex, Gemini and the API. The panel and
  Settings share one sign-in code and one extensions API.
- Windows installer, from Nathaly's real logs: a second Hokma-Setup now says an installation is already in progress and
  does not start. Downloads of uv, Git and Claude Code force TLS 1.2, give up only when no data arrives for 45 seconds
  (a slow but alive download is never cut), retry 3 times with a wait, and say clearly when the retries run out. The
  setup window shows the last real line, such as the MB downloaded or the package uv is fetching, instead of staying
  on "Downloading uv".
- It does not depend on the engine you use: the screen reads Hokma's own files, the same for Claude Code, Codex,
  Gemini and the API. Only Claude Code unpacks plugins today.

## 0.0.18

The Windows installer works when it is started from PowerShell 7.

- The installer could stop with "Get-FileHash is not recognized" when the computer had PowerShell 7 open. The
  installer now starts Windows PowerShell with its own modules, and it calculates SHA256 without that cmdlet.

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
