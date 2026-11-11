"""PreToolUse hook: the locks that are always on, in both permission modes.

Four locks and the protection that keeps the first two on. Everything else is the owner's to ask for and the agent's
to do (install software and dependencies, install and remove plugins, skills and MCP servers, change Hokma's own
configuration, Git, permission settings):

    real_money       order tools of brokers and the hosts that place real orders
    secrets          keys, tokens and credentials: the keys stored for Connections, Hokma's database and sign-ins,
                     SSH, cloud and Git credentials, browser profiles, the system key stores, other processes' memory
    delete           deleting files or folders, including the obfuscated forms (the trash tool is the way)
    outside_workdir  writing outside the working folder, formatting or touching disks. Two places are open: Hokma's
                     data folder with its claude-config (plugins, settings, tools), and whatever a software
                     installer writes (npm, pip, winget, brew run their own writes; this hook only sees the writes
                     a command names)
    hokma_internal   the files of these hooks and their registration in the settings: without them the other four
                     could be switched off

It runs for every tool (matcher `.*`) next to `guard.py`, the same way: by absolute path, with an isolated
interpreter (`python -I /abs/hardblock.py --data D --workdir W --protect P ...`) from the data folder, and
`|| exit 2` in the command. It is FAIL CLOSED: any error, unreadable input or damaged data file exits with code 2.

A refusal prints `HOKMA_BLOCKED <rule>` and the reason on stderr (the model reads it and takes another way) and
appends one JSON line to `<data>/guard/blocks.jsonl` (`ts`, `rule`, `tool`, `target` cut to 200 characters,
`conversation_id`; never file contents). The engine turns that line into a `guard_events` row and a card in the chat.

Commands are not matched as raw text. They are read the way a shell reads them: quotes, escapes, `$'..'` strings,
binary paths and wrappers (`sudo`, `env`, `xargs`, `find -exec`, `bash -c`, `cmd /c`, `powershell -c`, `python -c`,
`node -e`) are resolved first; `;`, `&&`, `||`, `|`, `&` and new lines split the command; `$(...)`, backticks and
here-documents are read as commands; a command whose NAME is built at run time (`$x f`, `$(echo rm) f`), `eval`,
`iex`, encoded commands and a decoder piped into a shell are refused outright, except the installer idiom that feeds
the shell with a download (`curl url | sh`, `bash -c "$(curl url)"`, `iex (irm url)`), which software installers
use. A script of the working folder that a command runs is read and its text is checked with the same rules.

Scope of this hook: it is defense in depth, not a sandbox. The agent can run arbitrary code, so the integrity of the
hooks rests on the structural protections too: the sha256 integrity manifest of the installed app, which Hokma
verifies at every launch and `hokma doctor` reports, and the settings file that is rebuilt at every launch.

Standard library plus `hokma.i18n` and `hokma.kit.guard` only.
"""
import fnmatch
import json
import ntpath
import os
import re
import sys
from datetime import datetime, timezone

from hokma.i18n import t
from hokma.kit import guard
from hokma.kit.guard import _fold, under_any

BLOCK = 2
RULES = ("delete", "hokma_internal", "outside_workdir", "secrets", "real_money")
MAX_DEPTH = 6
MAX_SCRIPT_BYTES = 512 * 1024
MAX_TARGET = 200
BROKERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hardblock_brokers.json")


class Block(Exception):
    """A rule fired. `target` is what the call wanted to touch (shown to the owner, cut to 200 characters)."""

    def __init__(self, rule: str, target: str = "") -> None:
        super().__init__(rule)
        self.rule, self.target = rule, str(target)


# ------------------------------------------------------------------------------------------------ context

class Ctx:
    def __init__(self, data: str, roots: list[str], protect: list[str], home: str | None = None,
                 brokers: dict | None = None, cwd: str | None = None, conversation_id: str = "",
                 task_id: str = "") -> None:
        self.data = data
        self.roots = [r for r in roots if r]                    # the working folder: where a command may write
        self.write_roots = self.roots + [data]                  # plus Hokma's own data folder (config, plugins, tools)
        self.protect = [p for p in protect if p]                # the hook files and their registration
        self._protect_text = [norm_text(p) for p in self.protect if len(p) > 3]
        self.home = home or os.path.expanduser("~")
        self.brokers = brokers if brokers is not None else load_brokers()
        self.cwd = cwd or (self.roots[0] if self.roots else os.getcwd())
        self.conversation_id, self.task_id = conversation_id, task_id
        self.seen: set[str] = set()
        self._vars = self._build_vars()
        host_re = [re.escape(h) for h in self.brokers.get("hosts", [])]
        self.host_re = re.compile(r"(?<![a-z0-9.-])(?:" + "|".join(host_re) + r")(?![a-z0-9-])", re.I) if host_re else None
        pats = list(self.brokers.get("url_patterns", []))
        self.url_re = re.compile("|".join(f"(?:{p})" for p in pats), re.I) if pats else None

    def _build_vars(self) -> dict[str, str]:
        env = os.environ
        home = self.home
        local = env.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        roaming = env.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        out = {"HOME": home, "USERPROFILE": home, "LOCALAPPDATA": local, "APPDATA": roaming,
               "XDG_DATA_HOME": env.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share"),
               "XDG_CONFIG_HOME": env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"),
               "HOKMA_DATA_DIR": self.data, "PWD": self.cwd}
        if self.roots:
            out["HOKMA_HOME"] = self.roots[0]
        for name in ("TMPDIR", "TEMP", "TMP"):
            if env.get(name):
                out[name] = env[name]
        return out

    def var(self, name: str) -> str | None:
        return self._vars.get(name.upper())


def load_brokers(path: str = BROKERS_FILE) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("brokers file is not an object")
    return data


# ------------------------------------------------------------------------------------------- text and paths

_VAR = re.compile(r"\$\{?env:([A-Za-z_]\w*)\}?|\$\{([A-Za-z_]\w*)\}|\$([A-Za-z_]\w*)|%([A-Za-z_]\w*)%", re.I)
_WIN_ABS = re.compile(r"^[A-Za-z]:[\\/]")
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
DEVICES = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/stdin", "/dev/tty", "nul", "con", "$null", "nul:"}


def norm_text(s: str) -> str:
    return s.replace("\\", "/").lower()


def expand(text: str, ctx: Ctx) -> str:
    """`~` and the well-known variables, expanded as text. Unknown variables stay as written."""
    if text == "~" or text.startswith(("~/", "~\\")):
        text = ctx.home + text[1:]

    def sub(m: re.Match) -> str:
        name = next(g for g in m.groups() if g)
        value = ctx.var(name)
        return value if value is not None else m.group(0)

    return _VAR.sub(sub, text)


def unresolved(text: str) -> bool:
    return "\0" in text or bool(_VAR.search(text)) or text.startswith("~")


def is_win_abs(p: str) -> bool:
    return bool(_WIN_ABS.match(p)) or p.startswith("\\\\")


def is_abs(p: str) -> bool:
    return p.startswith("/") or is_win_abs(p)


def joined(path: str, cwd: str) -> str:
    if os.name != "nt" and not is_abs(path):
        path = path.replace("\\", "/")                          # `.\script.ps1` written for PowerShell
    return path if is_abs(path) else (cwd.rstrip("/\\") + ("\\" if is_win_abs(cwd) else "/") + path)


def clean_path(p: str) -> str:
    return ntpath.normpath(p) if is_win_abs(p) and os.name != "nt" else os.path.normpath(p)


def under(path: str, roots: list[str]) -> bool:
    if not roots:
        return False
    if is_win_abs(path) and os.name != "nt":
        win = [ntpath.normcase(ntpath.normpath(r)) for r in roots if is_win_abs(r)]
        p = ntpath.normcase(ntpath.normpath(path))
        return any(p == r or p.startswith(r.rstrip("\\") + "\\") for r in win)
    return under_any(os.path.normpath(path), roots)


def short(s: str) -> str:
    return " ".join(str(s).split())[:MAX_TARGET]


# Markers are looked for in lower-case text with `/` as the only separator.
_B = r"(?:^|[\s\"'=:;,()<>|&/@])"
_E = r"(?:/|\s|$|[\"'])"
DATA_SECRET_PLACES = (r"hokma/(?:database\.json|hokma\.db|identity\.key|codex-home|acp-home|backups)|"
                      r"claude-config/(?:\.credentials|credentials\.json|\.claude\.json|backups)")
