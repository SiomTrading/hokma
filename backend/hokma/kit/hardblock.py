"""PreToolUse hook: the protections that are always on, in both permission modes.

Seven rules, nothing else is refused (everything not listed here is the owner's to do):

    delete           deleting files or folders, including the obfuscated forms
    truncate         emptying a file that has content
    hokma_internal   Hokma's own runtime, configuration, trash and `.hokma/`; settings files of the working folder
    force_push       rewriting or deleting remote Git history
    outside_workdir  writing outside the working folder; formatting or touching disks
    secrets          the keys stored for Connections, credentials, SSH, cloud and Git credentials
    real_money       order tools of brokers and the hosts that place real orders

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
`iex`, encoded commands and a decoder piped into a shell are refused outright. A script of the working folder
that a command runs is read and its text is checked with the same rules.

Git arguments (task 3045, closed): the global options before the subcommand are a white list (`-C <path>` only when
the path stays inside the working folder, `--no-pager`, `--no-optional-locks`, `-c key=value` only with the safe keys,
`--version`); any other one is refused. `--template`, `--upload-pack`, `--receive-pack`, `--exec`, every abbreviation of
them and `clone -u` are refused in clone, init, fetch, pull, push, ls-remote and archive. A command line (or a script
of the working folder) that sets `GIT_CONFIG*`, `GIT_DIR`, `GIT_EXEC_PATH` or `GIT_TEMPLATE_DIR` is refused; reading
them is free.

Scope of this hook: it is defense in depth, not a sandbox. In no-prompt mode the agent can run arbitrary code, so the
integrity of the harness rests on the structural protections, not on parsing every command: (1) the restore points are
Git refs in the project (`refs/hokma/checkpoints/*`) and only those whose commit SHA matches the one recorded in
Hokma's own database are trusted, so a forged, moved or packed ref is ignored, and this hook refuses writes to
`.git/refs/hokma` and `packed-refs`; (2) the owner-only endpoints need the session token plus the owner confirmation
registered once with a one-time secret; (3) the sha256 integrity manifest of the installed app, which `hokma doctor`
verifies. A further parser finding is classified B unless it breaks one of those three protections.

Standard library plus `hokma.i18n` and `hokma.kit.guard` only.
"""
import json
import ntpath
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import unquote

from hokma.i18n import t
from hokma.kit.guard import under_any

BLOCK = 2
RULES = ("delete", "truncate", "hokma_internal", "force_push", "outside_workdir", "secrets", "real_money")
MAX_DEPTH = 6
MAX_SCRIPT_BYTES = 512 * 1024
MAX_EDIT_BYTES = 8 * 1024 * 1024
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
        self.roots = [r for r in roots if r]
        self.protect = [p for p in protect if p] or [data]
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


_SLASHES = re.compile(r"/{2,}")
SHUTDOWN_ROUTE_RE = re.compile(r"/api/system/shutdown")
# The backstop: the three words in order, whatever sits between them (spaces, control characters, a stray segment).
SHUTDOWN_WORDS_RE = re.compile(r"api.{0,64}?system.{0,64}?shutdown", re.S)
_WHITESPACE = re.compile(r"[\s\x00-\x1f\x7f]+")


def _decoded(s: str) -> str:
    """Percent-decoded until it stops changing, backslashes as slashes, lowercase."""
    for _ in range(8):
        decoded = unquote(s)
        if decoded == s:
            break
        s = decoded
    return s.replace("\\", "/").lower()


def remove_dot_segments(s: str) -> str:
    """RFC 3986 remove_dot_segments over the whole text, split on `/`: `.` is dropped and `..` drops the segment before
    it, whatever characters that segment holds (spaces and control characters included). Empty segments are dropped."""
    out: list[str] = []
    for segment in s.split("/"):
        if segment == ".":
            continue
        if segment == "..":
            if out:
                out.pop()
            continue
        if segment == "":
            continue
        out.append(segment)
    return "/" + "/".join(out) if s.startswith("/") else "/".join(out)


def norm_route(s: str) -> str:
    """Text as a server would read a path in it (`/api/system/%73hutdown`, `/api/system/./shutdown`,
    `/api//system/shutdown`, `/api/x y/../system/shutdown`)."""
    return remove_dot_segments(_SLASHES.sub("/", _decoded(s)))


