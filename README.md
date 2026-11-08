# Hokma

Installable harness by SiomTrading. A local backend (FastAPI) serves a dashboard (React)
and talks to the student's own `claude` over JSON-RPC on WebSocket.

## Run in development

Requirements: [uv](https://docs.astral.sh/uv/) and Node 22+.

```bash
# 1. Build the dashboard (the backend serves dashboard/dist)
cd dashboard && npm install && npm run build && cd ..

# 2. Start Hokma: `hokma start` opens the app in your browser (use --no-browser to skip)
uv run hokma start

# 3. Check
curl http://127.0.0.1:8765/health      # {"status":"ok","version":"0.0.6"}
uv run hokma doctor
uv run hokma version
uv run pytest
```

Dashboard with hot reload: `cd dashboard && npm run dev` (proxies `/health`, `/rpc`, `/api` and `/ws` to port 8765).

## Install (student)

From a checkout of this repo, one command and no administrator rights:

```bash
bash installer/install.sh            # macOS and Linux
```

```powershell
powershell -ExecutionPolicy Bypass -File installer\install.ps1   # Windows
```

Options: `--dir <folder>` / `-Dir <folder>` (or `HOKMA_DIR`) to choose the working folder
(default `~/Hokma`), `--no-doctor` / `-NoDoctor`.

### Where things live

| What | Linux | macOS | Windows |
|---|---|---|---|
| Working folder (Git repo, kit; where the agent works) | `~/Hokma` | `~/Hokma` | `%USERPROFILE%\Hokma` |
| Data folder: runtime (`app`, `venv`, `bin/uv`, `python`) and Claude config (`claude-config`) | `${XDG_DATA_HOME:-~/.local/share}/hokma` | `~/Library/Application Support/Hokma` | `%LOCALAPPDATA%\Hokma` |
| `hokma` command | `~/.local/bin/hokma` | `~/.local/bin/hokma` | `%USERPROFILE%\.local\bin\hokma.cmd` |

The runtime and the Claude config are deliberately OUTSIDE the working folder: the agent runs in the
working folder, so anything inside it could be edited by the agent (its own settings, hooks, Hokma code).
Defense in depth:

- the installer refuses a data folder inside the working folder (and the reverse);
- Claude Code sandbox for Bash, with writes allowed only in the working folder and the data folder,
  config and launcher in `denyWrite` (not available on native Windows: `hokma doctor` warns; use WSL2);
- a deterministic PreToolUse hook (`hokma.kit.guard`, fails closed) that resolves real paths for
  Edit/Write/NotebookEdit and blocks the protected paths;
- the Claude `settings.json` is regenerated from the package template on EVERY launch and passed with
  `--settings` and `--setting-sources user`, so an edit by the agent does not survive;
- the installer writes a sha256 manifest of `app/`; `hokma` refuses to start and `hokma doctor` fails if
  a file differs (reinstall to fix);
- `permissions.deny` rules for Edit/Write/Bash on those paths. The Bash substring rule is best effort only;
- `hokma doctor` FAILS if the runtime or the config end up inside the working folder and checks that every
  protected path has its rules, its `denyWrite` entry and its hook argument.

Updates and extensions: `hokma update --from MANIFEST`, `hokma ext ...`.

The installer verifies the uv download against `installer/lock.json` (sha256) before running anything,
installs the pinned Python with uv, copies the app, creates the working folder as a Git repo with the
kit, puts `hokma` in the user PATH and runs `hokma doctor`. It never touches `~/.claude`.
`git` and the `claude` CLI are detected, never installed behind your back.

Uninstall: `bash installer/uninstall.sh` (or `uninstall.ps1`) removes the runtime and the command.
The working folder and the Claude session in `claude-config` are kept unless you pass
`--purge` / `-Purge` and type the confirmation word shown in the prompt to confirm.

Non-ASCII user names (accent, enye): the `.sh` flow is tested with a HOME whose name has an accent, an enye and a space.
On Windows the `hokma.cmd` launcher is written as UTF-8 without BOM and switches the console to
code page 65001 while it runs.

## Tests

```bash
uv run pytest                                                    # unit + static installer checks
HOKMA_E2E=1 uv run pytest backend/tests/test_installer_e2e.py    # real install in a temp HOME (network)
```

Not covered by automated tests yet (needs real machines):

- macOS, Intel and Apple Silicon.
- Windows 10/11 with PowerShell 5.1 and 7, including a user name with accent or enye: `hokma.cmd`
  under cmd.exe, robocopy, user PATH, git with CRLF warnings, PSScriptAnalyzer (not installed on the development machine).
- Antivirus behaviour.
- A student with a real Claude plan logged in, and the exact Claude Code permission-rule syntax of the
  generated `settings.json` (`Edit(//abs/path/**)`, `Bash(*path*)`), especially Windows paths.

## Layout

Top level: `backend/`, `cli/`, `dashboard/`, `contracts/`,
`installer/`, `kit/`, `scheduler/`, `docs/`.

## Language

Code, comments, docs, folder names and log lines are in English. Everything the student reads goes through
message catalogs, Spanish by default and English available: set `HOKMA_LANG=en` (or `--lang en` in the
installer, which remembers the choice in `<data>/language`).

- CLI, doctor, update, approvals and hooks: `backend/hokma/catalog/{es,en}.json`, used through `hokma.i18n.t`.
- Installers: `installer/messages/{es,en}.properties`.
- Dashboard: `dashboard/src/i18n`.

`scripts/check_repo_rules.py` (run in CI) fails on handwritten Spanish outside those catalogs and on references
to the origin of any code or idea.

## Licenses

MIT. See `LICENSE`.