RAW_DATA_SECRET_RE = re.compile(DATA_SECRET_PLACES)
SECRET_RE = re.compile("|".join([
    _B + r"\.ssh" + _E, _B + r"\.aws" + _E, _B + r"\.config/gh" + _E, _B + r"\.git-credentials(?:$|[\s\"'])",
    _B + r"\.gnupg" + _E, _B + r"_?\.?netrc(?:$|[\s\"'])", r"github cli/hosts\.yml", r"hokma-market",
    # The keyring service that holds the remembered session (read or write), same as the Connections one.
    r"hokma-session", r"hokma-engine", r"engine_key",
    # Engine credentials: the Codex and Gemini sign-ins (Hokma's copies and the student's own) and the student's own
    # Claude session file.
    r"codex-home/auth\.json", r"\.codex/auth\.json", r"\.gemini/oauth_creds\.json", r"\.claude/\.credentials\.json",
    # What Hokma's data folder keeps secret, by its default places (the real folder is judged by identity and text).
    DATA_SECRET_PLACES,
    # The session token of the app, the owner confirmation and the calls that start, resume or hand over a session,
    # and the control channel and lock of the background engine (a Unix socket, a Windows pipe and the lock file).
    r"x-hokma-token", r"x-hokma-owner", r"/api/session/(?:owner-nonce|resume|logout|challenge|enter|handoff-code)",
    r"control\.sock", r"hokma-control-", r"engine\.lock",
    # The system stores themselves: the Secret Service on D-Bus, the keyring and wallet files, the Keychain folder and
    # the Windows credential and vault folders. Naming any of them is a way to read the Connections keys.
    r"org[./]freedesktop[./]secrets", r"\.local/share/keyrings", r"library/keychains", r"kdewallet",
    r"microsoft/(?:credentials|vault)(?:/|\s|$|[\"'])",
    r"python_keyring", r"keyring_pass\.cfg",                       # the Python keyring library's own files
    # Browser profiles: the app keeps its session token in the page storage of the student's browser, so the folders
    # that hold that storage (Local Storage, Session Storage, IndexedDB, Firefox storage) are secrets too.
    r"\.config/(?:google-chrome|chromium|microsoft-edge|bravesoftware)", r"snap/(?:chromium|firefox)/common",
    r"\.mozilla/firefox", r"mozilla/firefox/profiles", r"google/chrome/user data", r"microsoft/edge/user data",
    r"bravesoftware/brave-browser/user data", r"chromium/user data",
    r"application support/(?:google/chrome|firefox|microsoft edge|bravesoftware|chromium)",
    r"snap/(?:brave|opera|microsoft-edge)[^/]*/common", r"\.var/app/(?:org\.mozilla\.firefox|com\.google\.chrome|"
    r"org\.chromium\.chromium|com\.brave\.browser|com\.microsoft\.edge)",
    r"library/(?:safari|cookies|webkit|containers/com\.apple\.safari)(?:/|\s|$|[\"'])",
    # The page storage folders and the cookie and state files by name, for a profile kept anywhere (--user-data-dir).
    r"local storage/leveldb", r"session storage(?:/|\s|$|[\"'])", r"(?:^|/)indexeddb(?:/|\s|$|[\"'])",
    r"(?:default|profile \d+)/(?:cookies|local state)(?:-journal)?(?:\s|$|[\"'])", r"user data/local state",
    # The cookie store of every browser profile, named or searched for: the `Cookies` file and `Network/Cookies` of
    # Chromium family profiles, `cookies.sqlite` of Firefox, the bare name in `find -name`, `-Filter` and `-Include`,
    # and the obvious globs (`Coo*`, `*ookies`, `[cC]ookies`).
    _B + r"(?:network/)?cookies(?:-journal)?(?:\s|$|[\"'*,;)])", r"cookies\.sqlite(?:-wal|-shm)?",
    _B + r"(?:coo|cook|cooki|cookie|cookies)[*?]", r"[*?](?:ookies|okies|kies)(?:\s|$|[\"'])", r"\[cc\]ookies",
    # The device key of the remembered session lives in the IndexedDB of the origin of this installation, so that store
    # is named, searched for and globbed like the cookie one: the Chromium `.indexeddb.leveldb` and `.indexeddb.blob`
    # folders, the word `indexeddb` after any character that is not a letter or a digit, the obvious globs (`index*`,
    # `*ndexeddb*`, `[iI]ndexed*`), the origin folder of the panel (`hokma.localhost_<port>`) and the Firefox
    # `storage/default/` folder of that origin.
    r"\.indexeddb\.(?:leveldb|blob)", r"(?:^|[^a-z0-9])indexeddb",
    _B + r"index[*?]", r"[*?]ndexeddb", r"\[ii\]ndexed", r"indexed[*?]", r"[*?]ndexed",
    r"hokma\.localhost", r"storage/default/(?:https?\+\+\+)?hokma\.localhost",
    # Other processes' memory, environment and command line (the token is in none of them, but they are how a
    # process's secrets are read): `self` is the agent's own process and stays free.
    r"/proc/(?!self/|thread-self/)[^/\s\"']+/(?:mem|environ|cmdline|maps|smaps)(?:\s|$|[\"'])",
]))
# Inside Hokma's data folder these stay secret; the rest of it (plugins, settings, logs, tools) is readable and writable.
DATA_SECRET_TOPS = ("database.json", "hokma.db", "identity.key", "codex-home", "acp-home", "backups")
CONFIG_SECRET_PREFIXES = (".claude.json", ".credentials", "credentials.json", "backups")
CRED_CODE = re.compile(r"engine_key|\bimport\s+keyring\b|\bfrom\s+keyring\b|\bkeyring\s*\.\s*(?:get|set|delete)|"
                       r"\bPasswordVault\b|\bCredRead\b|Get-StoredCredential|\bsecretstorage\b|"
                       r"(?:import_module|__import__)\s*\(\s*[\"']keyring[\"']|"          # loading it by name at run time
                       r"[\"']key[\"']\s*\+\s*[\"']ring[\"']|[\"']keyr[\"']\s*\+\s*[\"']ing[\"']", re.I)


def data_secret_in_text(low: str, ctx: Ctx) -> bool:
    """The text names a secret inside the data folder by its real location (a folder that is not called `hokma`)."""
    data = norm_text(ctx.data).rstrip("/")
    if len(data) <= 3:
        return False
    start = 0
    while True:
        k = low.find(data, start)
        if k < 0:
            return False
        after = low[k + len(data):]
        if re.match(r"/(?:" + "|".join(re.escape(x) for x in DATA_SECRET_TOPS) + r")", after) or re.match(
                r"/claude-config/(?:" + "|".join(re.escape(x) for x in CONFIG_SECRET_PREFIXES) + r")", after):
            return True
        start = k + 1


DATA_PLACE_RE = re.compile(r"(?:localappdata%?|appdata/local|application support|\.local/share|xdg_data_home\}?)/hokma"
                           r"(?:/|\s|$|[\"'])")


def names_data_folder(low: str, ctx: Ctx) -> bool:
    """The text names Hokma's data folder: by its default places on each system, or by the real folder."""
    data = norm_text(ctx.data)
    return bool(DATA_PLACE_RE.search(low)) or (len(data) > 3 and data in low)


def names_protected(low: str, ctx: Ctx) -> bool:
    return any(p in low for p in ctx._protect_text)


def scan_secret_text(text: str) -> str | None:
    low = norm_text(text)
    m = SECRET_RE.search(low)
    return m.group(0).strip(" \"'=:;,()<>|&/@") if m else None


def check_text_markers(text: str, ctx: Ctx, *, reading: bool = False) -> None:
    """Secrets (and, unless the command only reads, the hook files) named in a text that names a path or in code."""
    expanded = expand(text, ctx)
    for candidate in (text, expanded):
        if scan_secret_text(candidate) or data_secret_in_text(norm_text(candidate), ctx):
            raise Block("secrets", short(text))
    if not reading:
        for candidate in (norm_text(text), norm_text(expanded)):
            if names_protected(candidate, ctx):
                raise Block("hokma_internal", short(text))


def check_real_path(path: str, ctx: Ctx, *, reading: bool = False, here: str | None = None, guarded: bool = True) -> None:
    """Resolve a path (relative to the call's folder) and refuse the secret places and, unless the command only reads,
    the hook files by identity. `guarded=False`: the hook files are judged elsewhere (`guard.decide`)."""
    if unresolved(path) and not path.startswith("~"):
        return
    full = clean_path(joined(expand(path, ctx), here or ctx.cwd))
    if is_win_abs(full) and os.name != "nt":
        return                                                  # a Windows path on another system: text markers only
    for candidate in {full, os.path.realpath(full)}:
        if SECRET_RE.search(norm_text(candidate)) or (
                under_any(candidate, [ctx.data]) and read_hidden(candidate, ctx, content=False)):
            raise Block("secrets", short(path))
        if guarded and not reading and under_any(candidate, ctx.protect):
            raise Block("hokma_internal", short(path))


GLOB_CHARS = "*?["


def read_hidden(candidate: str, ctx: Ctx, *, content: bool) -> bool:
    """True when `candidate` (a real path under the data folder) is a secret: the database and its copies, the sign-in
    folders and files, and, when the command reads CONTENT, a folder that holds any of them (the data folder and the
    claude-config folder), because a recursive search or a glob would walk into the credentials."""
    data = _fold(os.path.realpath(ctx.data))                    # case-folded where the file system ignores case
    try:
        rel = os.path.relpath(_fold(candidate), data)
    except ValueError:
        return True
    if rel == ".":
        return content
    if rel.startswith(".."):
        return True                                             # judged inside the data folder but not below it: hide
    parts = rel.replace("\\", "/").lower().split("/")
    if parts[0] in DATA_SECRET_TOPS:
        return True
    if parts[0] == "claude-config":
        if len(parts) == 1:
            return content
        return any(x.startswith(CONFIG_SECRET_PREFIXES) for x in parts[1:2])
    return False


def check_read_path(path: str, ctx: Ctx, here: str, *, content: bool) -> None:
    """A path that a read-only command or tool names. Under the data folder it is readable unless it is a secret
    (`read_hidden`). A glob is judged by the folder before its first wildcard."""
    text = expand(path, ctx)
    if unresolved(text) and not text.startswith("~"):
        return
    full = clean_path(joined(text, here))
    if is_win_abs(full) and os.name != "nt":
        return
    cut = min((full.find(c) for c in GLOB_CHARS if c in full), default=-1)
    if cut >= 0:
        tail = full[cut:].replace("\\", "/")
        if ".." in tail.split("/") or any(part.startswith(".") for part in (full[:cut].replace("\\", "/").rsplit("/", 1)[-1] + tail).split("/")):
            if under_any(os.path.dirname(full[:cut]) or "/", [ctx.data]):
                raise Block("secrets", short(path))             # a glob that can reach dot files or climb out of its folder
        full = os.path.dirname(full[:cut]) or "/"
    for candidate in {full, os.path.realpath(full)}:
        if under_any(candidate, [ctx.data]) and read_hidden(candidate, ctx, content=content):
            raise Block("secrets", short(path))


def check_path_arg(text: str, ctx: Ctx, *, guarded: bool = True) -> None:
    check_text_markers(text, ctx) if guarded else check_text_markers(text, ctx, reading=True)
    check_real_path(text, ctx, guarded=guarded)


def is_device(p: str) -> bool:
    low = p.lower()
    return low in DEVICES or low.startswith(("/dev/fd/", "/proc/self/fd/"))


def check_write_dest(text: str, ctx: Ctx, cwd: str, *, guarded: bool = True) -> str | None:
    """A path a command or a tool writes to. Returns the resolved path (None when it cannot be known)."""
    if not text or text == "-":
        return None
    if is_device(text):
        return None
    check_path_arg(text, ctx, guarded=guarded)
    if unresolved(expand(text, ctx)):
        return None                                             # built at run time: cannot be judged here
    full = clean_path(joined(expand(text, ctx), cwd))
    if is_device(full):
        return None
    if not is_win_abs(full):
        check_real_path(full, ctx, guarded=guarded)             # relative to a folder the command moved into with `cd`
    real = full if is_win_abs(full) and os.name != "nt" else os.path.realpath(full)
    if not under(real, ctx.write_roots):
        raise Block("outside_workdir", short(text))
    return full


# --------------------------------------------------------------------------------------------------- lexer

class Word:
    __slots__ = ("text", "dyn", "glob", "quoted")

    def __init__(self, text: str, dyn: bool, glob: bool, quoted: bool) -> None:
        self.text, self.dyn, self.glob, self.quoted = text, dyn, glob, quoted

    def __repr__(self) -> str:                                  # pragma: no cover
        return f"Word({self.text!r})"


class Cmd:
    def __init__(self) -> None:
        self.words: list[Word] = []
        self.redirs: list[tuple[str, Word]] = []
        self.heredocs: list[str] = []
        self.herestrs: list[str] = []
        self.subs: list[str] = []
        self.pipe_in = False
        self.call = False                       # PowerShell: this command follows the call operator `&`

    def empty(self) -> bool:
        return not (self.words or self.redirs or self.heredocs or self.herestrs or self.subs)


