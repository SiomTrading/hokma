# Changelog

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