def names_shutdown_route(text: str) -> bool:
    if SHUTDOWN_ROUTE_RE.search(norm_route(text)):
        return True
    return bool(SHUTDOWN_WORDS_RE.search(_WHITESPACE.sub("", _decoded(text))))


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
SECRET_RE = re.compile("|".join([
    _B + r"\.ssh" + _E, _B + r"\.aws" + _E, _B + r"\.config/gh" + _E, _B + r"\.git-credentials(?:$|[\s\"'])",
    _B + r"\.gnupg" + _E, _B + r"_?\.?netrc(?:$|[\s\"'])", r"github cli/hosts\.yml", r"hokma-market",
    # The keyring service that holds the remembered session (read or write), same as the Connections one.
    r"hokma-session",
    r"hokma/database\.json", r"hokma/claude-config/(?:\.credentials\.json|credentials\.json|\.claude\.json)",
    r"claude-config/(?:\.credentials\.json|\.claude\.json)",
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
INTERNAL_RE = re.compile("|".join([
    _B + r"\.hokma" + _E,
    # Hokma's own API: the session header, the owner confirmation and the calls that change the permission mode or
    # touch the Trash and the restore points. Those belong to the owner at the screen; an agent that names them is
    # trying to call them.
    r"x-hokma-token", r"x-hokma-owner", r"/api/profile/permission-mode", r"/api/session/owner-nonce",
    r"/api/system/shutdown",
    r"/api/trash/(?:empty|\d+/(?:restore|purge))", r"/api/checkpoints/[^/\s\"']+/restore-",
    r"(?:localappdata%?|appdata/local|application support|\.local/share|xdg_data_home\}?)/hokma" + r"(?:/|\s|$|[\"'])",
    # The remembered session belongs to the owner: sign-in resume and sign-out calls, and the control channel and
    # lock file of the background engine (a Unix socket, a Windows pipe and the lock with pid and port).
    r"/api/session/(?:resume|logout|challenge|enter|handoff-code)", r"control\.sock", r"hokma-control-", r"engine\.lock",
]))
# The restore point refs live in the project's Git; an agent that writes them (or packs them) could forge a point.
GIT_REFS_RE = re.compile(_B + r"\.git/(?:refs/hokma|packed-refs)" + r"(?:/|\s|$|[\"'])")
RAW_DATA_RE = re.compile(r"(?:localappdata%?|appdata/local|application support|\.local/share|xdg_data_home\}?)/hokma"
                         r"(?:/|\s|$|[\"'])")
SETTINGS_RE = re.compile(r"(?:^|[/\s\"'])\.claude/settings[^/\s\"']*\.json")
ENV_REDEFINE = re.compile(
    r"\b(?:CLAUDE_CONFIG_DIR|HOKMA_HOME|HOKMA_DATA_DIR)\b[\"'\]\)}]*\s*(?:\+?=|,)|\b(?:setx|unset)\s+(?:CLAUDE_CONFIG_DIR|"
    r"HOKMA_HOME|HOKMA_DATA_DIR)\b", re.I)
_GIT_ENV = (r"GIT_CONFIG_COUNT|GIT_CONFIG_KEY_\w+|GIT_CONFIG_VALUE_\w+|GIT_CONFIG_PARAMETERS|GIT_CONFIG_GLOBAL|"
            r"GIT_CONFIG_SYSTEM|GIT_CONFIG|GIT_DIR|GIT_EXEC_PATH|GIT_TEMPLATE_DIR")
# A command line that sets one of the variables that change what Git loads or runs: `VAR=x git ..`, `export VAR=`,
# `env VAR=..`, `set VAR=` (cmd), `$env:VAR=` (PowerShell), `declare -x`, `setx`, `Set-Item env:`, and the quoted name
# followed by a value (`os.environ['VAR'] = ..`, `SetEnvironmentVariable('VAR', ..)`). Reading or printing one is free.
GIT_ENV_RE = re.compile(
    r"(?<![\w$])(?:" + _GIT_ENV + r")\b[\"'\]\)}]*\s*\+?=|"
    r"[\"'](?:" + _GIT_ENV + r")[\"']\s*[,:]|"
    r"\bsetx\s+(?:/m\s+)?(?:" + _GIT_ENV + r")\b|"
    r"\b(?:set-item|si|new-item|ni)\b[^;&|\n]*\benv:(?:" + _GIT_ENV + r")\b", re.I)
CRED_CODE = re.compile(r"\bimport\s+keyring\b|\bfrom\s+keyring\b|\bkeyring\s*\.\s*(?:get|set|delete)|"
                       r"\bPasswordVault\b|\bCredRead\b|Get-StoredCredential|\bsecretstorage\b|"
                       r"(?:import_module|__import__)\s*\(\s*[\"']keyring[\"']|"          # loading it by name at run time
                       r"[\"']key[\"']\s*\+\s*[\"']ring[\"']|[\"']keyr[\"']\s*\+\s*[\"']ing[\"']", re.I)


def scan_secret_text(text: str) -> str | None:
    low = norm_text(text)
    m = SECRET_RE.search(low)
    return m.group(0).strip(" \"'=:;,()<>|&/@") if m else None


def check_text_markers(text: str, ctx: Ctx, *, settings: bool = True) -> None:
    """Secrets and Hokma-internal markers in a text that names a path (or in code)."""
    expanded = expand(text, ctx)
    for candidate in (text, expanded):
        hit = scan_secret_text(candidate)
        if hit:
            raise Block("secrets", short(text))
    low = norm_text(expanded) if expanded != text else norm_text(text)
    for candidate in (norm_text(text), low):
        if INTERNAL_RE.search(candidate) or GIT_REFS_RE.search(candidate) or (settings and SETTINGS_RE.search(candidate)):
            raise Block("hokma_internal", short(text))
    if names_shutdown_route(text) or names_shutdown_route(expanded):
        raise Block("hokma_internal", short(text))


def check_real_path(path: str, ctx: Ctx, *, settings: bool = True) -> None:
    """Resolve a path (relative to the call's folder) and refuse the secret and internal places by identity."""
    if unresolved(path) and not path.startswith("~"):
        return
    full = clean_path(joined(expand(path, ctx), ctx.cwd))
    if is_win_abs(full) and os.name != "nt":
        return                                                  # a Windows path on another system: text markers only
    for candidate in {full, os.path.realpath(full)}:
        low = norm_text(candidate)
        if SECRET_RE.search(low) or re.search(r"/database\.json$", low) and under_any(candidate, [ctx.data]):
            raise Block("secrets", short(path))
        if under_any(candidate, ctx.protect):
            raise Block("hokma_internal", short(path))
        if SETTINGS_RE.search(low) and settings:
            raise Block("hokma_internal", short(path))
        if GIT_REFS_RE.search(low + "/"):                       # the file itself, wherever a link or `..` led to it
            raise Block("hokma_internal", short(path))


def check_path_arg(text: str, ctx: Ctx, *, settings: bool = True) -> None:
    check_text_markers(text, ctx, settings=settings)
    check_real_path(text, ctx, settings=settings)


def is_device(p: str) -> bool:
    low = p.lower()
    return low in DEVICES or low.startswith(("/dev/fd/", "/proc/self/fd/"))


def check_write_dest(text: str, ctx: Ctx, cwd: str) -> str | None:
    """A path a command or a tool writes to. Returns the resolved path (None when it cannot be known)."""
    if not text or text == "-":
        return None
    if is_device(text):
        return None
    check_path_arg(text, ctx)
    if unresolved(expand(text, ctx)):
        return None                                             # built at run time: cannot be judged here
    full = clean_path(joined(expand(text, ctx), cwd))
    if is_device(full):
        return None
    if not is_win_abs(full):
        check_real_path(full, ctx)                              # relative to a folder the command moved into with `cd`
    real = full if is_win_abs(full) and os.name != "nt" else os.path.realpath(full)
    if not under(real, ctx.roots):
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
WRITE_FREE = {"mkdir", "md", "touch", "mkfifo", "tee"}
WRITE_LAST = {"cp", "mv", "ln", "install", "rsync", "scp", "copy", "xcopy", "move", "robocopy", "copy-item", "cpi",
              "move-item", "mi", "rename-item", "rni", "ren", "rename"}
PS_PATH_WRITERS = {"new-item", "ni", "set-content", "sc", "add-content", "ac", "out-file", "export-csv",
                   "export-clixml", "tee-object"}
PS_VALUE_PARAMS = ("path", "literalpath", "filepath", "destination", "value", "encoding", "name", "itemtype", "filter",
                   "include", "exclude", "stream", "width", "inputobject", "variable", "delimiter", "pv")
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
    ctx = run.ctx
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
    producer = empty_producer(words, cmd, mode)
    for op, w in cmd.redirs:
        if op in ("<", "<<", "<<<", "<&"):
            check_path_arg(w.text, ctx, settings=True)
            continue
        if op == ">&":
            continue
        full = check_write_dest(w.text, ctx, run.here)
        if op in (">", ">|") and producer and full and os.path.isfile(full) and os.path.getsize(full) > 0:
            raise Block("truncate", short(w.text))


def empty_producer(words: list[Word], cmd: Cmd, mode: str) -> bool:
    """True when the command writes nothing, so `> file` after it empties the file."""
    if not words:
        return not cmd.heredocs and not cmd.herestrs
    name = base_name(words[0].text)
    args = [w for w in words[1:]]
    if name in (":", "true", "false"):
        return True
    if name == "cat":
        devnull = any(w.text == "/dev/null" for w in args) or any(op == "<" and w.text == "/dev/null"
                                                                      for op, w in cmd.redirs)
        return devnull and all(w.text.startswith("-") or w.text == "/dev/null" for w in args)
    if name == "echo":
        flags = [w for w in args if re.fullmatch(r"-[neE]+", w.text)]
        rest = [w for w in args if w not in flags]
        return any("n" in f.text for f in flags) and all(w.text == "" for w in rest)
    if name == "printf":
        return bool(args) and all(w.text == "" for w in args)
    if name in ("write-output", "write-host") and mode == "ps":
        return all(w.text == "" for w in args)
    return False


LAUNCHER_VERBS = frozenset(("start", "stop", "restart", "open"))
LAUNCHER_RUNNERS = frozenset(("uv", "uvx", "pipx", "poetry", "pipenv", "pdm", "hatch", "rye", "conda", "npx", "env",
                              "start-process", "saps", "start", "invoke-expression", "iex", "call", "cmd", "wsl"))
_LAUNCHER_BASES = frozenset(("hokma", "hokma-script", "hokma_cli", "hokma_cli.main"))
_LAUNCHER_EXT = re.compile(r"\.(?:exe|cmd|bat|ps1|sh|py|pyw)$")
LAUNCHER_CODE = re.compile(
    r"[\"']hokma(?:\.exe)?[\"']\s*,\s*[\"'](?:start|stop|restart|open)[\"']|"
    r"[\"'][^\"'\n]*\bhokma(?:\.exe)?\s+(?:start|stop|restart|open)\b|"
    r"hokma_cli(?:\.main)?[\"']?\s*,\s*[\"'](?:start|stop|restart|open)[\"']", re.I)


def launcher_token(text: str) -> bool:
    """The token names the `hokma` launcher: a bare name, any path to it, or the module that implements it."""
    base = re.split(r"[\\/]", text.strip("\"'` ").lower())[-1]
    base = _LAUNCHER_EXT.sub("", base) if base != "hokma_cli.main" else base
    if base == "main" and "hokma_cli" in text.lower():
        return True                                             # .../hokma_cli/main.py
    return base in _LAUNCHER_BASES


def launcher_call(name: str, words: list[Word], cmd: Cmd) -> str | None:
    """The command line when it runs the `hokma` launcher with start, stop, restart or open (any way of reaching it)."""
    if name in ("echo", "printf", "write-output", "write-host", "write-error", "write-warning", "git"):
        return None
    texts = [w.text for w in words]
    for i, text in enumerate(texts):
        if not launcher_token(text):
            continue
        prev = texts[i - 1].lower() if i else ""
        reached = (i == 0 or name in LAUNCHER_RUNNERS or prev == "-m" or "/" in text or "\\" in text
                   or prev in ("run", "exec", "tool", "--"))
        if not reached:
            continue
        rest = [x.strip("\"'` ").lower() for part in texts[i + 1:] for x in part.split(",")]
        if any(x in LAUNCHER_VERBS for x in rest):
            return short(" ".join(texts))
        if not rest and cmd.pipe_in:
            return short(" ".join(texts))                       # `echo stop | xargs hokma`: the verb arrives on stdin
    for i, text in enumerate(texts):                            # `python -mhokma_cli.main stop`
        if text.lower().startswith("-mhokma_cli") and any(x.strip("\"'` ").lower() in LAUNCHER_VERBS
                                                         for x in texts[i + 1:]):
            return short(" ".join(texts))
    return None


def scan_words(name: str, words: list[Word], cmd: Cmd, run: Run) -> None:
    """Markers of secrets and Hokma internals anywhere in the arguments, plus the host list of brokers."""
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
        check_text_markers(text, ctx)
        for w in scan:
            if any(ch in w.text for ch in "/\\") or w.text.startswith(("~", ".", "$", "%")):
                check_real_path(w.text, ctx)
        if ENV_REDEFINE.search(text) or GIT_ENV_RE.search(text):
            raise Block("hokma_internal", short(text))
    full = " ".join(w.text for w in words)
    shown = launcher_call(name, words, cmd)
    if shown:
        raise Block("hokma_internal", shown)
    if ctx.host_re and ctx.host_re.search(full) or ctx.url_re and ctx.url_re.search(full):
        raise Block("real_money", short(full))


def dispatch(name: str, words: list[Word], args: list[Word], cmd: Cmd, prev: Cmd | None, mode: str, run: Run,
             depth: int, stdin: str | None) -> None:
    ctx = run.ctx
    texts = [w.text for w in args]
    shown = short(" ".join(w.text for w in words))
    low = [x.lower() for x in texts]

    if name in DYNAMIC_EXEC:
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

    harness_rules(name, args, mode, shown)

    if name == "find":
        find_rules(args, mode, run, depth)
        return

    if name == "git":
        git_rules(args, run)
        return

    if name in CD_NAMES:
        change_dir(args, run)
        return

    if name in ("truncate",):
        for i, a in enumerate(low):
            if a in ("-s", "--size") and i + 1 < len(low) and zero_size(low[i + 1]):
                raise Block("truncate", shown)
            if (a.startswith("--size=") and zero_size(a[7:])) or (a.startswith("-s") and len(a) > 2 and zero_size(a[2:])):
                raise Block("truncate", shown)
        return
    if name in ("clear-content", "clc"):
        raise Block("truncate", shown)

    if name == "cp" and any(x == "/dev/null" for x in texts):
        dest = [w for w in args if not w.text.startswith("-") and w.text != "/dev/null"]
        for w in dest:
            full = check_write_dest(w.text, ctx, run.here)
            if full and os.path.isfile(full) and os.path.getsize(full) > 0:
                raise Block("truncate", short(w.text))

    if name == "tee" and not any(x in ("-a", "--append") for x in low):
        empty_in = any(op == "<" and w.text == "/dev/null" for op, w in cmd.redirs) or (
            prev is not None and empty_producer(significant(prev.words, mode), prev, mode))
        if empty_in:
            for w in args:
                if not w.text.startswith("-"):
                    full = check_write_dest(w.text, ctx, run.here)
                    if full and os.path.isfile(full) and os.path.getsize(full) > 0:
                        raise Block("truncate", short(w.text))

    if name == "dd":
        if any(w.text.lower() == "if=/dev/null" for w in args):
            for w in args:
                if w.text.lower().startswith("of="):
                    full = check_write_dest(w.text[3:], ctx, run.here)
                    if full and os.path.isfile(full) and os.path.getsize(full) > 0:
                        raise Block("truncate", short(w.text))
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


def zero_size(v: str) -> bool:
    return bool(re.fullmatch(r"0+[kmgtb]*", v.lower()))


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


# ---- the harness itself (bug 1036): `.git`, `.claude`, `.hokma` must not be renamed, moved or reached by a link ----

HARNESS_NAMES = (".git", ".claude", ".hokma")
MOVE_NAMES = {"mv", "move", "move-item", "mi", "ren", "rename", "rename-item", "rni"}
LINK_NAMES = {"ln", "mklink", "new-item", "ni", "junction"}
LINK_KINDS = {"symboliclink", "junction", "hardlink"}


def _parts(text: str) -> list[str]:
    return [x for x in text.replace("\\", "/").lower().split("/") if x not in ("", ".")]


def harness_move_target(text: str) -> bool:
    """The folder itself (`.git`, `.claude/`, `a/.hokma`) or anything inside `.git` and `.hokma`."""
    parts = _parts(text)
    if not parts:
        return False
    return parts[-1] in HARNESS_NAMES or any(x in (".git", ".hokma") for x in parts[:-1])


def harness_link_target(text: str) -> bool:
    """A link may not point at `.git` or `.hokma` (or into them) nor at `.claude` or its settings files."""
    parts = _parts(text)
    return any(x in (".git", ".hokma") for x in parts) or bool(parts and parts[-1] == ".claude") or bool(
        SETTINGS_RE.search(norm_text(text)))


def harness_rules(name: str, args: list[Word], mode: str, shown: str) -> None:
    words = [w.text for w in args if w.text and not w.text.startswith("-")
             and not (mode == "cmd" and w.text.startswith("/"))]
    if name in MOVE_NAMES and any(harness_move_target(x) for x in words):
        raise Block("hokma_internal", shown)
    if name in LINK_NAMES:
        kinds = {w.text.lower() for w in args}
        if name in ("new-item", "ni") and not kinds & LINK_KINDS:
            return
        if any(harness_link_target(x) for x in words):
            raise Block("hokma_internal", shown)


SAFE_CONFIG_KEYS = {"user.name", "user.email", "core.autocrlf", "init.defaultbranch"}
CONFIG_READ_FLAGS = {"--get", "--get-all", "--get-regexp", "--list", "-l"}
CONFIG_SHOW_FLAGS = {"--show-origin", "--show-scope"}
CONFIG_READ_EXTRAS = {"--name-only", "--null", "-z"}
CONFIG_SCOPES = {"--local", "--global"}
CONFIG_SUBCOMMANDS = {"get", "list", "set", "unset"}
CONFIG_KNOWN = CONFIG_READ_FLAGS | CONFIG_SHOW_FLAGS | CONFIG_READ_EXTRAS | CONFIG_SCOPES | {"--add", "--unset"}


def safe_config_key(key: str) -> bool:
    """A key a command may write: one of the short list (Git ignores the case of a key), nothing around it."""
    return key.lower() in SAFE_CONFIG_KEYS


def config_allowed(args: list[Word]) -> bool:
    """`git config` (the words after `config`) by white list. True only for one of the known forms of reading, or
    for the write or unset of a key of SAFE_CONFIG_KEYS. Everything else is refused: an option that is not
    recognised exactly (abbreviations and short bundles included), a key outside the list, a word the shell expands."""
    if any(w.dyn or w.glob for w in args):
        return False
    options: list[str] = []
    positional: list[str] = []
    for w in args:
        if w.text.startswith("-") and len(w.text) > 1:
            if w.text not in CONFIG_KNOWN:
                return False
            options.append(w.text)
        else:
            positional.append(w.text)
    flags = set(options)
    if len(flags) != len(options):
        return False
    sub = positional[0] if positional and positional[0] in CONFIG_SUBCOMMANDS else None
    rest = positional[1:] if sub else positional
    reads = flags & CONFIG_READ_FLAGS
    if reads or sub in ("get", "list"):
        if len(reads) + (1 if sub else 0) != 1 or flags & {"--add", "--unset"} or sub in ("set", "unset"):
            return False
        if sub == "list" or reads <= {"--list", "-l"} and reads:
            return not rest
        return 1 <= len(rest) <= 2
    if flags & (CONFIG_SHOW_FLAGS | CONFIG_READ_EXTRAS):
        return False                                   # these only go with an explicit read
    if sub == "unset" or "--unset" in flags:
        return not (sub and "--unset" in flags) and flags <= CONFIG_SCOPES | {"--unset"} and \
            len(rest) == 1 and safe_config_key(rest[0])
    if sub == "set":
        return flags <= CONFIG_SCOPES and len(rest) == 2 and safe_config_key(rest[0])
    if len(rest) == 2:
        return flags <= CONFIG_SCOPES | {"--add"} and safe_config_key(rest[0])
    if len(rest) == 1:
        return flags <= CONFIG_SCOPES                  # a key with no value reads it
    return not rest and not flags                      # `git config` alone only prints its usage


def config_key_ok(text: str) -> bool:
    """The `name=value` of `git -c` and `--config-env`: the name has to be one of the safe keys."""
    return safe_config_key(text.split("=", 1)[0])


def clone_config_ok(rest: list[Word]) -> bool:
    """`git clone -c key=value` and `--config key=value` write the config of the new clone, hooks included."""
    i = 0
    while i < len(rest):
        x = rest[i].text
        if x == "--":
            break
        if x in ("-c", "--config"):
            if i + 1 >= len(rest) or not config_key_ok(rest[i + 1].text):
                return False
            i += 2
            continue
        if x.startswith("--") and len(x.split("=", 1)[0]) >= 3 and "--config".startswith(x.split("=", 1)[0]):
            if "=" not in x or not config_key_ok(x.split("=", 1)[1]):     # `--conf x`, `--config=x`
                return False
        elif x.startswith("-c") and len(x) > 2 and x[1] != "-":
            if not config_key_ok(x[2:]):
                return False
        elif x[:1] == "-" and x[1:2] != "-" and "c" in x[1:]:
            return False                                # a short bundle with c: its key cannot be proven
        i += 1
    return True

GIT_GLOBAL_FREE = {"--no-pager", "--no-optional-locks"}
PROGRAM_OPTION_SUBS = {"clone", "init", "fetch", "pull", "push", "ls-remote", "archive"}
PROGRAM_OPTIONS = ("template", "upload-pack", "receive-pack", "exec")      # they name a program or a folder Git runs from


def program_option(sub: str, rest: list[str]) -> bool:
    """An option that makes Git run a program or load hooks from a folder: `--template`, `--upload-pack`,
    `--receive-pack`, `--exec`, any abbreviation of them (Git accepts any unambiguous prefix, so every prefix is
    refused), the `--option=value` form, and `clone -u` (and `-u<value>`, short bundles with u). `push -u` is
    `--set-upstream` and stays free."""
    for x in rest:
        if x == "--":
            return False
        if x.startswith("--"):
            name = x[2:].split("=", 1)[0].lower()
            if name and any(p.startswith(name) for p in PROGRAM_OPTIONS):
                return True
        elif sub == "clone" and x.startswith("-") and len(x) > 1 and "u" in x[1:]:
            return True
    return False


def git_rules(args: list[Word], run: Run) -> None:
    a = [w.text for w in args]
    i = 0
    base = run.here                                  # `-C` moves the folder the rest of the line is judged from
    while i < len(a):
        x = a[i]
        w = args[i]
        if w.dyn or w.glob:
            raise Block("hokma_internal", short("git " + " ".join(a)))
        if x in GIT_GLOBAL_FREE:
            i += 1
        elif x == "-C":
            if i + 1 >= len(a) or args[i + 1].dyn or args[i + 1].glob or unresolved(expand(a[i + 1], run.ctx)):
                raise Block("hokma_internal", short("git " + " ".join(a)))
            target = clean_path(joined(expand(a[i + 1], run.ctx), base))
            if not under(target if is_win_abs(target) and os.name != "nt" else os.path.realpath(target), run.ctx.roots):
                raise Block("hokma_internal", short("git " + " ".join(a)))
            base = target
            i += 2
        elif x in ("-c", "--config-env"):
            if i + 1 >= len(a) or args[i + 1].dyn or args[i + 1].glob or not config_key_ok(a[i + 1]):
                raise Block("hokma_internal", short("git " + " ".join(a)))
            i += 2
        elif x.startswith("--config-env=") or (x.startswith("-c") and len(x) > 2 and x[1] == "c"):
            attached = x[len("--config-env="):] if x[1] == "-" else x[2:]          # `-ckey=value`, `--config-env=key=VAR`
            if not config_key_ok(attached):
                raise Block("hokma_internal", short("git " + " ".join(a)))
            i += 1
        elif x == "--version" and i == len(a) - 1:
            return
        elif x.startswith("-"):
            raise Block("hokma_internal", short("git " + " ".join(a)))     # every other global option is refused
        else:
            break
    if i >= len(a):
        return
    sub, rest = a[i].lower(), a[i + 1:]
    rl = [x.lower() for x in rest]
    shown = short("git " + " ".join(a))
    if args[i].dyn or args[i].glob:
        raise Block("hokma_internal", shown)         # the subcommand is not known: it could be `config`

    def letters(prefix_ok: str) -> bool:
        return any(len(x) > 1 and x[0] == "-" and x[1] != "-" and any(c in prefix_ok for c in x[1:]) for x in rest)

    if sub in PROGRAM_OPTION_SUBS and program_option(sub, rest):
        raise Block("hokma_internal", shown)
    if sub == "push":
        forced = any(x in ("--force", "--delete", "--mirror", "--prune", "--force-if-includes") or
                     x.startswith(("--force=", "--force-with-lease")) for x in rl) or letters("fd")
        refspec = any(len(x) > 1 and x[0] in "+:" for x in rest)
        if forced or refspec:
            raise Block("force_push", shown)
    elif sub in ("filter-branch", "filter-repo"):
        raise Block("force_push", shown)
    elif sub == "reflog" and any(x in ("expire", "delete") for x in rl):
        raise Block("force_push", shown)
    elif sub == "gc" and any(x == "--prune" or (x.startswith("--prune=") and x != "--prune=never") for x in rl):
        raise Block("force_push", shown)
    elif sub == "prune" and any(re.fullmatch(r"(?:--expire=)?now", x) for x in rl):
        raise Block("force_push", shown)
    elif sub == "credential":
        raise Block("secrets", shown)
    elif sub == "update-ref" and any("refs/hokma" in x for x in rl):
        raise Block("hokma_internal", shown)
    elif sub == "update-ref" and any(x in ("-d", "--delete", "--stdin") or (x[:1] == "-" and x[1:2] != "-" and "d" in x[1:])
                                     for x in rl):
        raise Block("force_push", shown)             # deleting a ref (branch, tag, point) loses history
    elif sub == "replace" and not any(x in ("-l", "--list", "--format") or x.startswith("--format=") for x in rl):
        raise Block("force_push", shown)             # a replace ref hides real commits behind others
    elif sub == "worktree" and rl[:1] and rl[0] in ("remove", "prune"):
        raise Block("delete", shown)                 # remove takes a working folder off the disk
    elif sub == "tag" and any(x in ("-d", "--delete") or (x[:1] == "-" and x[1:2] != "-" and "d" in x[1:]) for x in rest) \
            and any("hokma" in x for x in rl):
        raise Block("hokma_internal", shown)
    elif sub == "mv" and any(harness_move_target(x) for x in rest if not x.startswith("-")):
        raise Block("hokma_internal", shown)
    elif sub == "config" and not config_allowed(args[i + 1:]):
        raise Block("hokma_internal", shown)
    elif sub == "clone" and not clone_config_ok(args[i + 1:]):
        raise Block("hokma_internal", shown)
    elif sub == "rm" and "--cached" not in rl:
        raise Block("delete", shown)
    elif sub == "clean" and not any(x in ("-n", "--dry-run") or (x.startswith("-") and not x.startswith("--") and "n" in x)
                                    for x in rl):
        raise Block("delete", shown)
    elif sub == "reset" and "--hard" in rl:
        raise Block("delete", shown)
    elif sub == "checkout" and (("--" in rest and rest.index("--") < len(rest) - 1) or (rest and rest[-1] == ".")):
        raise Block("delete", shown)
    elif sub == "restore":
        staged_only = ("--staged" in rl or "-S" in rest) and not ("--worktree" in rl or "-W" in rest)
        if not staged_only:
            raise Block("delete", shown)
    elif sub == "stash" and rl[:1] and rl[0] in ("drop", "clear"):
        raise Block("delete", shown)
    elif sub == "stash" and any(x in ("--include-untracked", "--all") or (x[:1] == "-" and x[1:2] != "-"
                                                                          and any(c in x[1:] for c in "ua"))
                                for x in rl):
        raise Block("delete", shown)                 # it takes the untracked files out of the working folder
    elif sub == "branch" and any(x == "-D" or (x[:1] == "-" and x[1:2] != "-" and "D" in x[1:]) for x in rest):
        raise Block("delete", shown)


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
        if GIT_ENV_RE.search(code):                              # `VAR=x git ..` lines drop out of the word scan
            raise Block("hokma_internal", short(GIT_ENV_RE.search(code).group(0)))
        analyze_text(code, {"sh": "bash", "ps": "ps", "cmd": "cmd"}[kind], Run(ctx, run.here, strict=False), depth)
        return
    check_code_markers(code, ctx)
    scan_code_harness(code)
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


CODE_MOVE = re.compile(
    r"\bshutil\s*\.\s*move\s*\(|\bos\s*\.\s*(?:rename|renames|replace|symlink|link)\s*\(|\.\s*(?:rename|symlink_to|"
    r"hardlink_to|link_to)\s*\(|\b(?:rename|symlink|link)(?:Sync)?\s*\(|\bfs\w*\s*\.\s*(?:promises\s*\.\s*)?"
    r"(?:rename|symlink|link)\w*\s*\(|\bFile\s*\.\s*(?:rename|symlink|link)\b|\bFileUtils\s*\.\s*(?:mv|ln_s?)\b|"
    r"\bfrom\s+(?:os|shutil)\s+import\b[^\n]*\b(?:rename|renames|replace|symlink|link|move)\b|"
    r"\bMove-Item\b|\bRename-Item\b|\bSymbolicLink\b", re.I)


def scan_code_harness(code: str) -> None:
    """A call that renames, moves or links something, in a script that names `.git`, `.claude` or `.hokma`."""
    if not CODE_MOVE.search(code):
        return
    for m in STR_LITERAL.finditer(code):
        literal = m.group(1) if m.group(1) is not None else m.group(2)
        if literal and harness_move_target(literal) and len(literal) < 400:
            raise Block("hokma_internal", short(literal))


def check_code_markers(code: str, ctx: Ctx) -> None:
    if CRED_CODE.search(code):
        raise Block("secrets", "keyring")
    low = norm_text(code)
    if SECRET_RE.search(low):
        raise Block("secrets", short(SECRET_RE.search(low).group(0)))
    if INTERNAL_RE.search(low) or SETTINGS_RE.search(low) or GIT_REFS_RE.search(low):
        raise Block("hokma_internal", short((INTERNAL_RE.search(low) or SETTINGS_RE.search(low)
                                             or GIT_REFS_RE.search(low)).group(0)))
    if names_shutdown_route(code):
        raise Block("hokma_internal", "/api/system/shutdown")
    if LAUNCHER_CODE.search(code):
        raise Block("hokma_internal", short(LAUNCHER_CODE.search(code).group(0)))
    if ENV_REDEFINE.search(code):
        raise Block("hokma_internal", short(ENV_REDEFINE.search(code).group(0)))
    if GIT_ENV_RE.search(code):
        raise Block("hokma_internal", short(GIT_ENV_RE.search(code).group(0)))
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
    """Destinations of the listed write commands must be inside the working folder."""
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
            full = check_write_dest(target.text, ctx, run.here)
            if full and name in ("set-content", "sc", "out-file") and not any(ps_param(w.text, "append") for w in args):
                empty = piped_empty(prev, mode) if cmd.pipe_in else ps_empty_write(args, positional, target)
                if empty and os.path.isfile(full) and os.path.getsize(full) > 0:
                    raise Block("truncate", short(target.text))


def piped_empty(prev: Cmd | None, mode: str) -> bool:
    """The command before `| Set-Content f` produces nothing: an empty string, $null or an empty echo."""
    words = significant(prev.words, mode) if prev is not None else []
    if len(words) == 1:
        return words[0].text.strip() == "" or words[0].text.lower() == "$null"
    return bool(words) and empty_producer(words, prev, mode)


def ps_empty_write(args: list[Word], positional: list[Word], target: Word) -> bool:
    values = []
    for i, w in enumerate(args):
        if ps_param(w.text, "value") and i + 1 < len(args):
            values.append(args[i + 1].text)
    others = [w for w in positional if w is not target]
    values += [w.text for w in others]
    return all(v.strip() == "" or v.lower() == "$null" for v in values)


# ------------------------------------------------------------------------------------------------ the tools

FILE_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
READ_TOOLS = {"Read", "Grep", "Glob"}
SHELL_TOOLS = {"Bash": "bash", "PowerShell": "ps"}
ORDER_TOOL = re.compile(r"(?:place|submit|send|create|modify|replace|cancel)[_-]?order(?!s?[_-]?book)|"
                        r"order[_-](?:create|send|cancel)", re.I)


def edit_result(tool: str, tool_input: dict, current: str) -> str | None:
    if tool == "Edit":
        old, new = tool_input.get("old_string"), tool_input.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str) or old == "":
            return None
        return current.replace(old, new) if tool_input.get("replace_all") else current.replace(old, new, 1)
    if tool == "MultiEdit":
        text = current
        for edit in tool_input.get("edits") or []:
            if not isinstance(edit, dict):
                return None
            old, new = edit.get("old_string"), edit.get("new_string")
            if not isinstance(old, str) or not isinstance(new, str) or old == "":
                return None
            text = text.replace(old, new) if edit.get("replace_all") else text.replace(old, new, 1)
        return text
    return None


def check_file_tool(tool: str, tool_input: dict, ctx: Ctx, cwd: str) -> None:
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(target, str) or not target:
        raise Block("hokma_internal", "no path")
    full = check_write_dest(target, ctx, cwd)
    if full is None or tool == "NotebookEdit":
        return
    try:
        size = os.path.getsize(full) if os.path.isfile(full) else 0
    except OSError:
        size = 0
    if size == 0:
        return
    if tool == "Write":
        content = tool_input.get("content")
        if isinstance(content, str) and content.strip() == "":
            raise Block("truncate", short(target))
    elif size <= MAX_EDIT_BYTES:
        try:
            with open(full, "rb") as f:
                current = f.read().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return
        result = edit_result(tool, tool_input, current)
        if result is not None and result.strip() == "" and current.strip() != "":
            raise Block("truncate", short(target))


def check_read_tool(tool: str, tool_input: dict, ctx: Ctx, cwd: str) -> None:
    candidates = []
    if tool == "Read":
        candidates.append(tool_input.get("file_path"))
    elif tool == "Grep":
        candidates += [tool_input.get("path"), tool_input.get("glob")]
    else:
        candidates += [tool_input.get("path"), tool_input.get("pattern")]
    for item in candidates:
        if isinstance(item, str) and item:
            check_text_markers(item, ctx, settings=False)
            check_real_path(item, ctx, settings=False)


SECRET_FILES = re.compile(r"database\.json|claude-config/(?:\.credentials\.json|\.claude\.json)")


def raw_data_rule(low: str, ctx: Ctx) -> str | None:
    """`secrets` or `hokma_internal` when the command text names Hokma's data folder (default places or the real one)."""
    spots = []
    m = RAW_DATA_RE.search(low)
    if m:
        spots.append(low[m.start():m.start() + 200])
    data = norm_text(ctx.data)
    if len(data) > 3 and data in low:
        k = low.find(data)
        spots.append(low[k:k + len(data) + 200])
    if not spots:
        return None
    return "secrets" if any(SECRET_FILES.search(s) for s in spots) else "hokma_internal"


def check_command(tool: str, command: object, cwd: str, ctx: Ctx) -> None:
    if not isinstance(command, str):
        raise Block("delete", "command is not text")
    run = Run(ctx, cwd)
    mode = SHELL_TOOLS[tool]
    rule = raw_data_rule(norm_text(command), ctx)              # backslashes may be eaten by the shell reading below
    if rule:
        raise Block(rule, short(command))
    if ENV_REDEFINE.search(command) or GIT_ENV_RE.search(command):
        stripped = re.sub(r"\b(?:echo|printf)\b[^;&|\n]*", "", command)
        if ENV_REDEFINE.search(stripped) or GIT_ENV_RE.search(stripped):
            raise Block("hokma_internal", short(command))
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
    "truncate": lambda x: (t("hardblock.truncate", target=x), t("hardblock.hint.truncate")),
    "hokma_internal": lambda x: (t("hardblock.hokma_internal", target=x), t("hardblock.hint.hokma_internal")),
    "force_push": lambda x: (t("hardblock.force_push", target=x), t("hardblock.hint.force_push")),
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
    return Ctx(data, roots, arguments(args, "--protect") or [data], conversation_id=env.get("HOKMA_CONVERSATION_ID", ""),
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