_NAME_START = re.compile(r"[A-Za-z_]")
_SIMPLE_VAR = re.compile(r"\$(?:env:)?[A-Za-z_]\w*|\$[0-9@#?*!$-]")
_ESC = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "f": "\f", "v": "\v", "e": "\x1b", "\\": "\\",
        "'": "'", '"': '"', "?": "?"}


def ansi_c(body: str) -> str:
    out, i = [], 0
    while i < len(body):
        c = body[i]
        if c != "\\" or i + 1 >= len(body):
            out.append(c)
            i += 1
            continue
        n = body[i + 1]
        if n == "x":
            m = re.match(r"[0-9a-fA-F]{1,2}", body[i + 2:])
            if m:
                out.append(chr(int(m.group(0), 16)))
                i += 2 + len(m.group(0))
                continue
        if n in "01234567":
            m = re.match(r"[0-7]{1,3}", body[i + 1:])
            out.append(chr(int(m.group(0), 8) & 0xFF))
            i += 1 + len(m.group(0))
            continue
        if n == "u" or n == "U":
            m = re.match(r"[0-9a-fA-F]{1,8}", body[i + 2:])
            if m:
                try:
                    out.append(chr(int(m.group(0), 16)))
                except (ValueError, OverflowError):
                    pass
                i += 2 + len(m.group(0))
                continue
        out.append(_ESC.get(n, "\\" + n))
        i += 2
    return "".join(out)


def match_paren(text: str, i: int) -> int:
    """Index just after the `)` that closes the `(` already consumed (text[i] is the first inner character).
    Quotes, nested parentheses, backticks and here-documents are skipped. An unclosed group runs to the end."""
    depth, n = 1, len(text)
    pending: list[tuple[str, bool]] = []
    while i < n:
        c = text[i]
        if c == "\n" and pending:
            i += 1
            for delim, strip in pending:
                while i < n:
                    end = text.find("\n", i)
                    line = text[i:] if end < 0 else text[i:end]
                    i = n if end < 0 else end + 1
                    if (line.lstrip("\t") if strip else line).rstrip("\r") == delim:
                        break
            pending = []
            continue
        elif c == "\\":
            i += 2
            continue
        elif c == "'":
            end = text.find("'", i + 1)
            i = n if end < 0 else end + 1
            continue
        elif c == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\":
                    i += 1
                elif text[i] == "$" and text[i + 1:i + 2] == "(":
                    i = match_paren(text, i + 2) - 1
                i += 1
            i += 1
            continue
        elif c == "`":
            end = text.find("`", i + 1)
            i = n if end < 0 else end + 1
            continue
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        elif c == "<" and text[i:i + 2] == "<<" and text[i:i + 3] != "<<<":
            m = re.match(r"<<(-?)\s*(?:\\?([A-Za-z_][\w.-]*)|'([^']*)'|\"([^\"]*)\")", text[i:])
            if m:
                pending.append((m.group(2) or m.group(3) or m.group(4) or "", bool(m.group(1))))
                i += m.end()
                continue
        i += 1
    return n


def lex(text: str, mode: str) -> list[Cmd]:
    """Split `text` into simple commands. mode: `bash`, `ps` (PowerShell) or `cmd`."""
    ps, cmdx = mode == "ps", mode == "cmd"
    cmds: list[Cmd] = []
    cur = Cmd()
    buf: list[str] = []
    st = {"dyn": False, "glob": False, "quoted": False, "any": False}
    redir: list = [None]                    # operator waiting for its target word
    hd_wait: list = [None]                  # (strip_tabs) while the here-document delimiter is expected
    here_wait = [False]
    pending_hd: list[tuple[str, bool, Cmd]] = []
    n, i = len(text), 0

    def reset() -> None:
        buf.clear()
        st.update(dyn=False, glob=False, quoted=False, any=False)

    def end_word() -> None:
        nonlocal cur
        if not st["any"]:
            return
        w = Word("".join(buf), st["dyn"], st["glob"], st["quoted"])
        reset()
        if hd_wait[0] is not None:
            pending_hd.append((w.text, hd_wait[0], cur))
            hd_wait[0] = None
        elif here_wait[0]:
            cur.herestrs.append(w.text)
            here_wait[0] = False
        elif redir[0] is not None:
            op = redir[0]
            redir[0] = None
            if op in (">&", "<&") and (w.text.isdigit() or w.text == "-"):
                return
            cur.redirs.append((op, w))
        else:
            cur.words.append(w)

    def end_cmd(sep: str) -> None:
        nonlocal cur
        end_word()
        redir[0] = None
        if not cur.empty():
            cmds.append(cur)
        cur = Cmd()
        cur.pipe_in = sep in ("|", "|&")
        cur.call = ps and sep == "&"

    def add(ch: str) -> None:
        buf.append(ch)
        st["any"] = True

    def read_heredocs(pos: int) -> int:
        for delim, strip, owner in pending_hd:
            lines = []
            while pos < n:
                end = text.find("\n", pos)
                line = text[pos:] if end < 0 else text[pos:end]
                pos = n if end < 0 else end + 1
                if (line.lstrip("\t") if strip else line).rstrip("\r") == delim:
                    break
                lines.append(line)
            owner.heredocs.append("\n".join(lines))
        pending_hd.clear()
        return pos

    def dollar(pos: int, quoted: bool) -> int:
        """Handle `$` at text[pos]; returns the next index."""
        nxt = text[pos + 1:pos + 2]
        if nxt == "(":
            if text[pos + 2:pos + 3] == "(":                    # arithmetic: not a command
                end = text.find("))", pos + 3)
                end = n if end < 0 else end + 2
                add("\0")
                st["dyn"] = True
                return end
            end = match_paren(text, pos + 2)
            inner = text[pos + 2:end - 1] if text[end - 1:end] == ")" else text[pos + 2:end]
            cur.subs.append(inner)
            add("\0")
            st["dyn"] = True
            return end
        if nxt == "{":
            depth, j = 1, pos + 2
            while j < n and depth:
                depth += (text[j] == "{") - (text[j] == "}")
                j += 1
            add(text[pos:j])
            st["dyn"] = True
            return j
        if nxt == "'" and not quoted and not ps and not cmdx:
            end = pos + 2
            while end < n and text[end] != "'":
                end += 2 if text[end] == "\\" else 1
            add(ansi_c(text[pos + 2:end]))
            st["quoted"] = True
            return end + 1
        m = _SIMPLE_VAR.match(text, pos)
        if m:
            add(m.group(0))
            st["dyn"] = True
            return m.end()
        add("$")
        return pos + 1

    def double_quoted(pos: int) -> int:
        """text[pos] is the opening quote; returns the index after the closing one."""
        st["quoted"] = True
        st["any"] = True
        pos += 1
        while pos < n and text[pos] != '"':
            c = text[pos]
            if c == "\\" and not ps and not cmdx and pos + 1 < n:
                nx = text[pos + 1]
                if nx in '$`"\\':
                    add(nx)
                    pos += 2
                    continue
                if nx == "\n":
                    pos += 2
                    continue
                add("\\")
                pos += 1
                continue
            if c == "`" and ps and pos + 1 < n:
                add(text[pos + 1])
                pos += 2
                continue
            if c == '"' and ps and text[pos + 1:pos + 2] == '"':
                add('"')
                pos += 2
                continue
            if c == "$" and not cmdx:
                pos = dollar(pos, True)
                continue
            if c == "`" and not ps and not cmdx:
                end = text.find("`", pos + 1)
                end = n if end < 0 else end
                cur.subs.append(text[pos + 1:end])
                add("\0")
                st["dyn"] = True
                pos = end + 1
                continue
            add(c)
            pos += 1
        return pos + 1

    while i < n:
        c = text[i]
        if c in " \t":
            end_word()
            i += 1
        elif c == "\r":
            i += 1
        elif c == "\n":
            end_cmd("\n")
            i += 1
            if pending_hd:
                i = read_heredocs(i)
        elif c == "#" and not st["any"] and not cmdx:
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif c == "\\" and not ps and not cmdx:
            nx = text[i + 1:i + 2]
            if nx == "\n":
                i += 2
            elif nx == "":
                i += 1
            elif _DRIVE_PREFIX.match("".join(buf)) and nx.isalnum():
                add("\\")                                       # C:\Users\... written with plain backslashes
                i += 1
            else:
                add(nx)
                st["quoted"] = True
                i += 2
        elif c == "^" and cmdx:
            if i + 1 < n:
                add(text[i + 1])
                i += 2
            else:
                i += 1
        elif c == "`" and ps:
            nx = text[i + 1:i + 2]
            if nx == "\n":
                i += 2
            elif nx:
                add(nx)
                st["quoted"] = True
                i += 2
            else:
                i += 1
        elif c == "'" and not cmdx:
            st["quoted"] = True
            st["any"] = True
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if ps and text[j + 1:j + 2] == "'":
                        add("'")
                        j += 2
                        continue
                    break
                add(text[j])
                j += 1
            i = j + 1
        elif c == '"':
            i = double_quoted(i)
        elif c == "`" and not cmdx:
            end = text.find("`", i + 1)
            end = n if end < 0 else end
            cur.subs.append(text[i + 1:end])
            add("\0")
            st["dyn"] = True
            i = end + 1
        elif c == "$" and not cmdx:
            i = dollar(i, False)
        elif c in "<>" and not ps and text[i + 1:i + 2] == "(" and not cmdx:
            end = match_paren(text, i + 2)
            cur.subs.append(text[i + 2:end - 1] if text[end - 1:end] == ")" else text[i + 2:end])
            add("\0")
            st["dyn"] = True
            i = end
        elif c in "<>" or (c == "&" and text[i + 1:i + 2] == ">" and not ps):
            # redirection operators
            fd_only = st["any"] and "".join(buf).isdigit() and not st["quoted"]
            if fd_only:
                reset()
            else:
                end_word()
            if c == "&":
                op, i = ">", i + 1
            else:
                op = c
            if op == ">":
                if text[i + 1:i + 2] == ">":
                    op, i = ">>", i + 1
                elif text[i + 1:i + 2] == "|":
                    op, i = ">|", i + 1
                elif text[i + 1:i + 2] == "&":
                    op, i = ">&", i + 1
            elif op == "<" and not ps:
                if text[i + 1:i + 3] == "<<":
                    op, i = "<<<", i + 2
                elif text[i + 1:i + 2] == "<":
                    op, i = "<<", i + 1
                    j = i + 1
                    if text[j:j + 1] == "-":
                        hd_wait[0], i = True, i + 1
                    else:
                        hd_wait[0] = False
                elif text[i + 1:i + 2] == "&":
                    op, i = "<&", i + 1
            elif ps:
                add(c)
                i += 1
                continue
            if op == "<<<":
                here_wait[0] = True
            elif op != "<<":
                redir[0] = op
            i += 1
        elif c in ";|&()" or (c in "{}" and ps):
            end_word()
            if c == "|":
                if text[i + 1:i + 2] in ("|", "&"):
                    sep, i = "|" + text[i + 1], i + 1
                else:
                    sep = "|"
                end_cmd(sep)
            elif c == "&":
                if text[i + 1:i + 2] == "&":
                    i += 1
                    end_cmd("&&")
                else:
                    end_cmd("&")
            else:
                end_cmd(c)
            i += 1
        elif c in "*?[" or (c == "{" and "," in text[i:i + 40]):
            st["glob"] = True
            add(c)
            i += 1
        else:
            add(c)
            i += 1
    end_cmd("\n")
    if pending_hd:
        read_heredocs(n)
    return cmds


# ------------------------------------------------------------------------------------------ command tables

DELETE_NAMES = {"rm", "rmdir", "unlink", "shred", "del", "erase", "rd", "remove-item", "ri", "srm", "deltree"}
DYNAMIC_EXEC = {"eval", "iex", "invoke-expression"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "ash", "mksh", "csh", "tcsh"}
PS_NAMES = {"powershell", "pwsh"}
SKIP_WORDS = {"{", "}", "!", "then", "else", "elif", "if", "do", "while", "until", "fi", "done", "esac", "time",
              "coproc", "function"}
EXE_SUFFIX = re.compile(r"\.(exe|cmd|bat|com|ps1|sh)$", re.I)
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
PY_NAME = re.compile(r"^(python[\d.]*|pythonw[\d.]*|py|pypy[\d.]*)$")
NODE_NAMES = {"node", "nodejs", "deno", "bun"}
WRAPPERS: dict[str, set[str]] = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-r", "-t", "-D", "-R", "-T", "--user", "--group", "--host", "--prompt",
             "--chdir"},
    "doas": {"-u", "-C"}, "env": {"-u", "-C", "-S", "--unset", "--chdir"}, "nohup": set(), "exec": {"-a"},
    "builtin": set(), "command": set(), "nice": {"-n", "--adjustment"}, "ionice": {"-c", "-n", "-p", "-P", "-u"},
    "setsid": set(), "stdbuf": {"-i", "-o", "-e"}, "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "xargs": {"-I", "-n", "-P", "-L", "-E", "-d", "-s", "-a", "--max-args", "--max-procs", "--delimiter",
              "--arg-file", "--max-lines", "--replace"},
    "watch": {"-n", "-d"}, "busybox": set(), "wsl": {"-d", "-u", "--distribution", "--user", "--cd"},
    "chronic": set(), "unbuffer": set(), "flock": set(), "script": set(),
}
FORMAT_NAMES = {"format", "diskpart", "format-volume", "clear-disk", "initialize-disk", "bcdedit", "wipefs",
                "mkswap", "sgdisk", "mklabel"}
WRITE_LAST = {"cp", "mv", "ln", "install", "rsync", "scp", "copy", "xcopy", "move", "robocopy", "copy-item", "cpi",
              "move-item", "mi", "rename-item", "rni", "ren", "rename"}
PS_PATH_WRITERS = {"new-item", "ni", "set-content", "sc", "add-content", "ac", "out-file", "export-csv",
                   "export-clixml", "tee-object"}
CD_NAMES = {"cd", "pushd", "chdir", "set-location", "sl"}
SCRIPT_SUFFIX = {".sh": "sh", ".bash": "sh", ".zsh": "sh", ".py": "py", ".js": "node", ".mjs": "node",
                 ".cjs": "node", ".ts": "node", ".ps1": "ps", ".bat": "cmd", ".cmd": "cmd", ".rb": "ruby",
                 ".pl": "perl"}

PY_DELETE = re.compile(
    r"\bos\s*\.\s*(?:remove|unlink|rmdir|removedirs)\s*\(|\bshutil\s*\.\s*rmtree\s*\(|\b(?:rmtree|removedirs)\s*\(|"
    r"\.\s*(?:unlink|rmdir)\s*\(|\bfrom\s+os\s+import\b[^\n]*\b(?:remove|unlink|rmdir|removedirs)\b|"
    r"\bfrom\s+shutil\s+import\b[^\n]*\brmtree\b")
NODE_FS = re.compile(r"""(?:require\s*\(|from\s*|import\s*\()\s*['"](?:node:)?fs(?:/promises)?['"]""")
NODE_DELETE = re.compile(
    r"\b(?:rmSync|unlinkSync|rmdirSync|rimraf|rimrafSync|removeSync|emptyDirSync)\s*\(|"
    r"\bfsp?\s*\.\s*(?:promises\s*\.\s*)?(?:rm|unlink|rmdir)\s*\(|\.\s*promises\s*\.\s*(?:rm|unlink|rmdir)\s*\(")
NODE_BARE = re.compile(r"(?<![\w.])(?:rm|unlink|rmdir)\s*\(")
RUBY_DELETE = re.compile(r"\bFile\s*\.\s*(?:delete|unlink)\b|\bFileUtils\s*\.\s*(?:rm|rm_f|rm_r|rm_rf|remove\w*)\b|"
                         r"\b(?:rm_rf|rm_r|remove_entry)\b|\bDir\s*\.\s*(?:rmdir|delete|unlink)\b")
PERL_DELETE = re.compile(r"\bunlink\b|\brmtree\b|\brmdir\b|\bremove_tree\b")
PS_STATIC = re.compile(r"\[\s*(?:system\.)?io\.(?:file|directory|fileinfo|directoryinfo)\s*\]\s*::\s*delete|"
                       r"\.\s*delete\s*\(", re.I)
EXEC_HINT = re.compile(r"subprocess|os\s*\.\s*(?:system|popen|exec\w*|spawn\w*)|child_process|execSync|spawnSync|"
                       r"\bexec\s*\(|\bsystem\s*\(|Popen|Process\s*\.\s*Start", re.I)
STR_LITERAL = re.compile(r"""'((?:[^'\\\n]|\\.)*)'|"((?:[^"\\\n]|\\.)*)\"""")
SKIP_PARTS = {"node_modules", ".venv", "venv", "site-packages", ".git", "__pycache__", "dist-packages", ".tox"}
SHELL_DELETE_WORD = {"rm", "rmdir", "unlink", "shred", "remove-item", "del", "erase", "rd"}


def base_name(text: str) -> str:
    """Command name of a word: last path segment, lower case, without `.exe` and friends."""
    name = re.split(r"[\\/]", text)[-1].lower()
    return EXE_SUFFIX.sub("", name)


# ----------------------------------------------------------------------------------------------- analysis

class Run:
    """State of one analysis: the context, how deep we are, whether names built at run time are refused."""

    def __init__(self, ctx: Ctx, cwd: str, strict: bool = True) -> None:
        self.ctx, self.cwd, self.strict = ctx, [cwd], strict
        self.reading = False                    # the whole command only reads: Hokma's data folder may be read
        self.source = ""                        # the command line of the call, for the one idiom PowerShell splits up

    @property
    def here(self) -> str:
        return self.cwd[0]


def analyze_text(text: str, mode: str, run: Run, depth: int, stdin_text: str | None = None) -> None:
    if depth > MAX_DEPTH:
        raise Block("delete", short(text))
    if mode == "ps":
        if PS_STATIC.search(text):
            raise Block("delete", short(text))
        if CRED_CODE.search(text):
            raise Block("secrets", short(text))
    prev: Cmd | None = None
    for cmd in lex(text, mode):
        for inner in cmd.subs:
            analyze_text(inner, "bash" if mode == "bash" else mode, run, depth + 1)
        analyze_cmd(cmd, prev if cmd.pipe_in else None, mode, run, depth, stdin_text)
        prev = cmd


def significant(words: list[Word], mode: str) -> list[Word]:
    """Drop leading assignments and grouping keywords."""
    i = 0
    while i < len(words):
        w = words[i]
        if (w.text in SKIP_WORDS and not w.quoted) or (ASSIGNMENT.match(w.text) and not w.quoted):
            i += 1
            continue
        break
    return words[i:]


def analyze_cmd(cmd: Cmd, prev: Cmd | None, mode: str, run: Run, depth: int, stdin_text: str | None) -> None:
    words = significant(cmd.words, mode)
    check_redirections(cmd, words, prev, mode, run)
    if not words:
        return
    if mode == "ps" and not cmd.call and words[0].text.startswith(("$", "[")):
        return                                  # `$x.Name` is an expression in PowerShell, not a command call
    name = command_name(words[0], run)
    args = words[1:]
    stdin = stdin_for(cmd, prev, mode, run)
    if stdin is None:
        stdin = stdin_text
    scan_words(name, words, cmd, run)
    dispatch(name, words, args, cmd, prev, mode, run, depth, stdin)


def command_name(w: Word, run: Run) -> str:
    text = w.text
    last = re.split(r"[\\/]", text)[-1]
    if run.strict and ("$" in last or "\0" in last or "`" in last or (w.glob and last not in ("[", "[["))):
        raise Block("delete", short(text.replace("\0", "$(...)")))
    return base_name(text)


def stdin_for(cmd: Cmd, prev: Cmd | None, mode: str, run: Run) -> str | None:
    """Text that reaches the command through stdin, when it can be known."""
    if cmd.heredocs:
        return "\n".join(cmd.heredocs)
    if cmd.herestrs:
        return "\n".join(cmd.herestrs)
    for op, w in cmd.redirs:
        if op == "<" and not unresolved(w.text):
            text = read_script(w.text, run)
            if text is not None:
                return text
    if prev is not None:
        pw = significant(prev.words, mode)
        if pw:
            pname = base_name(pw[0].text)
            if pname in ("echo", "printf", "write-output", "write-host"):
                return " ".join(w.text for w in pw[1:] if not w.text.startswith("-"))
            if pname in ("cat", "type", "get-content", "gc") and len(pw) > 1:
                return read_script(pw[-1].text, run)
            if pname == "cat" and (prev.heredocs or prev.herestrs):           # cat <<EOF | sh
                return "\n".join(prev.heredocs or prev.herestrs)
            if pname in ("base64", "openssl", "xxd", "gunzip", "zcat", "gzip", "unzip", "certutil", "decode"):
                return "\0DECODER"
    return None


def read_script(path: str, run: Run, limit: int = MAX_SCRIPT_BYTES) -> str | None:
    ctx = run.ctx
    if unresolved(path):
        return None
    full = clean_path(joined(expand(path, ctx), run.here))
    if is_win_abs(full) and os.name != "nt":
        return None
    try:
        real = os.path.realpath(full)
        if not under(real, ctx.roots) or not os.path.isfile(real) or os.path.getsize(real) > limit:
            return None
        if SKIP_PARTS & set(real.replace("\\", "/").split("/")):
            return None                                         # dependencies and tool launchers, not the student's code
        if real in ctx.seen:
            return None
        ctx.seen.add(real)
        with open(real, "rb") as f:
            data = f.read(limit + 1)
    except OSError:
        return None
    if b"\0" in data[:4096]:
        return None
    return data.decode("utf-8", "replace")


def check_redirections(cmd: Cmd, words: list[Word], prev: Cmd | None, mode: str, run: Run) -> None:
    ctx = run.ctx
    for op, w in cmd.redirs:
        if op in ("<", "<<", "<<<", "<&"):
            check_path_arg(w.text, ctx, guarded=False)          # reading a file is free, what it holds is not
            continue
        if op == ">&" and (w.text.isdigit() or w.text == "-"):
            continue
        check_write_dest(w.text, ctx, run.here)


# Tools that walk a whole tree. Pointed at a folder that CONTAINS the data folder (`~`, `~/.local/share`,
# `%LOCALAPPDATA%`, `/`) they read the database and the credentials, so that is a read of the data folder's content.
RECURSIVE_ALWAYS = {"ugrep", "ug", "rg", "ag", "ack", "rgrep", "findstr", "select-string", "sls"}
RECURSIVE_FLAG = {"grep": "rRd", "egrep": "rRd", "fgrep": "rRd", "cp": "rRa", "scp": "r", "rsync": "ra",
                  "zip": "rR", "7z": "", "7za": "", "tar": "", "pax": "", "cpio": "", "xcopy": "", "robocopy": "",
                  "copy-item": "", "cpi": "", "compress-archive": ""}


def _recursive_call(name: str, words: list[Word]) -> bool:
    if name == "find":                                   # listing names reads nothing; `-exec` and `-fprint` do
        return any(w.text.lower() in FIND_WRITERS - {"-delete"} for w in words[1:])
    if name in RECURSIVE_ALWAYS:
        return not (name in ("select-string", "sls") and not any(
            w.text.lower() == "-recurse" for w in words[1:])) and not (
            name == "findstr" and not any(w.text.lower().startswith("/s") for w in words[1:]))
    if name in RECURSIVE_FLAG:
        letters = RECURSIVE_FLAG[name]
        for w in words[1:]:
            tx = w.text
            if tx in ("-recurse",) or (tx.startswith("--") and (tx[2:].startswith(("rec", "dereference-rec", "arch"))
                                                                or tx.startswith("--dir"))) or (
                    letters and re.match(r"^-[A-Za-z]+$", tx) and any(c in tx[1:] for c in letters)) or (
                    name == "grep" and tx.startswith("--directories=recurse")):
                return True
        return name in ("tar", "pax", "cpio", "7z", "7za", "xcopy", "robocopy", "copy-item", "cpi", "compress-archive")
    return False


def _contains_data(full: str, ctx: Ctx) -> bool:
    """True when the folder `full` is, or is an ancestor of, the real data folder, or a glob of it can match it."""
    data = _fold(os.path.realpath(ctx.data))
    cut = min((full.find(c) for c in GLOB_CHARS if c in full), default=-1)
    glob_part = ""
    if cut >= 0:
        seg = max(full.rfind("/", 0, cut), full.rfind("\\", 0, cut))     # the whole segment that holds the glob
        glob_part = full[seg + 1:].replace("\\", "/")
        full = full[:seg] or "/"
    for candidate in {full, os.path.realpath(full)}:
        base = _fold(os.path.realpath(candidate)) if not glob_part else _fold(candidate)
        try:
            rel = os.path.relpath(data, base)
        except ValueError:
            continue
        if rel.startswith(".."):
            continue
        if not glob_part:
            return True
        first = rel.replace("\\", "/").split("/")[0]
        if first == "." or fnmatch.fnmatch(first, _fold(glob_part.split("/")[0])):
            return True
    return False


def _exclude_value(v: str) -> str:
    v = _fold(v.strip("\"'").lstrip("!"))                       # case-folded only where the file system ignores case
    for prefix in ("**/", "*/", "./"):
        while v.startswith(prefix):
            v = v[len(prefix):]
    for suffix in ("/**", "/*", "/"):
        while v.endswith(suffix):
            v = v[:-len(suffix)]
    return v


def _excludes_data(name: str, words: list[Word], base: str) -> bool:
    """True only for a (tool, option) pair that cuts a whole directory tree out and whose value is exactly the data
    folder's name: grep and rg `--exclude-dir=hokma`, rg `-g '!hokma'`, tar and rsync `--exclude=hokma`, zip
    `-x '*/hokma/*'`. Anything else (`find -prune`, grep `--exclude`, an include glob, a looser mention) is not."""
    texts = [w.text for w in words]
    if any(t.lower().startswith(("--include", "--iglob", "--files-from", "--filter", "--anchored", "--no-ignore",
                                 "-T", "--from-file", "-f", "-F")) for t in texts) or any(
            t in ("-g", "--glob") and i + 1 < len(texts) and not texts[i + 1].strip("\"'").startswith("!")
            or t.startswith("--glob=") and not t[7:].strip("\"'").startswith("!") for i, t in enumerate(texts)):
        return False                                             # an include or a file list can undo the exclusion
    for i, t in enumerate(texts):
        low = t.lower()
        opt, value = (low.split("=", 1)[0], t.split("=", 1)[1]) if low.startswith("--") and "=" in low else (
            low, texts[i + 1] if i + 1 < len(texts) else None)
        if value is None:
            continue
        if name in ("grep", "egrep", "fgrep", "rgrep", "rg", "ag", "ack") and opt == "--exclude-dir":
            ok = _exclude_value(value) == base
        elif name == "rg" and opt in ("-g", "--glob"):
            ok = value.strip("\"'").startswith("!") and _exclude_value(value) == base
        elif name in ("tar", "rsync") and opt == "--exclude":
            ok = _exclude_value(value) == base
        else:
            continue
        if ok:
            return True
    return False


def check_recursive(name: str, words: list[Word], run: Run) -> None:
    ctx = run.ctx
    if not _recursive_call(name, words):
        return
    # Fail closed: EVERY non-option word is a possible root (a pattern given with -e/-f, `find -H`, option values).
    operands = [w for w in words[1:] if not w.text.startswith("-") or w.text == "-"]
    extra = [Word(w.text.split("=", 1)[1], False, False, False) for w in words[1:]
             if w.text.startswith("--") and "=" in w.text and not w.text.endswith("=")]   # `--directory=x`
    extra += [Word(v, False, False, False) for w in words[1:] if w.text.startswith("-") and not w.text.startswith("--")
              for v in _option_values(w.text)]               # `-C/dir`, `-Cdir` glued to a short option
    operands += extra
    if name in ("grep", "egrep", "fgrep", "rg", "findstr", "select-string", "sls", "ag", "ack", "rgrep", "fd"):
        if len(operands) <= 1:
            operands = operands + [Word(".", False, False, False)]      # no path: it walks the folder it runs in
    elif name == "find" and not operands:
        operands = [Word(".", False, False, False)]
    excluded = _excludes_data(name, words, _fold(os.path.basename(os.path.realpath(ctx.data))))
    follows = any(w.text in ("-R", "-L", "-h", "-follow", "-H", "--follow", "--dereference", "--copy-links",
                             "--dereference-recursive") or w.text.startswith(("--deref", "--follow"))
                  or re.match(r"^-[A-Za-z]*[RLh][A-Za-z]*$", w.text) and name in ("grep", "egrep", "fgrep", "tar", "rg",
                                                                               "rsync", "cp") for w in words[1:])
    if name == "tar" and len(words) > 1 and re.match(r"^[A-Za-z]*h[A-Za-z]*$", words[1].text):
        follows = True                                           # old style `tar chf`
    if follows:
        excluded = False                                         # a link can lead into the folder by another name
    for w in operands:
        text = expand(w.text, ctx)
        if w.text.startswith("~") and not re.match(r"^~(?:$|[/\\])", w.text) and w not in extra:
            raise Block("secrets", short(w.text))         # `~user`, `~+`: a home the check cannot see
        if (unresolved(text) and not text.startswith("~")) or re.search(r"\{[^}]*(?:,|\.\.)[^}]*\}", text):
            raise Block("secrets", short(w.text))         # built at run time or by brace expansion: fail closed
        full = clean_path(joined(text, run.here))
        if is_win_abs(full) and os.name != "nt":
            continue                                             # a Windows path on another system: text markers only
        if _contains_data(full, ctx) and (
                not excluded or _fold(os.path.realpath(full)) == _fold(os.path.realpath(ctx.data))):
            raise Block("secrets", short(w.text))


def scan_words(name: str, words: list[Word], cmd: Cmd, run: Run) -> None:
    """Markers of secrets (and of the hook files) anywhere in the arguments, plus the host list of brokers."""
    ctx = run.ctx
    args = [w for w in words[1:]]
    if name in ("echo", "printf", "write-output", "write-host", "write-error", "write-warning"):
        scan = []
    elif name == "git" and any(w.text == "commit" for w in args):
        scan = [w for w in args if w.text.startswith("-") or not any(ch.isspace() for ch in w.text)]
    else:
        scan = list(words)
    text = " ".join(w.text for w in scan)
    if text:
        check_text_markers(text, ctx, reading=run.reading)
        content = name in CONTENT_READERS
        for w in scan:
            if run.reading and w is not words[0]:
                if unresolved(expand(w.text, ctx)) and not w.text.startswith("~"):
                    raise Block("secrets", short(w.text))                 # built at run time: could name a secret
                if (w.text.startswith("~") and not re.match(r"^~(?:$|[/\\])", w.text)) or (
                        "," in w.text and any(ch in w.text for ch in "/\\")):
                    raise Block("secrets", short(w.text))                 # `~+`, `~user`, a PowerShell path list
                if "{" in w.text or "}" in w.text:
                    raise Block("secrets", short(w.text))                 # brace expansion builds names the check cannot see
                if w.text.startswith("-"):
                    for value in _option_values(w.text):
                        check_read_path(value, ctx, run.here, content=content)
                if not w.text.startswith("-") and name not in CD_NAMES:
                    check_read_path(w.text, ctx, run.here, content=content)
                if name in SEARCHERS:
                    check_read_path(run.here, ctx, run.here, content=True)   # `grep -r x` with no path searches here
                elif name in CD_NAMES and not w.text.startswith("-"):
                    check_read_path(w.text, ctx, run.here, content=False)
            # Judged from the TRACKED folder (`cd x && cat y`) and by real path (links), also for a bare name.
            if any(ch in w.text for ch in "/\\") or w.text.startswith(("~", ".", "$", "%")) or (
                    w is not words[0] and not w.text.startswith("-") and not run.reading):
                check_real_path(w.text, ctx, reading=run.reading, here=run.here)
            if w.text.startswith("-") and not run.reading:
                for value in _option_values(w.text):
                    check_real_path(value, ctx, here=run.here)
        check_recursive(name, words, run)
    full = " ".join(w.text for w in words)
    if ctx.host_re and ctx.host_re.search(full) or ctx.url_re and ctx.url_re.search(full):
        raise Block("real_money", short(full))


DOWNLOADERS = {"curl", "wget", "irm", "iwr", "invoke-restmethod", "invoke-webrequest"}
# PowerShell reads `iex (irm url)` as `iex` followed by the group `(irm url)`: the line is judged as a whole.
PS_IEX_DOWNLOAD_RE = re.compile(r"\b(?:iex|invoke-expression)\b\s*\(+\s*(?:irm|iwr|invoke-restmethod|invoke-webrequest|"
                                r"\(?\s*new-object\s+net\.webclient\s*\)?\s*\.\s*downloadstring)\b", re.I)


def is_download(text: str, mode: str) -> bool:
    """The command text starts with a download tool (`curl -fsSL url`)."""
    try:
        first = significant(lex(text, mode)[0].words, mode)
    except (IndexError, ValueError):
        return False
    return bool(first) and base_name(first[0].text) in DOWNLOADERS


def installer_idiom(words: list[Word], cmd: Cmd, prev: Cmd | None, mode: str, source: str) -> bool:
    """`irm url | iex`, `eval "$(curl url)"`, `iex (irm url)`: the interpreter is fed ONLY by a download, the way software
    installers (Homebrew, Bun, Node managers) are run. Any literal code next to it (`eval rm f`) is not the idiom."""
    args = words[1:]
    if not args:
        if prev is not None and cmd.pipe_in:
            pw = significant(prev.words, mode)
            if pw and base_name(pw[0].text) in DOWNLOADERS:
                return True
        return mode == "ps" and bool(PS_IEX_DOWNLOAD_RE.search(source))
    return bool(cmd.subs) and all(w.text.replace("\0", "").strip() == "" for w in args) and all(
        is_download(x, mode) for x in cmd.subs)


def dispatch(name: str, words: list[Word], args: list[Word], cmd: Cmd, prev: Cmd | None, mode: str, run: Run,
             depth: int, stdin: str | None) -> None:
    ctx = run.ctx
    texts = [w.text for w in args]
    shown = short(" ".join(w.text for w in words))
    low = [x.lower() for x in texts]

    if name in DYNAMIC_EXEC and not installer_idiom(words, cmd, prev, mode, run.source):
        raise Block("delete", name)

    if name in WRAPPERS:
        inner = unwrap(name, args, mode)
        if inner:
            sub = Cmd()
            sub.words, sub.redirs, sub.heredocs, sub.herestrs, sub.pipe_in = inner, cmd.redirs, cmd.heredocs, \
                cmd.herestrs, cmd.pipe_in
            analyze_cmd_words(sub, prev, mode, run, depth, stdin)
        return

    if name in DELETE_NAMES:
        raise Block("delete", shown)

    if name in ("mv", "move-item", "mi", "move") and texts and is_null(texts[-1]):
        raise Block("delete", shown)

    if name == "find":
        find_rules(args, mode, run, depth)
        return

    if name == "git":
        git_rules(args, run)
        return

    if name in CD_NAMES:
        change_dir(args, run)
        return

    if name == "dd":
        for w in args:
            if w.text.lower().startswith("of="):
                target = w.text[3:]
                if is_device(target):
                    continue
                if target.startswith("/dev/") or is_win_abs(target) and target.startswith("\\\\.\\"):
                    raise Block("outside_workdir", short(w.text))
                check_write_dest(target, ctx, run.here)
        return

    if name.startswith("mkfs") or name in FORMAT_NAMES:
        raise Block("outside_workdir", shown)
    if name == "diskutil" and texts and re.match(r"(?:erase|partition|reformat|secureerase|zero)", low[0]):
        raise Block("outside_workdir", shown)
    if name == "reg" and low[:1] == ["delete"]:
        raise Block("outside_workdir", shown)
    if name == "vssadmin" and "delete" in low:
        raise Block("outside_workdir", shown)
    if name == "cipher" and any(x.startswith("/w") for x in low):
        raise Block("outside_workdir", shown)

    if name == "gh" and "auth" in low and ("token" in low or any(x in ("-t", "--show-token") for x in low)):
        raise Block("secrets", "gh auth token")
    if name == "cmdkey" and any(x.startswith(("/list", "/l")) for x in low) or name in ("vaultcmd", "get-storedcredential",
                                                                                          "secret-tool", "kwallet-query",
                                                                                          "keyring", "kwalletcli"):
        raise Block("secrets", short(name + " " + " ".join(texts)))
    if name == "security" and any(re.match(r"(?:find-[\w-]*password|dump-keychain|export)", x) for x in low):
        raise Block("secrets", short("security " + " ".join(texts)))

    if name in SHELLS or name in ("source", "."):
        procsub_scripts(args, cmd, mode, run, depth)
    if name in SHELLS:
        run_shell(name, args, cmd, mode, run, depth, stdin)
        return
    if name in PS_NAMES:
        run_powershell(args, cmd, run, depth, stdin)
        return
    if name == "cmd":
        run_cmd_exe(args, run, depth)
        return
    if name in ("source", "."):
        if texts:
            text = read_script(texts[0], run)
            if text is not None:
                analyze_text(text, "bash", Run(ctx, run.here, strict=False), depth + 1)
        return
    if name in ("start-process", "saps", "start"):
        start_process(args, mode, run, depth)
        return
    if PY_NAME.match(name):
        run_interpreter("py", args, cmd, run, depth, stdin)
        return
    if name in NODE_NAMES:
        run_interpreter("node", args, cmd, run, depth, stdin)
        return
    if name in ("ruby", "perl"):
        run_interpreter(name, args, cmd, run, depth, stdin)
        return

    # scripts run directly: ./do.sh, ./tool.py, script.ps1
    first = words[0]
    if (any(ch in first.text for ch in "/\\") or first.text.startswith(".")) and not unresolved(first.text):
        script_file(first.text, cmd, run, depth)

    write_rules(name, words, args, mode, run, cmd, prev)


def analyze_cmd_words(sub: Cmd, prev: Cmd | None, mode: str, run: Run, depth: int, stdin: str | None) -> None:
    words = significant(sub.words, mode)
    if not words:
        return
    name = command_name(words[0], run)
    scan_words(name, words, sub, run)
    dispatch(name, words, words[1:], sub, prev, mode, run, depth + 1 if depth < MAX_DEPTH else depth, stdin)


def is_null(text: str) -> bool:
    return text.lower() in ("/dev/null", "nul", "nul:", "$null")




def unwrap(name: str, args: list[Word], mode: str) -> list[Word]:
    """The command a wrapper runs: its words, after the wrapper's own options."""
    arity = WRAPPERS[name]
    i, n = 0, len(args)
    if name == "command" and any(a.text in ("-v", "-V", "-p") and a.text != "-p" for a in args):
        return []
    while i < n:
        t = args[i].text
        if t == "--":
            i += 1
            break
        if t in arity:
            i += 2
            continue
        if t.startswith("-") and len(t) > 1:
            i += 1
            continue
        if ASSIGNMENT.match(t) and name in ("env", "sudo", "nice", "nohup", "time", "exec"):
            i += 1
            continue
        if name == "timeout" and re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", t):
            i += 1
            continue
        break
    rest = args[i:]
    if name == "wsl" and rest and rest[0].text in ("-e", "--exec", "--"):
        rest = rest[1:]
    return rest


def find_rules(args: list[Word], mode: str, run: Run, depth: int) -> None:
    texts = [w.text for w in args]
    for i, x in enumerate(texts):
        low = x.lower()
        if low == "-delete":
            raise Block("delete", "find -delete")
        if low in ("-exec", "-execdir", "-ok", "-okdir"):
            j = i + 1
            while j < len(texts) and texts[j] not in (";", "+"):
                j += 1
            inner = args[i + 1:j]
            if inner:
                sub = Cmd()
                sub.words = list(inner)
                analyze_cmd_words(sub, None, mode, run, depth, None)


def change_dir(args: list[Word], run: Run) -> None:
    ctx = run.ctx
    operands = [w for w in args if not w.text.startswith("-") or w.text == "-"]
    if not operands:
        run.cwd[0] = ctx.home
        return
    target = operands[0].text
    if target == "-" or unresolved(expand(target, ctx)):
        return
    run.cwd[0] = clean_path(joined(expand(target, ctx), run.here))

def git_rules(args: list[Word], run: Run) -> None:
    """Git is free inside the working folder. What stays: the credential helper (a way to read stored credentials) and
    the places Git can write to outside the folders a command may write (`-C`, the destination of `clone` and `init`,
    the path of `worktree add`)."""
    ctx = run.ctx
    a = [w.text for w in args]
    i = 0
    base = run.here                                  # `-C` moves the folder the rest of the line is judged from
    while i < len(a):
        x = a[i]
        if x == "-C" and i + 1 < len(a):
            target = check_write_dest(a[i + 1], ctx, base)
            if target is not None:
                base = target
            i += 2
        elif x in ("-c", "--config-env", "--git-dir", "--work-tree", "--namespace", "--exec-path") and i + 1 < len(a):
            i += 2
        elif x.startswith("-"):
            i += 1
        else:
            break
    if i >= len(a):
        return
    sub, rest = a[i].lower(), [w for w in args[i + 1:]]
    if sub == "credential":
        raise Block("secrets", short("git " + " ".join(a)))
    paths = [w.text for w in rest if not w.text.startswith("-")]
    if sub == "clone" and len(paths) >= 2 and not re.search(r"://|^[\w.-]+@[\w.-]+:", paths[-1]):
        check_write_dest(paths[-1], ctx, base)
    elif sub == "init" and paths:
        check_write_dest(paths[-1], ctx, base)
    elif sub == "worktree" and len(paths) >= 2 and paths[0] == "add":
        check_write_dest(paths[1], ctx, base)


def procsub_scripts(args: list[Word], cmd: Cmd, mode: str, run: Run, depth: int) -> None:
    """`bash <(echo rm f)`, `source <(cat x.sh)`: the script is what the substituted command prints, when that is known."""
    if mode != "bash" or not cmd.subs or not any(w.text == "\0" for w in args):
        return
    for inner in cmd.subs:
        for sub in lex(inner, mode):
            text = stdin_for(Cmd(), sub, mode, run)
            if text == "\0DECODER":
                raise Block("delete", "process substitution (decoded input)")
            if text:
                analyze_text(text, "bash", Run(run.ctx, run.here, strict=False), depth + 1)


def run_shell(name: str, args: list[Word], cmd: Cmd, mode: str, run: Run, depth: int, stdin: str | None) -> None:
    code = None
    i = 0
    while i < len(args):
        a = args[i].text
        if a.startswith("-") and not a.startswith("--") and "c" in a[1:]:
            code = args[i + 1].text if i + 1 < len(args) else ""
            break
        if a == "--command":
            code = args[i + 1].text if i + 1 < len(args) else ""
            break
        if a.startswith("-") and a not in ("-", "--"):
            i += 1
            continue
        break
    if code is not None:
        if code.replace("\0", "").strip() == "" and cmd.subs and all(is_download(x, mode) for x in cmd.subs):
            return                                                  # `bash -c "$(curl url)"`: an installer fed by a download
        analyze_text(code, "bash", run, depth + 1)
        return
    if i < len(args) and not args[i].text.startswith("-"):
        script_file(args[i].text, cmd, run, depth)
        return
    if stdin is not None:
        if stdin == "\0DECODER":
            raise Block("delete", short(name + " (decoded input)"))
        analyze_text(stdin, "bash", run, depth + 1)


def ps_param(arg: str, *names: str) -> bool:
    if not arg.startswith("-") or len(arg) < 2:
        return False
    p = arg[1:].lower().rstrip(":")
    return any(n.startswith(p) for n in names)


def run_powershell(args: list[Word], cmd: Cmd, run: Run, depth: int, stdin: str | None) -> None:
    for i, w in enumerate(args):
        a = w.text
        if ps_param(a, "encodedcommand") or a.lower() in ("-e", "-ec", "-enc"):
            raise Block("delete", "powershell -EncodedCommand")
        if ps_param(a, "command"):
            code = " ".join(x.text for x in args[i + 1:])
            if code.strip() == "-" and stdin is not None:
                code = stdin
            analyze_text(code, "ps", run, depth + 1)
            return
        if ps_param(a, "file") and i + 1 < len(args):
            script_file(args[i + 1].text, cmd, run, depth)
            return
    if stdin is not None:
        if stdin == "\0DECODER":
            raise Block("delete", "powershell (decoded input)")
        analyze_text(stdin, "ps", run, depth + 1)


def run_cmd_exe(args: list[Word], run: Run, depth: int) -> None:
    texts = [w.text for w in args]
    for i, x in enumerate(texts):
        if x.lower() in ("/c", "/k", "/r"):
            analyze_text(" ".join(texts[i + 1:]), "cmd", run, depth + 1)
            return
        m = re.match(r"(?i)^/[ck](.+)$", x)
        if m:
            analyze_text(m.group(1) + " " + " ".join(texts[i + 1:]), "cmd", run, depth + 1)
            return


def start_process(args: list[Word], mode: str, run: Run, depth: int) -> None:
    program, extra = None, []
    i = 0
    while i < len(args):
        w = args[i]
        if ps_param(w.text, "filepath"):
            program = args[i + 1] if i + 1 < len(args) else None
            i += 2
        elif ps_param(w.text, "argumentlist"):
            if i + 1 < len(args):
                for part in lex(args[i + 1].text, "cmd"):
                    extra += part.words
            i += 2
        elif w.text.startswith("-"):
            i += 1
        else:
            if program is None:
                program = w
            else:
                extra.append(w)
            i += 1
    if program is not None:
        sub = Cmd()
        sub.words = [program, *extra]
        analyze_cmd_words(sub, None, mode, run, depth, None)


def script_file(path: str, cmd: Cmd, run: Run, depth: int) -> None:
    text = read_script(path, run)
    if text is None:
        return
    kind = SCRIPT_SUFFIX.get(os.path.splitext(path)[1].lower())
    if kind is None:
        head = text[:120].splitlines()[0] if text else ""
        if head.startswith("#!"):
            kind = "py" if "python" in head else "node" if "node" in head else "ruby" if "ruby" in head else \
                "perl" if "perl" in head else "ps" if "pwsh" in head or "powershell" in head else "sh"
        else:
            return
    scan_code(kind, text, run, depth + 1)


def scan_code(kind: str, code: str, run: Run, depth: int) -> None:
    """Check the text of a script or of `-c` code the way its language is written."""
    ctx = run.ctx
    if kind in ("sh", "ps", "cmd"):
        analyze_text(code, {"sh": "bash", "ps": "ps", "cmd": "cmd"}[kind], Run(ctx, run.here, strict=False), depth)
        return
    check_code_markers(code, ctx)
    if kind == "py":
        if PY_DELETE.search(code):
            raise Block("delete", short(PY_DELETE.search(code).group(0)))
    elif kind == "node":
        if NODE_DELETE.search(code) or (NODE_FS.search(code) and NODE_BARE.search(code)):
            m = NODE_DELETE.search(code) or NODE_BARE.search(code)
            raise Block("delete", short(m.group(0)))
    elif kind == "ruby" and RUBY_DELETE.search(code):
        raise Block("delete", short(RUBY_DELETE.search(code).group(0)))
    elif kind == "perl" and PERL_DELETE.search(code):
        raise Block("delete", short(PERL_DELETE.search(code).group(0)))
    if EXEC_HINT.search(code):
        for m in STR_LITERAL.finditer(code):
            literal = m.group(1) if m.group(1) is not None else m.group(2)
            parts = literal.split()
            if parts and parts[0].lower() in SHELL_DELETE_WORD and (len(parts) > 1 or literal.strip() == parts[0]):
                raise Block("delete", short(literal))

def check_code_markers(code: str, ctx: Ctx) -> None:
    if CRED_CODE.search(code):
        raise Block("secrets", "keyring")
    low = norm_text(code)
    m = SECRET_RE.search(low)
    if m or data_secret_in_text(low, ctx):
        raise Block("secrets", short(m.group(0) if m else "data folder"))
    if names_protected(low, ctx):
        raise Block("hokma_internal", "hook files")
    if ctx.host_re and ctx.host_re.search(code) or ctx.url_re and ctx.url_re.search(code):
        raise Block("real_money", "broker host")


def run_interpreter(kind: str, args: list[Word], cmd: Cmd, run: Run, depth: int, stdin: str | None) -> None:
    code, script = None, None
    i = 0
    while i < len(args):
        a = args[i].text
        if kind == "py":
            if a == "-m":
                break
            if a in ("-W", "-X", "-Q"):
                i += 2
                continue
            if a == "-c" or (a.startswith("-") and not a.startswith("--") and len(a) > 2 and a[1:].isalpha()
                             and a.endswith("c")):
                code = args[i + 1].text if i + 1 < len(args) else ""
                break
            if a.startswith("-c") and len(a) > 2:
                code = a[2:]
                break
            if a.startswith("-") and a != "-":
                i += 1
                continue
        else:
            if a in ("-e", "--eval", "-p", "--print", "-E") or (kind in ("perl", "ruby") and a.startswith("-")
                                                              and "e" in a[1:] and not a.startswith("--")):
                code = args[i + 1].text if i + 1 < len(args) else ""
                break
            if a.startswith("-") and a != "-":
                i += 1
                continue
        script = a
        break
    if code is not None:
        scan_code(kind, code, run, depth + 1)
    elif script is not None and script != "-":
        text = read_script(script, run)
        if text is not None:
            scan_code(kind, text, run, depth + 1)
    elif stdin is not None:
        if stdin == "\0DECODER":
            raise Block("delete", "interpreter (decoded input)")
        scan_code(kind, stdin, run, depth + 1)


def write_rules(name: str, words: list[Word], args: list[Word], mode: str, run: Run, cmd: Cmd,
                prev: Cmd | None) -> None:
    """Destinations of the listed write commands must be inside the working folder or Hokma's data folder."""
    ctx = run.ctx
    positional = [w for w in args if not w.text.startswith("-") and not (mode == "cmd" and w.text.startswith("/"))]
    if name == "tee":
        for w in positional:
            check_write_dest(w.text, ctx, run.here)
    elif name in ("mkdir", "md", "touch", "mkfifo"):
        skip_next = {"mkdir": ("-m", "--mode"), "touch": ("-r", "-t", "-d", "--reference", "--date")}.get(name, ())
        skip = False
        for w in args:
            if skip:
                skip = False
                continue
            if w.text in skip_next:
                skip = True
                continue
            if w.text.startswith("-"):
                continue
            check_write_dest(w.text, ctx, run.here)
    elif name in WRITE_LAST:
        dest = None
        for i, w in enumerate(args):
            lw = w.text.lower()
            if lw in ("-t", "--target-directory") and i + 1 < len(args):
                dest = args[i + 1]
            elif lw.startswith("--target-directory="):
                dest = Word(w.text.split("=", 1)[1], False, False, True)
            elif ps_param(w.text, "destination") and i + 1 < len(args) and mode == "ps":
                dest = args[i + 1]
        if dest is None and len(positional) >= 2:
            dest = positional[-1]
        if dest is not None and not (":" in dest.text and not _WIN_ABS.match(dest.text) and name in ("rsync", "scp")):
            check_write_dest(dest.text, ctx, run.here)
    elif name in PS_PATH_WRITERS:
        target = None
        for i, w in enumerate(args):
            if ps_param(w.text, "path", "literalpath", "filepath") and i + 1 < len(args):
                target = args[i + 1]
                break
        if target is None and positional:
            target = positional[0]
        if target is not None:
            check_write_dest(target.text, ctx, run.here)


# ------------------------------------------------------------------------------------------------ the tools

FILE_TOOLS = guard.FILE_TOOLS
READ_TOOLS = {"Read", "Grep", "Glob"}
SHELL_TOOLS = {"Bash": "bash", "PowerShell": "ps"}
ORDER_TOOL = re.compile(r"(?:place|submit|send|create|modify|replace|cancel)[_-]?order(?!s?[_-]?book)|"
                        r"order[_-](?:create|send|cancel)", re.I)


def check_file_tool(tool: str, tool_input: dict, ctx: Ctx, cwd: str) -> None:
    """Edit, Write and NotebookEdit: the hook files are `guard`'s (the settings file may change while it keeps the
    registration of the hooks); the rest is judged here: secrets, and writing outside the allowed folders."""
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    shown = short(target) if isinstance(target, str) and target else "no path"
    if isinstance(target, str) and is_win_abs(target) and os.name != "nt":
        if under(clean_path(joined(target, cwd)), ctx.protect):        # a Windows path judged on another system
            raise Block("hokma_internal", shown)
    elif guard.decide({"tool_name": tool, "tool_input": tool_input, "cwd": cwd}, ctx.protect)[0]:
        raise Block("hokma_internal", shown)
    check_write_dest(target, ctx, cwd, guarded=False)


def check_read_tool(tool: str, tool_input: dict, ctx: Ctx, cwd: str) -> None:
    """Read, Grep and Glob. Hokma's data folder may be read (plugins, settings, logs) except its secrets; `Glob` only
    lists names, so it may also list a folder that holds secrets."""
    candidates = []
    if tool == "Read":
        candidates.append((tool_input.get("file_path"), True))
    elif tool == "Grep":
        candidates += [(tool_input.get("path"), True), (tool_input.get("glob"), False)]
    else:
        candidates += [(tool_input.get("path"), False), (tool_input.get("pattern"), False)]
    if tool in ("Grep", "Glob") and not tool_input.get("path"):
        check_read_path(cwd, ctx, cwd, content=tool == "Grep")      # the implicit search root is the folder it runs in
    if tool == "Grep":                                              # a search over a folder that holds the data folder
        root = expand(tool_input.get("path") or cwd, ctx)
        if isinstance(root, str) and not unresolved(root) and not (is_win_abs(root) and os.name != "nt") and _contains_data(
                clean_path(joined(root, cwd)), ctx):
            raise Block("secrets", short(root))
    for item, content in candidates:
        if isinstance(item, str) and item:
            check_text_markers(item, ctx, reading=True)
            check_real_path(item, ctx, reading=True)
            if tool != "Grep" or content:
                check_read_path(item, ctx, cwd, content=content)


# Commands that only read. A command line is read-only only when EVERY command in it is one of these; such a line may
# name the hook files (reading them is free) and is judged strictly where it touches Hokma's data folder.
LIST_COMMANDS = {"ls", "dir", "stat", "du", "pwd", "get-childitem", "gci", "get-item", "gi", "test-path",
                 "get-location", "realpath", "readlink", "basename", "dirname", "which", "where", "find", "echo", "printf",
                 "true", "false", "write-output", "write-host"}
CONTENT_READERS = {"cat", "head", "tail", "grep", "egrep", "fgrep", "rg", "type", "get-content", "gc", "select-string",
                   "sls", "strings", "cmp", "md5sum", "sha1sum", "sha256sum", "wc", "cut", "nl", "tac",
                   "get-filehash", "diff"}
LISTERS = {"ls", "dir", "get-childitem", "gci", "get-item", "gi", "find"}
SEARCHERS = {"grep", "egrep", "fgrep", "rg", "select-string", "sls"}
BAD_READ_FLAGS = ("--pre",)
READ_COMMANDS = LIST_COMMANDS | CONTENT_READERS
FIND_WRITERS = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}


def only_reads(command: str, mode: str, depth: int = 0) -> bool:
    """True when every command in the line only reads: known read commands, no `find -exec/-delete`, no output
    redirection except to a device, no here-document, no command substitution that is not itself read-only."""
    if depth > 3:
        return False
    try:
        cmds = lex(command, mode)
    except Exception:
        return False
    cds = 0
    tainted = False                                             # an upstream command of this pipeline lists or emits files
    for cmd in cmds:
        if cmd.heredocs or cmd.herestrs:
            return False
        first = base_name(significant(cmd.words, mode)[0].text) if significant(cmd.words, mode) else ""
        tainted = tainted and cmd.pipe_in
        if mode == "ps" and tainted and first in CONTENT_READERS:
            return False                                        # `Get-ChildItem | Get-Content`: reads what it lists
        tainted = tainted or first in LISTERS or first in ("echo", "write-output", "write-host", "printf")
        for op, w in cmd.redirs:
            if (op == ">&" and (w.text.isdigit() or w.text == "-")) or (
                    op in (">", ">>", ">|") and is_device(w.text)):
                continue
            return False
        if not all(only_reads(inner, mode, depth + 1) for inner in cmd.subs):
            return False
        words = significant(cmd.words, mode)
        if mode == "ps" and any(any(x in w.text for x in ("(", ")", "::", "@", "+")) for w in words):
            return False                                        # PowerShell expressions build values the check cannot see
        if any(ASSIGNMENT.match(w.text) and not w.quoted for w in cmd.words[:len(cmd.words) - len(words)]):
            return False                                        # `x=...; cat $x/..`: the value could name a secret
        if not words:
            continue
        name = base_name(words[0].text)
        if any(ch in words[0].text for ch in "/\\") or words[0].text.startswith(("~", ".")):
            return False                                        # a script of the working folder named like a reader
        if name in CD_NAMES:
            cds += 1
            if cds > 1 or cmd.pipe_in or any(w.text in ("-", "$OLDPWD") for w in words[1:]):
                return False                                    # the folder the next words run in must be certain
            continue
        if name not in READ_COMMANDS:
            return False
        if any(w.text.lower().startswith(BAD_READ_FLAGS) for w in words[1:]):
            return False
        if name == "find" and any(w.text.lower() in FIND_WRITERS for w in words[1:]):
            return False
    return True


def lists_into_reader(command: str, mode: str) -> bool:
    """A PowerShell pipeline where a listing (or an echo) feeds a content reader: `Get-ChildItem x | Get-Content`."""
    if mode != "ps":
        return False
    try:
        cmds = lex(command, mode)
    except Exception:
        return False
    tainted = False
    for cmd in cmds:
        words = significant(cmd.words, mode)
        first = base_name(words[0].text) if words else ""
        tainted = tainted and cmd.pipe_in
        if tainted and first in CONTENT_READERS:
            return True
        tainted = tainted or first in LISTERS or first in ("echo", "write-output", "write-host", "printf")
    return False


def _option_values(text: str) -> list[str]:
    """The paths an option word may carry: `--file=x`, PowerShell `-Path:x`, a short option glued to its value (`-fx`)."""
    found = [text.split(sep, 1)[1] for sep in ("=", ":") if sep in text]
    if not text.startswith("--") and len(text) > 2:
        found.append(text[2:])
    return [x for x in found if x]


def check_command(tool: str, command: object, cwd: str, ctx: Ctx) -> None:
    if not isinstance(command, str):
        raise Block("delete", "command is not text")
    run = Run(ctx, cwd)
    run.source = command
    mode = SHELL_TOOLS[tool]
    raw = norm_text(command)                                   # backslashes may be eaten by the shell reading below
    for candidate in (raw, norm_text(expand(command, ctx))):   # the text as typed: a shell eats backslashes
        if RAW_DATA_SECRET_RE.search(candidate) or data_secret_in_text(candidate, ctx):
            raise Block("secrets", short(command))
    in_data = under_any(cwd, [ctx.data])
    hooks = names_protected(raw, ctx) or under_any(cwd, ctx.protect)
    if hooks or in_data or names_data_folder(raw, ctx):
        if lists_into_reader(command, mode):
            raise Block("secrets", short(command))             # `Get-ChildItem <folder> | Get-Content` reads what it lists
        if only_reads(command, mode):
            run.reading = True                                 # read-only: Hokma's folders may be read, not their secrets
        elif hooks:
            raise Block("hokma_internal", short(command))      # something other than a read, on the hook files
    analyze_text(command, mode, run, 0)


def evaluate(payload: dict, ctx: Ctx) -> Block | None:
    """The rule a tool call breaks, or None when it is allowed."""
    tool = payload.get("tool_name")
    if not isinstance(tool, str):
        raise ValueError("no tool name")
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        raise ValueError("tool_input is not an object")
    cwd = payload.get("cwd")
    cwd = cwd if isinstance(cwd, str) and os.path.isabs(cwd) else ctx.cwd
    ctx.cwd = cwd
    ctx._vars["PWD"] = cwd
    try:
        if tool.startswith("mcp__"):
            if ORDER_TOOL.search(tool):
                raise Block("real_money", tool)
        elif tool in FILE_TOOLS:
            check_file_tool(tool, tool_input, ctx, cwd)
        elif tool in READ_TOOLS:
            check_read_tool(tool, tool_input, ctx, cwd)
        elif tool in SHELL_TOOLS:
            check_command(tool, tool_input.get("command"), cwd, ctx)
    except Block as block:
        return block
    return None


# ------------------------------------------------------------------------------------------------ the hook

MESSAGES = {
    "delete": lambda x: (t("hardblock.delete", target=x), t("hardblock.hint.delete")),
    "hokma_internal": lambda x: (t("hardblock.hokma_internal", target=x), t("hardblock.hint.hokma_internal")),
    "outside_workdir": lambda x: (t("hardblock.outside_workdir", target=x), t("hardblock.hint.outside_workdir")),
    "secrets": lambda x: (t("hardblock.secrets", target=x), t("hardblock.hint.secrets")),
    "real_money": lambda x: (t("hardblock.real_money", target=x), t("hardblock.hint.real_money")),
}


def log_block(ctx: Ctx, tool: str, block: Block) -> None:
    """One JSON line in `<data>/guard/blocks.jsonl`. Never fails the block: a log problem is ignored."""
    try:
        folder = os.path.join(ctx.data, "guard")
        os.makedirs(folder, exist_ok=True)
        line = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "rule": block.rule, "tool": tool,
                "target": short(block.target), "conversation_id": ctx.conversation_id}
        if ctx.task_id:
            line["task_id"] = ctx.task_id
        data = (json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(os.path.join(folder, "blocks.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    except OSError:
        pass


def arguments(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == flag and i + 1 < len(args)]


def build_ctx(args: list[str], env: dict | None = None) -> Ctx:
    env = os.environ if env is None else env
    data = (arguments(args, "--data") or [""])[0]
    if not data:
        raise ValueError("no --data")
    roots = arguments(args, "--workdir")
    extra = env.get("HOKMA_WORK_ROOT", "")
    if extra:
        roots.append(extra)
    if not roots:
        raise ValueError("no --workdir")
    protect = arguments(args, "--protect")
    if not protect:
        raise ValueError("no --protect")
    return Ctx(data, roots, protect, conversation_id=env.get("HOKMA_CONVERSATION_ID", ""),
               task_id=env.get("HOKMA_TASK_ID", ""))


def main(argv: list[str] | None = None) -> int:
    try:
        args = sys.argv[1:] if argv is None else argv
        ctx = build_ctx(args)
        payload = json.loads(sys.stdin.read())
        block = evaluate(payload, ctx)
        if block is None:
            return 0
        log_block(ctx, str(payload.get("tool_name", "")), block)
        print(f"HOKMA_BLOCKED {block.rule}", file=sys.stderr)
        reason, hint = MESSAGES[block.rule](short(block.target))
        print(reason, file=sys.stderr)
        print(hint, file=sys.stderr)
        return BLOCK
    except BaseException as exc:  # fail closed on anything unexpected
        print("HOKMA_BLOCKED error", file=sys.stderr)
        print(t("hardblock.error", error=type(exc).__name__), file=sys.stderr)
        return BLOCK


if __name__ == "__main__":
    raise SystemExit(main())

