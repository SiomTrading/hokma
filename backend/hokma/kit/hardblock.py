"""PreToolUse hook: the locks that are always on, in both permission modes.

    real_money   calling a broker outside the order tools of Hokma: order tools of other servers and the hosts and URL shapes of brokers
    secrets      the stores of keys and sign-ins: the keys of brokers and Connections, the tokens and sign-ins of Hokma, the keyring of the
                 system, and the credential files (SSH, cloud, Git, browser profiles)
    delete       deleting files or folders, including the obfuscated forms (the trash tool is the way). One exception (1.3.3): an agent
                 may delete the files and folders IT created in this same chat session (see "own files" below)
    hokma_internal  writing, by shell or interpreter, the lock and its hooks and registration, the installed package, the runtime venv (pip and
                 ensurepip included) and the file of the base (hokma.db, -wal, -shm, -journal). Reading all of them is free

Everything else is the owner's to ask for and the agent's to do: install software, write anywhere, import Hokma's code (not the modules that talk to
a broker or put orders: hokma.broker, schwab, connections, desks, exec, core.processguard), read the base, load code by a path or a built name,
run python from any folder, Git, permission settings. The protection of the file tools on the hook files is `guard.py`, a separate hook
(`outside_workdir` left this one in 1.3.1; 1.3.1 round 2 brought back `hokma_internal` for the shell and the interpreters).

It runs for every tool (matcher `.*`) next to `guard.py`, the same way: by absolute path, with an isolated
interpreter (`python -I /abs/hardblock.py --data D --workdir W --protect P ...`) from the data folder, and
`|| exit 2` in the command. It is FAIL CLOSED: any error, unreadable input or damaged data file exits with code 2.
A path that is built at run time, a system folder or code assembled at run time is not a reason to refuse: only the stores above are.

A refusal prints `HOKMA_BLOCKED <rule>` and the reason on stderr (the model reads it and takes another way) and
appends one JSON line to `<data>/guard/blocks.jsonl` (`ts`, `rule`, `tool`, `target` cut to 200 characters,
`conversation_id`; never file contents). The engine turns that line into a `guard_events` row and a card in the chat.

Commands are not matched as raw text. They are read the way a shell reads them: quotes, escapes, `$'..'` strings,
binary paths and wrappers (`sudo`, `env`, `xargs`, `find -exec`, `bash -c`, `cmd /c`, `powershell -c`, `python -c`,
`node -e`) are resolved first; `;`, `&&`, `||`, `|`, `&` and new lines split the command; `$(...)`, backticks and
here-documents are read as commands; a command whose NAME is built at run time (`$x f`, `$(echo rm) f`), `eval`,
`iex`, encoded commands and a decoder piped into a shell are refused outright (they hide a delete), except the installer idiom that feeds
the shell with a download (`curl url | sh`, `bash -c "$(curl url)"`, `iex (irm url)`), which software installers
use. A script of the working folder that a command runs is read and its text is checked with the same rules.

Scope of this hook: it is defense in depth, not a sandbox. The agent can run arbitrary code, so the integrity of the
hooks rests on the structural protections too: the sha256 integrity manifest of the installed app, which Hokma
verifies at every launch and `hokma doctor` reports, and the settings file that is rebuilt at every launch.

Standard library plus `hokma.i18n` and `hokma.kit.guard` only.
"""
import ast
import fnmatch
import glob
import json
import ntpath
import os
import posixpath
import re
import sys
import urllib.parse
from datetime import datetime, timezone

from hokma.i18n import t
from hokma.kit import guard
from hokma.kit.guard import _fold, under_any
from hokma.permissions.modes import ALWAYS_ASK

BLOCK = 2
RULES = ("delete", "hokma_internal", "secrets", "real_money")
MAX_DEPTH = 6
MAX_SCRIPT_BYTES = 512 * 1024
MAX_TARGET = 200
ORDER_CONSUMER = "hokma_broker"                                    # the broker service: the only consumer of `order_exceptions`
BROKERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hardblock_brokers.json")


class Block(Exception):
    """A rule fired. `target` is what the call wanted to touch (shown to the owner, cut to 200 characters)."""

    def __init__(self, rule: str, target: str = "", reason: str = "") -> None:
        super().__init__(rule)
        self.rule, self.target, self.reason = rule, str(target), reason


# ------------------------------------------------------------------------------------------------ context

class Ctx:
    def __init__(self, data: str, roots: list[str], protect: list[str], home: str | None = None,
                 brokers: dict | None = None, cwd: str | None = None, conversation_id: str = "",
                 task_id: str = "") -> None:
        self.data = data
        self.roots = [r for r in roots if r]                    # the working folder: what is judged as the owner's work, not as Hokma's data
        # `protect` (the hooks, the installed package, the runtime venv: see `settings.protected_paths`) is what a shell or an interpreter does not write (`integrity_target`);
        # `guard.py` still judges the file tools
        # `<data>/guard`, `<data>/bin` and `<data>/tools` (the block log, the PATH folders) are protected like the hooks: a shell or an interpreter does not write them. Reading them is free.
        # The identity hook and the server write there with their own process, not through this hook.
        self.protect = [p for p in protect if p]
        self.home = home or os.path.expanduser("~")
        self.brokers = brokers if brokers is not None else load_brokers()
        self.cwd = cwd or (self.roots[0] if self.roots else os.getcwd())
        self.conversation_id, self.task_id = conversation_id, task_id
        self.session = ""                                       # the chat session of the call (see session_of): who owns the files it creates
        self.pending: set[str] = set()                          # what this call creates, written to the session file only when the call is allowed
        self._own: dict[str, tuple | None] | None = None        # the entries of the session file (path -> identity), read once
        self.links: set[str] = set()                            # the links this same call makes (ln, mklink, New-Item SymbolicLink): never deleted through
        self.gone: set[str] = set()                             # what this call deletes of the list, struck out when the call is allowed
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
               "HOKMA_DATA_DIR": self.data, "HOKMA_DATA": self.data, "PWD": self.cwd}
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


_MARKER = re.compile(r"\{(\w+)\}")


def unsafe_segment(segment: str) -> bool:
    """A path segment that is `.` or `..`, or that hides one, a `/` or a `\\` behind percent encoding (`%2e`, `%2F`, `%5c`,
    also encoded twice: `%252e`). It is decoded again and again until nothing changes; a segment that keeps decoding
    after four rounds is refused as well. A plain `%24SPX` (an encoded `$`) is not unsafe."""
    current = segment
    for _ in range(4):
        if current in (".", "..") or "/" in current or "\\" in current:
            return True
        decoded = urllib.parse.unquote(current)
        if decoded == current:
            return False
        current = decoded
    return True


def _template_matches(template: str, params: dict, bare: str) -> bool:
    """A `path_template` rule: `{name}` stands for exactly one path segment (no `/`, not empty) that must fully match
    the regex given for `name` in `params`. The path has the same number of segments as the template; every other
    segment is compared as written."""
    wanted, got = template.split("/"), bare.split("/")          # a trailing "/" is an empty segment: no match
    if len(wanted) != len(got):
        return False
    for want, have in zip(wanted, got):
        marker = _MARKER.fullmatch(want)
        if marker is None:
            if want != have:
                return False
            continue
        pattern = params.get(marker.group(1)) if isinstance(params, dict) else None
        if not isinstance(pattern, str) or not have or "/" in have or unsafe_segment(have) or re.fullmatch(pattern, have) is None:
            return False
    return True


def read_exception_match(host: str, method: str, path: str, consumer: str, brokers: dict | None = None) -> dict | None:
    """The `read_exceptions` rule that lets `consumer` make exactly this request to a listed host, or None.

    Only Hokma's own backend asks this (the option chain reads contracts from the live Alpaca host, SPEC-014; the Schwab
    reads of SPEC-015). The hook never calls it: for an agent every listed host stays refused, whatever the method or
    the path. A rule matches only when host, method (compared in capitals), consumer and the path (no query) all agree.
    Two kinds of rule: `exact_path_query_allowed` (the path is the rule's `path`) and `path_template` (the rule's
    `path` has `{name}` markers and `params` gives the regex each marker must fully match; see `_template_matches`)."""
    rules = (brokers if brokers is not None else load_brokers()).get("read_exceptions", [])
    bare = path.split("?", 1)[0].split("#", 1)[0]
    for rule in rules:
        if not (isinstance(rule, dict) and rule.get("consumer") == consumer
                and str(rule.get("host", "")).lower() == host.lower()
                and str(rule.get("method", "")).upper() == method.upper()):
            continue
        kind = rule.get("match")
        if kind == "exact_path_query_allowed" and rule.get("path") == bare:
            return rule
        if kind == "path_template" and _template_matches(str(rule.get("path", "")), rule.get("params") or {}, bare):
            return rule
    return None


def stream_hosts(brokers: dict | None = None) -> tuple[str, ...]:
    """The only hosts the Schwab Streamer socket may be opened against (SPEC-016 4.1 point 2). A token never travels to a host
    that is not in this list. Read from `stream_hosts` of `hardblock_brokers.json`; not a rule the hook applies to agents."""
    raw = (brokers if brokers is not None else load_brokers()).get("stream_hosts", [])
    return tuple(str(h).lower() for h in raw if isinstance(h, str)) if isinstance(raw, list) else ()


def read_exception_allows(host: str, method: str, path: str, consumer: str, brokers: dict | None = None) -> bool:
    """True when `read_exception_match` finds a rule for this request."""
    return read_exception_match(host, method, path, consumer, brokers) is not None


def _rule_match(list_name: str, host: str, method: str, path: str, consumer: str, brokers: dict | None) -> dict | None:
    rules = (brokers if brokers is not None else load_brokers()).get(list_name, [])
    bare = path.split("?", 1)[0].split("#", 1)[0]
    for rule in rules if isinstance(rules, list) else []:
        if not (isinstance(rule, dict) and rule.get("consumer") == consumer
                and str(rule.get("host", "")).lower() == host.lower()
                and str(rule.get("method", "")).upper() == method.upper()):
            continue
        if rule.get("match") == "path_template" and _template_matches(str(rule.get("path", "")),
                                                                      rule.get("params") or {}, bare):
            return rule
    return None


def order_exception_match(host: str, method: str, path: str, consumer: str, brokers: dict | None = None) -> dict | None:
    """The `order_exceptions` rule (SPEC-015 18.7) that lets `consumer` send, replace, cancel or preview exactly this order
    request, or None. A separate list from `read_exceptions`: a rule of one list never authorizes a request of the other
    (this function never looks at `read_exceptions`, and `read_exception_match` never looks at this list). Only
    `path_template` rules count, and the hook never calls this: for an agent every listed host stays refused."""
    if consumer != ORDER_CONSUMER:                                  # no rule file can give an order to anybody else
        return None
    return _rule_match("order_exceptions", host, method, path, consumer, brokers)


def order_exception_allows(host: str, method: str, path: str, consumer: str, brokers: dict | None = None) -> bool:
    """True when `order_exception_match` finds a rule for this request."""
    return order_exception_match(host, method, path, consumer, brokers) is not None


def exception_match(host: str, method: str, path: str, consumer: str, brokers: dict | None = None) -> dict | None:
    """The rule of the one list that belongs to `consumer`: the backend reads (`read_exceptions`), the broker service orders
    (`order_exceptions`). Any other consumer gets None. `schwab/http.py` asks this, so it never names the lists."""
    finder = {"hokma_backend": read_exception_match, ORDER_CONSUMER: order_exception_match}.get(consumer)
    return finder(host, method, path, consumer, brokers) if finder else None


# ------------------------------------------------------------------------------------------- text and paths

_VAR = re.compile(r"\$\{?env:([A-Za-z_]\w*)\}?|\$\{([A-Za-z_]\w*)\}|\$([A-Za-z_]\w*)|%([A-Za-z_]\w*)%", re.I)
_WIN_ABS = re.compile(r"^[A-Za-z]:[\\/]")
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
DEVICES = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/stdin", "/dev/tty", "nul", "con", "$null", "nul:", "conout$", "conin$",
           "//./nul", "//./con", "//?/nul", "//?/con", "//./conout$", "//./conin$"}
DEVICE_NAMES = {"nul", "nul:", "con", "conout$", "conin$"}


def norm_text(s: str) -> str:
    return s.replace("\\", "/").lower()


def expand(text: str, ctx: Ctx) -> str:
    """`~` and the well-known variables, expanded as text. Unknown variables stay as written."""
    if text == "~" or text.startswith(("~/", "~\\")):
        text = ctx.home + text[1:]
    elif text == "~+" or text.startswith(("~+/", "~+\\")):
        text = ctx.cwd + text[2:]                               # `~+` is the current folder

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


_MSYS_DRIVE = re.compile(r"^(?:/cygdrive)?/([A-Za-z])(?=/|$)")


def msys_to_win(p: str) -> str:
    """Git Bash spells `C:\\Users\\a` as `/c/Users/a` (Cygwin: `/cygdrive/c/...`). On Windows that is the drive C:, not a folder named c on the current drive."""
    m = _MSYS_DRIVE.match(p) if os.name == "nt" else None
    return f"{m.group(1).upper()}:/" + p[m.end():].lstrip("/") if m else p


def joined(path: str, cwd: str) -> str:
    path, cwd = msys_to_win(path), msys_to_win(cwd)
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
DATA_SECRET_PLACES = (r"hokma/(?:database\.json|identity\.key|codex-home|acp-home|backups)|"
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
DATA_SECRET_TOPS = ("database.json", "identity.key", "codex-home", "acp-home", "backups")
CONFIG_SECRET_PREFIXES = (".claude.json", ".credentials", "credentials.json", "backups")
CRED_CODE = re.compile(r"engine_key|\bimport\s+keyring\b|\bfrom\s+keyring\b|\bkeyring\s*\.\s*(?:get|set|delete)|"
                       r"\bPasswordVault\b|\bCredRead\b|Get-StoredCredential|\bsecretstorage\b|"
                       r"(?:import_module|__import__)\s*\(\s*[\"']keyring[\"']", re.I)          # loading it by its literal name


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



def scan_secret_text(text: str) -> str | None:
    low = norm_text(text)
    m = SECRET_RE.search(low)
    return m.group(0).strip(" \"'=:;,()<>|&/@") if m else None


def check_text_markers(text: str, ctx: Ctx) -> None:
    """Secrets named in a text that names a path or in code."""
    expanded = expand(text, ctx)
    for candidate in (text, expanded):
        if scan_secret_text(candidate) or data_secret_in_text(norm_text(candidate), ctx):
            raise Block("secrets", short(text))


def in_workfolder_only(candidate: str, ctx: Ctx) -> bool:
    """True when the path sits under the working folder of the fund (its scratch folder included) and neither the path nor where it really leads
    is Hokma's data folder. The working folder holds the work of the owner and of the agents: it is never a place of keys. The comparison is by the
    names of the folders, so an identity that a file system reports badly cannot make the working folder look like the data folder; a link that leads
    into the data folder is still judged as the data folder."""

    def inside(path: str, root: str) -> bool:
        low, base = _fold(os.path.normpath(path)), _fold(os.path.normpath(root))
        return low == base or low.startswith(base.rstrip("\\/") + os.sep)

    real = os.path.realpath(candidate)
    if any(inside(p, r) for p in (candidate, real) for r in (ctx.data, os.path.realpath(ctx.data))):
        return False
    return any(inside(candidate, root) for root in ctx.roots) and any(inside(real, root) or inside(real, os.path.realpath(root)) for root in ctx.roots)


def check_real_path(path: str, ctx: Ctx, *, here: str | None = None) -> None:
    """Resolve a path (relative to the call's folder) and refuse the secret places by identity: both spellings of it (as written and by
    real path), so a link that leads into the stores of keys is judged as the store. A path built at run time cannot be judged and is free."""
    if is_device(path) or (unresolved(path) and not path.startswith("~")):
        return
    full = clean_path(joined(expand(path, ctx), here or ctx.cwd))
    if is_device(full) or (is_win_abs(full) and os.name != "nt"):
        return                                                  # a Windows path on another system: text markers only
    for candidate in dict.fromkeys((full, os.path.realpath(full))):
        if SECRET_RE.search(norm_text(candidate)) or (
                under_any(candidate, [ctx.data]) and not in_workfolder_only(candidate, ctx) and read_hidden(candidate, ctx, content=False)):
            raise Block("secrets", short(path))


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
    if is_device(full) or (is_win_abs(full) and os.name != "nt"):
        return
    cut = min((full.find(c) for c in GLOB_CHARS if c in full), default=-1)
    if cut >= 0:
        tail = full[cut:].replace("\\", "/")
        if ".." in tail.split("/") or any(part.startswith(".") for part in (full[:cut].replace("\\", "/").rsplit("/", 1)[-1] + tail).split("/")):
            if under_any(os.path.dirname(full[:cut]) or "/", [ctx.data]):
                raise Block("secrets", short(path))             # a glob that can reach dot files or climb out of its folder
        full = os.path.dirname(full[:cut]) or "/"
    for candidate in {full, os.path.realpath(full)}:
        if under_any(candidate, [ctx.data]) and not in_workfolder_only(candidate, ctx) and read_hidden(candidate, ctx, content=content):
            raise Block("secrets", short(path))


def check_path_arg(text: str, ctx: Ctx) -> None:
    check_text_markers(text, ctx)
    if dynamic_key_path(text, ctx):
        raise Block("secrets", short(text.replace("\0", "$(...)")))
    check_real_path(text, ctx)


_FD_DEVICE = re.compile(r"^/(?:dev|proc/self)/fd/\d+$")
_RAW_DRIVE = re.compile(r"^//[?.]/[a-z]:")
POSIX_DEVICES = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/stdin", "/dev/tty"}


def is_device(p: str) -> bool:
    """A destination that is no file: the null device and the standard streams, by EXACT name after normalising the path (`/dev/null`, `/dev/fd/N`,
    `$null`). `/dev/fd/../../data/x` normalises to somewhere else and is no device. The names of Windows (`NUL`, `CON`, `\\\\.\\NUL`) count only on Windows
    (`os.name == 'nt'`), where they are the device in any folder (`C:\\work\\NUL`), and never behind a raw path with a drive (`\\\\?\\C:\\x\\NUL` is a file)."""
    low = p.strip("\"'").lower().replace("\\", "/")
    if low == "$null":
        return True
    raw_with_drive = bool(_RAW_DRIVE.match(low))
    if not raw_with_drive:
        norm = posixpath.normpath(low)
        if norm in POSIX_DEVICES or _FD_DEVICE.match(norm):
            return True
    return os.name == "nt" and not raw_with_drive and low.rsplit("/", 1)[-1] in DEVICE_NAMES


def check_write_dest(text: str, ctx: Ctx, cwd: str) -> str | None:
    """A path a command or a tool writes to: refused when it is a store of keys, by its text or by where it really leads. Returns the resolved
    path (None when it cannot be known). Where it writes is the owner's business: the folder is not judged."""
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
PY_NAME = re.compile(r"^(python[\d.]*[dt]?|pythonw[\d.]*[dt]?|py|pypy[\d.]*)$")
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
        self.pure_delete = False                # the whole call is only deletes (rm, del, Remove-Item, rmdir) chained with ; or &&: the only shape that may delete its own files

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
    cmds = list(lex(text, mode))
    if depth == 0:
        run.pure_delete = only_deletes(cmds, mode, run)
    for cmd in cmds:
        for inner in cmd.subs:
            analyze_text(inner, "bash" if mode == "bash" else mode, run, depth + 1)
        analyze_cmd(cmd, prev if cmd.pipe_in else None, mode, run, depth, stdin_text)
        prev = cmd


def only_deletes(cmds: list[Cmd], mode: str, run: Run) -> bool:
    """True when the call is nothing but delete commands: no other command, no redirection, no pipe, no substitution, no assignment, no wrapper."""
    found = False
    for cmd in cmds:
        if cmd.empty():
            continue
        if cmd.redirs or cmd.heredocs or cmd.herestrs or cmd.subs or cmd.pipe_in or cmd.call or not cmd.words:
            return False
        if len(significant(cmd.words, mode)) != len(cmd.words):
            return False
        w = cmd.words[0]
        if w.dyn or w.glob or w.quoted or base_name(w.text) not in OWN_DELETE or re.split(r"[\\/]", w.text)[-1] != w.text:
            return False
        found = True
    return found


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
            check_path_arg(w.text, ctx)                         # reading a file is free, what it holds is not
            continue
        if op == ">&" and (w.text.isdigit() or w.text == "-"):
            continue
        check_write_dest(w.text, ctx, run.here)
        check_integrity_path(w.text, ctx, run.here)             # the hooks, the package, the runtime venv and the file of the base are not written by a redirection
        note_create(ctx, w.text, run.here)
        if w.text.lower().endswith(PYTHON_FILES):                # a Python file written with a heredoc or an echo is judged like the code it holds
            check_sensitive_import("\n".join([*cmd.heredocs, *cmd.herestrs, " ".join(x.text for x in words[1:])]))


# Tools that walk a whole tree. Pointed at a folder that CONTAINS the data folder (`~`, `~/.local/share`,
# `%LOCALAPPDATA%`, `/`) they read the database and the credentials, so that is a read of the data folder's content.
RECURSIVE_ALWAYS = {"ugrep", "ug", "rg", "ag", "ack", "rgrep", "findstr", "select-string", "sls"}
RECURSIVE_FLAG = {"grep": "rRd", "egrep": "rRd", "fgrep": "rRd", "cp": "rRa", "scp": "r", "rsync": "ra",
                  "zip": "rR", "7z": "", "7za": "", "tar": "", "pax": "", "cpio": "", "xcopy": "", "robocopy": "",
                  "copy-item": "", "cpi": "", "compress-archive": ""}


SEARCHERS = {"grep", "egrep", "fgrep", "rg", "select-string", "sls"}


# ------------------------------------------------------------------------------- 1.3.1 doors (task 3436, Vera round 1)
# Three narrow doors on top of the keys, the brokers and delete. Each one names exactly what it closes; nothing else is judged.

# --- A. a path with a piece decided at run time that can lead to a file of keys of the data folder
_DYN_PIECE = re.compile(r"\0|\$\{?env:[A-Za-z_]\w*\}?|\$\{[A-Za-z_]\w*\}|\$[A-Za-z_]\w*|%[A-Za-z_]\w*%|\x60")
KEY_COMPONENTS = ("database.json", "identity.key", "codex-home", "acp-home", ".credentials", "credentials.json")


def _key_component(comp: str, names_data: bool) -> bool:
    """A path component that is a file or folder of keys. `backups` is common, so it counts only when the text names the data folder; a glob counts when its literal start
    can only grow into a key name (`ident*.key`), not for a bare `*.json`."""
    if not comp:
        return False
    if comp.startswith(KEY_COMPONENTS):
        return True
    if comp.startswith("backups"):
        return names_data
    if any(c in comp for c in GLOB_CHARS):
        literal = re.split(r"[*?\[]", comp)[0]
        return len(literal) >= 3 and any(k.startswith(literal) for k in KEY_COMPONENTS)
    return False


def dynamic_key_path(text: str, ctx: Ctx) -> bool:
    """True when a path has a piece that only the shell knows (`$(cmd)`, `$var`) and what follows that piece can be a file of keys of the data folder
    (`$d/identity.key`, `x/../$(basename x)/identity.key`), or when it names the data folder and the piece is the very next component
    (`<data>/$x`). A dynamic path that does not go near the keys is not judged."""
    low = norm_text(expand(text, ctx))
    pieces = list(_DYN_PIECE.finditer(low))
    if not pieces:
        return False
    names_data = names_data_folder(low, ctx)
    if any(_key_component(comp, names_data) for comp in low[pieces[0].end():].split("/")[1:]):
        return True
    if names_data:
        data = norm_text(ctx.data).rstrip("/")
        starts = [m.end() for m in DATA_PLACE_RE.finditer(low)]
        k = low.find(data) if len(data) > 3 else -1
        while k >= 0:
            starts.append(k + len(data) + 1)
            k = low.find(data, k + 1)
        for begin in starts:
            parts = low[begin:].split("/")
            for i in range(min(2, len(parts))):
                if _DYN_PIECE.search(parts[i]) and (i == 0 or parts[0] == "claude-config"):
                    return True
    return False


# --- B. the modules of Hokma that talk to the broker or put orders: not imported or loaded by an agent
SENSITIVE_MODS = r"(?:broker|schwab|connections|desks|exec|core\s*\.\s*processguard|kit\s*\.\s*(?:exec_tools|desk_tools))"
SENSITIVE_PATH = r"(?:broker|schwab|connections|desks|exec|core[\\/]+processguard|kit[\\/]+(?:exec_tools|desk_tools))"
SENSITIVE_NAMES = {"broker", "schwab", "connections", "desks", "exec"}
SENSITIVE_IMPORT_RES = (
    re.compile(r"(?<!\w)hokma\s*\.\s*" + SENSITIVE_MODS + r"(?!\w)"),                    # import, from, -m, importlib and __import__ with the literal name
    re.compile(r"(?<!\w)hokma[\\/]+" + SENSITIVE_PATH + r"(?!\w)"),                      # loaded by its path
    re.compile(r"import_module\s*\(\s*['\"]\.\s*" + SENSITIVE_MODS + r"['\"]\s*,\s*(?:package\s*=\s*)?['\"]hokma['\"]"),
)
_FROM_HOKMA = re.compile(r"\bfrom\s+hokma\s+import\s*\(?([\w\s,]*)")
_IMPORT_HOKMA_AS = re.compile(r"\bimport\s+hokma\s+as\s+(\w+)")
_NAME_LITERAL = re.compile(r"""(?:getattr\s*\([^()]*|fromlist\s*=\s*[\[(][^\])]*)['"](broker|schwab|connections|desks|exec)['"]""")


def sensitive_import(code: str) -> str | None:
    """The module of Hokma that holds the broker or the orders that `code` imports or loads by a literal name, or None. Names built at run time are free."""
    for rx in SENSITIVE_IMPORT_RES:
        m = rx.search(code)
        if m:
            return m.group(0)
    for m in _FROM_HOKMA.finditer(code):
        for name in re.findall(r"\w+", m.group(1)):
            if name in SENSITIVE_NAMES:
                return "hokma." + name
    for m in _IMPORT_HOKMA_AS.finditer(code):
        alias = re.search(r"(?<![\w.])" + re.escape(m.group(1)) + r"\s*\.\s*(" + "|".join(SENSITIVE_NAMES) + r")(?!\w)", code)
        if alias:
            return "hokma." + alias.group(1)
    named = _NAME_LITERAL.search(code)
    if named and re.search(r"\bhokma\b", code):
        return "hokma." + named.group(1)                                              # getattr(hokma, 'broker'), __import__('hokma', fromlist=['broker'])
    return None


PYTHON_FILES = (".py", ".pyw", ".ipynb", ".pth")


def check_sensitive_import(code: str) -> None:
    found = sensitive_import(code)
    if found:
        raise Block("real_money", short(found))


# --- C and D. the files of Hokma itself: the lock, its hooks and registration, the installed package, the runtime venv and the base
PROTECT_NAMES_RE = re.compile(r"hokma/kit/(?:hardblock|guard|identity_hook|creator_gate)\.py|hokma/kit/hardblock_brokers\.json")
DB_FILE_RE = re.compile(r"(?:^|/)hokma\.db(?:-wal|-shm|-journal)?$")
DB_NAMED_RE = re.compile(r"(?:^|[/\\\s'\"=:])hokma\.db(?:-wal|-shm|-journal)?(?![\w.])", re.I)
SQL_WRITE = re.compile(
    r"\binsert\s+(?:or\s+\w+\s+)?into\b|\bupdate\s+[\w.\"'\[\]\x60]+\s+set\b|\bdelete\s+from\b|\bdrop\s+(?:table|index|view|trigger)\b|"
    r"\bcreate\s+(?:temp\w*\s+)?(?:unique\s+)?(?:table|index|view|trigger|virtual)\b|\balter\s+table\b|\breplace\s+into\b|\battach\b|\bvacuum\b|\breindex\b|"
    r"\bpragma\s+(?:writable_schema|journal_mode|user_version|synchronous\s*=|wal_checkpoint|schema_version)|\bexecutescript\b|\.commit\s*\(|\bto_sql\b|\bupsert\b", re.I)
SQL_DOT_WRITE = re.compile(r"(?:^|\s|;)\.(?:import|read|restore|shell|system|load|clone)\b", re.I)
CODE_WRITES = re.compile(
    r"""\bopen\s*\([^)]*?['"][rbtU]*[wax+][bt+]*['"]|\bmode\s*=\s*['"][rbtU]*[wax+][bt+]*['"]|\bO_(?:WRONLY|RDWR|APPEND|TRUNC|CREAT)\b|"""
    r"""\.write_(?:text|bytes)\s*\(|\.truncate\s*\(|\bos\s*\.\s*(?:replace|rename|remove|unlink|truncate|chmod|link|symlink|utime)\s*\(|\bshutil\s*\.\s*(?:move|rmtree)\b|"""
    r"""\bwriteFile(?:Sync)?\b|\bappendFile(?:Sync)?\b|\bcreateWriteStream\b|\bcopyFile(?:Sync)?\b|\brename(?:Sync)?\s*\(|\bFile\s*\.\s*(?:write|open)\b|\bsed\s+-\w*i|\btee\b|"""
    + EXEC_HINT.pattern, re.I)
_COPY_CALL = re.compile(r"\bshutil\s*\.\s*(?:copy2?|copyfile|copytree|copymode|copystat)\s*\(\s*[^,()]*(?:\([^()]*\))?[^,()]*,([^)]*)\)", re.I)
PIP_READ_ONLY = {"list", "show", "freeze", "check", "help", "inspect", "debug", "hash", "search", "index"}
PIP_IN_CODE = re.compile(r"\b(?:import|from)\s+(?:ensurepip|pip)\b|pip\s*\.\s*main\b|['\"]-m['\"]\s*,\s*['\"](?:pip|ensurepip)['\"]|"
                         r"(?:import_module|__import__)\s*\(\s*['\"](?:ensurepip|pip)['\"]")


def source_names_protected(run: "Run") -> bool:
    low = norm_text(run.source)
    return any(len(r) > 3 and r in low for r in (norm_text(p).rstrip("/") for p in run.ctx.protect))


def integrity_target(text: str, ctx: Ctx, here: str) -> bool:
    """True when the path `text` is one of Hokma's own files that an agent does not write: the lock and its hooks (by name and by the protected paths
    of the registration), the installed package, the runtime venv and the file of the base with its -wal, -shm and -journal. Where it really leads counts."""
    if not text or text == "-":
        return False
    exp = expand(text, ctx)
    low = norm_text(exp).rstrip("/")
    if (PROTECT_NAMES_RE.search(low) or DB_FILE_RE.search(low)) and not is_device(exp):
        return True
    if unresolved(exp) and not exp.startswith("~"):
        return False                                            # built at run time: cannot be judged here
    full = clean_path(joined(exp, here))
    if is_device(full) or (is_win_abs(full) and os.name != "nt"):
        return False
    for cand in dict.fromkeys((full, os.path.realpath(full))):
        norm = norm_text(cand)
        if PROTECT_NAMES_RE.search(norm) or DB_FILE_RE.search(norm) or (ctx.protect and under_any(cand, ctx.protect)):
            return True
    return False


def check_integrity_path(text: str, ctx: Ctx, here: str) -> None:
    for form in (text, *brace_variants(text, ctx=ctx)):
        if integrity_target(form, ctx, here):
            raise Block("hokma_internal", short(text))


# What each writer touches: every operand, only the destination (the last operand or `-t`), the operands after the first, or the value of an option.
WRITES_ALL = {"tee", "touch", "truncate", "chmod", "chown", "chgrp", "chattr", "setfacl", "patch", "ed", "ex", "vi", "vim", "nvim", "nano", "pico", "emacs",
              "sponge", "mkdir", "mv", "move", "mkfifo", "mknod", "set-content", "sc", "add-content", "ac", "out-file", "new-item", "ni", "rename-item", "rni",
              "clear-content", "clc", "move-item", "mi", "tee-object", "export-csv", "ren", "rename", "icacls", "attrib", "takeown"}
WRITES_LAST = {"cp", "install", "ln", "rsync", "scp", "copy", "copy-item", "cpi", "mklink"}
WRITES_AFTER_FIRST = {"xcopy", "robocopy"}
SED_NAMES = {"sed", "gsed"}
INPLACE_NAMES = {"perl", "ruby", "gawk", "awk"}
TARGET_OPTIONS = {"-t", "--target-directory", "-o", "--output", "-O", "--output-document", "-P", "--directory-prefix", "-d", "--directory", "-C", "--to-command",
                  "-destination", "-path", "-filepath", "-literalpath", "-name"}


def integrity_writes(name: str, words: list[Word], args: list[Word], run: Run) -> None:
    """A command that writes: refused when what it writes is one of Hokma's own files (see `integrity_target`). Reading them is free."""
    ctx = run.ctx
    texts = [w.text for w in args]
    low = [x.lower() for x in texts]
    operands = [x for x in texts if not x.startswith("-") or x == "-"]
    if name in SED_NAMES or name in ("perl", "ruby"):
        inplace = any(re.match(r"^-(?:[A-Za-z]*i.*|-in-place.*)$", x) for x in texts)
    elif name in ("gawk", "awk"):
        inplace = "inplace" in low
    if name in SED_NAMES or name in INPLACE_NAMES:
        if not inplace:
            return
        targets = operands
    elif name in WRITES_ALL:
        targets = operands
    elif name in WRITES_LAST:
        targets = operands[-1:]
    elif name in WRITES_AFTER_FIRST:
        targets = operands[1:]
    elif name in ("curl", "wget", "unzip", "7z", "7za", "dd"):
        targets = []
    elif name in ("tar", "pax", "cpio"):
        if not any(re.match(r"^-?[A-Za-z]*x[A-Za-z]*$", x) or x in ("--extract", "--get") for x in texts[:2] + [y for y in texts if y.startswith("-")]):
            return
        targets = []
    else:
        return
    values = []
    for i, x in enumerate(low):
        key = texts[i] if texts[i] in TARGET_OPTIONS else x             # `-C` and `-c`, `-O` and `-o` are different options
        if key in TARGET_OPTIONS and i + 1 < len(texts):
            values.append(texts[i + 1])
        elif x.startswith("--") and "=" in x and x.split("=", 1)[0] in TARGET_OPTIONS:
            values.append(texts[i].split("=", 1)[1])
        elif name in ("curl", "wget", "7z", "7za") and re.match(r"^-(?:o|O|P)[^-\s]", texts[i]) and not texts[i].lower().startswith("-ou"):
            values.append(texts[i][2:])
        elif name == "dd" and x.startswith("of="):
            values.append(texts[i][3:])
    for target in [*targets, *values]:
        check_integrity_path(target, ctx, run.here)


def check_pip(name: str, words: list[Word], args: list[Word], run: Run) -> None:
    """pip, ensurepip and uv pip in the runtime venv of Hokma (or in any protected folder). Listing what is installed is free."""
    texts = [w.text for w in args]
    sub = next((x.lower() for x in texts if not x.startswith("-")), "")
    if name != "ensurepip" and (sub in PIP_READ_ONLY or any(x in ("--version", "-V") for x in texts)):
        return
    if name == "uv" and sub != "pip":
        return
    here, ctx = run.here, run.ctx
    pointed = integrity_target(words[0].text, ctx, here) or source_names_protected(run) or any(
        integrity_target(v, ctx, here) for x in texts for v in ([x] if not x.startswith("--") or "=" not in x else [x.split("=", 1)[1]]))
    if pointed:
        raise Block("hokma_internal", short(" ".join(w.text for w in words)))


def check_python_gates(words: list[Word], args: list[Word], run: Run) -> None:
    """`python -m <module>`: the modules that talk to the broker are not run by an agent; pip and ensurepip are not run with the runtime venv."""
    how, value, _ = parse_python_args([w.text for w in args])
    ctx, here = run.ctx, run.here
    in_env = integrity_target(words[0].text, ctx, here) or source_names_protected(run)
    if how == "m" and value:
        found = sensitive_import(value)
        if found:
            raise Block("real_money", short(value))
        pointed = in_env or any(integrity_target(v, ctx, here) for x in (w.text for w in args) for v in ([x] if not x.startswith("--") or "=" not in x else [x.split("=", 1)[1]]))
        if pointed and value.split(".")[0].lower() in ("pip", "ensurepip") and not any(x.lower() in PIP_READ_ONLY for x in [w.text for w in args]):
            raise Block("hokma_internal", short(" ".join(w.text for w in words)))
    elif how == "c" and value and in_env and PIP_IN_CODE.search(value):
        raise Block("hokma_internal", short(value))


def code_writes_to(code: str, ctx: Ctx, mention: "re.Pattern[str] | None", needles: list[str]) -> bool:
    """The code names one of Hokma's own files (by a literal) and writes: `open(..., 'w')`, `write_text`, `os.replace`, a command, `shutil.move`, or a copy that has it as destination."""
    low = norm_text(code)
    named = bool(mention and mention.search(low)) or any(n in low for n in needles)
    if not named:
        return False
    if CODE_WRITES.search(code):
        return True
    return any((mention and mention.search(norm_text(m.group(1)))) or any(n in norm_text(m.group(1)) for n in needles) for m in _COPY_CALL.finditer(code))


def check_code_integrity(code: str, ctx: Ctx) -> None:
    needles = [n for n in (norm_text(p).rstrip("/") for p in ctx.protect) if len(n) > 3]
    if code_writes_to(code, ctx, PROTECT_NAMES_RE, needles):
        raise Block("hokma_internal", "hook files")
    if DB_NAMED_RE.search(code) and (SQL_WRITE.search(code) or code_writes_to(code, ctx, DB_NAMED_RE, [])):
        raise Block("hokma_internal", "hokma.db")


def check_sqlite(name: str, args: list[Word], stdin: str | None) -> None:
    """The sqlite3 shell on the file of the base: reading is free, a statement that writes is not (`attach`, `.import`, `.restore`, `-init`)."""
    texts = [w.text for w in args]
    blob = "\n".join(texts) + "\n" + (stdin if stdin and stdin != "\0DECODER" else "")
    if not DB_NAMED_RE.search(" " + blob):
        return
    if SQL_WRITE.search(blob) or SQL_DOT_WRITE.search(blob) or any(x in ("-init",) for x in texts) or name == "sqlite-utils" and any(
            x.lower() in ("insert", "upsert", "update", "delete", "drop", "create-table", "query") and x.lower() != "query" for x in texts):
        raise Block("hokma_internal", "hokma.db")


def brace_variants(text: str, limit: int = 64, ctx: Ctx | None = None) -> list[str]:
    """The words a shell builds from the braces of `text` (`{a,b}`), to judge each one; empty when there are no braces. Ranges (`{1..3}`) are not read.
    When the expansion passes `limit` the rest cannot be judged: with a `ctx`, a text that touches the data folder or names a file of keys then fails closed
    (secrets); any other text is not a reason to refuse."""
    out = [text]
    while True:
        nxt, changed = [], False
        for item in out:
            m = re.search(r"\{([^{}]*,[^{}]*)\}", item)
            if m:
                changed = True
                nxt += [item[:m.start()] + alt + item[m.end():] for alt in m.group(1).split(",")]
            else:
                nxt.append(item)
        if not changed:
            break
        if len(nxt) > limit:
            if ctx is not None and touches_keys(text, ctx):
                raise Block("secrets", short(text))
            out = nxt[:limit]
            break
        out = nxt
    return out if out != [text] else []


def touches_keys(text: str, ctx: Ctx) -> bool:
    """The text names the data folder or a file of keys (by its default places, its real folder or the name of the file)."""
    low = norm_text(expand(text, ctx))
    return names_data_folder(low, ctx) or any(k in low for k in KEY_COMPONENTS)


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
        for text in (expand(x, ctx) for x in (w.text, *brace_variants(w.text, ctx=ctx))):
            if unresolved(text) and not text.startswith("~"):
                continue                                         # built at run time: cannot be judged, and it is not a reason to refuse
            full = clean_path(joined(text, run.here))
            if is_win_abs(full) and os.name != "nt":
                continue                                         # a Windows path on another system: text markers only
            if _contains_data(full, ctx) and (
                    not excluded or _fold(os.path.realpath(full)) == _fold(os.path.realpath(ctx.data))):
                raise Block("secrets", short(w.text))


def scan_words(name: str, words: list[Word], cmd: Cmd, run: Run) -> None:
    """Markers of secrets anywhere in the arguments, plus the host list of brokers."""
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
        content = name in CONTENT_READERS
        paths = [x for x in words[1:] if not x.text.startswith("-")]
        for w in scan:
            for form in (w.text, *brace_variants(w.text, ctx=ctx)):          # `{a,b}` is read as the shell reads it: each word it builds is judged
                if form is not w.text:
                    check_text_markers(form, ctx)
                if dynamic_key_path(form, ctx):
                    raise Block("secrets", short(w.text.replace("\0", "$(...)")))      # a piece decided at run time in front of a file of keys
                if run.reading and w is not words[0]:
                    if form.startswith("-"):
                        for value in _option_values(form):
                            check_read_path(value, ctx, run.here, content=content)
                    if not form.startswith("-") and name not in CD_NAMES:
                        check_read_path(form, ctx, run.here, content=content)
                    if name in SEARCHERS and len(paths) <= 1:
                        check_read_path(run.here, ctx, run.here, content=True)   # `grep -r x` with no path searches here
                    elif name in CD_NAMES and not form.startswith("-"):
                        check_read_path(form, ctx, run.here, content=False)
                # Judged from the TRACKED folder (`cd x && cat y`) and by real path (links), also for a bare name.
                if any(ch in form for ch in "/\\") or form.startswith(("~", ".", "$", "%")) or (
                        w is not words[0] and not form.startswith("-") and not run.reading):
                    check_real_path(form, ctx, here=run.here)
                if form.startswith("-") and not run.reading:
                    for value in _option_values(form):
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


# ------------------------------------------------------------------------------------------------ own files (1.3.3)
# When the hook allows a call that creates a path that did not exist (Write, Edit, MultiEdit, NotebookEdit, a redirection, tee, touch, cp, mv, mkdir),
# the real path goes to <data>/guard/created/<session_id>.jsonl. Only this hook writes there (<data>/guard is protected against the agents). The
# delete rule lets a delete through ONLY when every path it names is in the list of the same session; anything else (an unknown path, a pattern that
# matches something else, a folder with something inside that is not in the list, a path built at run time) is refused as before. The protected places
# (data folder, guard, hooks, package, runtime) are refused even when they are in the list.
CREATED_DIR = "created"
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
OWN_DELETE = {"rm", "rmdir", "unlink", "del", "erase", "rd", "remove-item", "ri"}


def session_of(payload: dict, ctx: Ctx) -> str:
    for cand in (payload.get("session_id"), ctx.conversation_id):
        if isinstance(cand, str) and SESSION_RE.match(cand):
            return cand
    return ""


def own_key(path: str) -> str:
    """The entry itself (a link is not followed): the real path of its folder plus its name."""
    path = os.path.normpath(path)
    return _fold(os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path)))


def own_map(ctx: Ctx) -> dict:
    """The paths this session created before this call, each with the identity (st_dev, st_ino) of what was created.

    The identity is taken the first time the hook sees the path on disk (the next call after the one that created it), so a file moved or
    copied over it later (mv, ln -sf) is another file and is not this session's. A line {"p": path, "x": 1} strikes the path out."""
    if ctx._own is None:
        found: dict = {}
        if ctx.session:
            fname = os.path.join(ctx.data, "guard", CREATED_DIR, ctx.session + ".jsonl")
            try:
                with open(fname, encoding="utf-8") as f:
                    for line in f:
                        try:
                            item = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(item, dict) or not isinstance(item.get("p"), str):
                            continue
                        ident = item.get("i")
                        if item.get("x"):
                            found.pop(item["p"], None)
                        else:
                            found[item["p"]] = tuple(ident) if isinstance(ident, list) and len(ident) == 2 else None
            except OSError:
                pass
            stamps = ""
            for key, ident in found.items():
                if ident is None and os.path.lexists(key):
                    try:
                        st = os.lstat(key)
                    except OSError:
                        continue
                    found[key] = (st.st_dev, st.st_ino)
                    stamps += json.dumps({"p": key, "i": [st.st_dev, st.st_ino]}, ensure_ascii=False) + "\n"
            if stamps:
                try:
                    fd = os.open(fname, os.O_WRONLY | os.O_APPEND)
                    try:
                        os.write(fd, stamps.encode("utf-8"))
                    finally:
                        os.close(fd)
                except OSError:
                    pass
        ctx._own = found
    return ctx._own


def own_set(ctx: Ctx) -> set[str]:
    """The paths of the session (see own_map) plus the ones this same call creates."""
    return set(own_map(ctx)) | ctx.pending


def same_entry(key: str, ctx: Ctx) -> bool:
    """True when what is on disk at key is the very file or folder the session created (same device and inode); a path this same call
    creates has nothing on disk yet."""
    if key in ctx.pending:
        return True
    ident = own_map(ctx).get(key)
    if ident is None:
        return not os.path.lexists(key)
    try:
        st = os.lstat(key)
    except OSError:
        return True                                             # already gone
    return (st.st_dev, st.st_ino) == tuple(ident)


def note_create(ctx: Ctx, text: str, here: str) -> None:
    """A call is about to create text. When it does not exist yet, it and every missing folder above it are this session's."""
    if not ctx.session or not text or text == "-" or is_device(text):
        return
    exp = expand(text, ctx)
    if unresolved(exp) or any(ch in exp for ch in "*?[{}"):
        return
    full = clean_path(joined(exp, here))
    if is_device(full) or (is_win_abs(full) and os.name != "nt"):
        return
    chain: list[str] = []
    cur = full
    while cur and not os.path.lexists(cur) and len(chain) < 64:
        chain.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    for item in chain:
        ctx.pending.add(own_key(item))


def commit_created(ctx: Ctx) -> None:
    """The call was allowed: its new paths join the list of the session. Never fails the call."""
    if not ctx.session or not (ctx.pending or ctx.gone):
        return
    try:
        folder = os.path.join(ctx.data, "guard", CREATED_DIR)
        os.makedirs(folder, mode=0o700, exist_ok=True)
        data = "".join(json.dumps({"p": k}, ensure_ascii=False) + "\n" for k in sorted(ctx.pending - ctx.gone)).encode("utf-8")
        data += "".join(json.dumps({"p": k, "x": 1}, ensure_ascii=False) + "\n" for k in sorted(ctx.gone)).encode("utf-8")
        fd = os.open(os.path.join(folder, ctx.session + ".jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        if ctx._own is not None:
            for k in ctx.pending - ctx.gone:
                ctx._own[k] = None
            for k in ctx.gone:
                ctx._own.pop(k, None)
    except OSError:
        pass
    ctx.pending, ctx.gone = set(), set()


def own_entry(entry: str, ctx: Ctx, here: str, text: str) -> bool:
    """True when entry (a real, unprotected path) and everything inside it, if it is a folder, was created by this session."""
    if integrity_target(text, ctx, here) or SECRET_RE.search(norm_text(entry)):
        return False
    if under_any(entry, [ctx.data, *ctx.protect]):
        return False
    own = own_set(ctx)
    key = own_key(entry)
    if key in ctx.links or "?" in ctx.links:
        return False                                            # a link made by this same line: nothing is deleted through it
    if key not in own or not same_entry(key, ctx):
        return False                                            # not listed, or another file now sits at the path (mv, cp -f, ln -sf over it)
    if text.rstrip().endswith(("/", "\\")) and os.path.islink(entry):
        return False                                            # never delete through a link
    if not os.path.lexists(entry) or os.path.islink(entry) or os.path.isfile(entry):
        ctx.gone.add(key)
        return True                                             # a path of the session that is already gone, or one file
    if not os.path.isdir(entry):
        return False
    inside = [key]
    for top, dirs, files in os.walk(entry, followlinks=False):
        for name in (*dirs, *files):
            if os.path.islink(os.path.join(top, name)):
                return False                                    # a link inside a folder: nothing is deleted with it
            k = own_key(os.path.join(top, name))
            if k not in own or not same_entry(k, ctx):
                return False
            inside.append(k)
    ctx.gone.update(inside)
    return True


def own_path(text: str, ctx: Ctx, here: str, mode: str, wildcard: bool = False) -> bool:
    """True when what text names (a pattern is read against the disk) is only this session's work."""
    exp = expand(text, ctx)
    if not text or unresolved(exp) or any(ch in exp for ch in "{}"):
        return False
    full = clean_path(joined(exp, here))
    if is_win_abs(full) and os.name != "nt":
        return False
    if any(ch in exp for ch in GLOB_CHARS) and (wildcard or mode == "ps"):
        found = sorted(glob.glob(glob.escape(os.path.dirname(full)) + os.sep + os.path.basename(full)))
        return bool(found) and all(own_entry(f, ctx, here, f) for f in found)
    return own_entry(full, ctx, here, text)


_RM_SHORT = set("fFrRvdiI")
_RM_LONG = {"--force", "--recursive", "--verbose", "--dir", "--interactive", "--interactive=never", "--interactive=always"}
_PS_FLAGS = {"force", "recurse", "r", "verbose", "whatif", "confirm"}
_PS_VALUES = {"erroraction", "ea"}
_PS_PATHS = {"path", "literalpath"}


def delete_is_own(name: str, args: list["Word"], mode: str, run: "Run") -> bool:
    """rm, unlink, del, Remove-Item and their kin, when every path they name is this session's work."""
    ctx = run.ctx
    if not ctx.session or not run.pure_delete or name not in OWN_DELETE:
        return False
    targets: list = []
    i, end = 0, False
    while i < len(args):
        w = args[i]
        x = w.text
        i += 1
        if end or (not x.startswith("-") and not (mode == "cmd" and re.match(r"^/[A-Za-z]$", x))) or x == "-":
            targets.append(w)
        elif x == "--":
            end = True
        elif mode == "cmd":
            if not re.match(r"^/[A-Za-z]$", x) or x[1].lower() not in "sqf":
                return False
        elif mode == "ps":
            key, _, val = x.lstrip("-").partition(":")
            key = key.lower()
            if key in _PS_FLAGS:
                continue
            if key in _PS_VALUES or key in _PS_PATHS:
                if not val:
                    if i >= len(args):
                        return False
                    val, i = args[i].text, i + 1
                if key in _PS_PATHS:
                    if "," in val:
                        return False
                    targets.append(Word(val, False, w.glob, w.quoted))
                continue
            return False
        elif x.startswith("--"):
            if x not in _RM_LONG:
                return False
        elif not set(x[1:]) <= _RM_SHORT:
            return False
    if not targets:
        return False
    recursive = any(re.match(r"^-[a-zA-Z]*[rR]", w.text) or w.text in ("--recursive", "-Recurse", "/s", "/S") or w.text.lower() in ("-recurse", "-r")
                    for w in args if w.text.startswith(("-", "/")))
    for w in targets:
        if w.dyn or "\0" in w.text or not own_path(w.text, ctx, run.here, mode, w.glob):
            return False
        if ctx.links and (recursive or w.text.rstrip().endswith(("/", "\\"))):
            exp = expand(w.text, ctx)
            if not os.path.lexists(clean_path(joined(exp, run.here))):
                return False                                    # a folder that only exists in this line, with a link made in it
    return True


MOVE_NAMES = {"mv", "move", "move-item", "mi"}
RENAME_NAMES = {"ren", "rename", "rename-item", "rni"}
PY_MOVE = re.compile(r"""\bos\s*\.\s*(?:replace|rename|renames)\s*\(|\bshutil\s*\.\s*move\s*\(|\bfrom\s+os\s+import\b[^\n]*\b(?:replace|rename|renames)\b|"""
                     r"""\bfrom\s+shutil\s+import\b[^\n]*\bmove\b""")
PY_MOVE_LIT = re.compile(r"""\b(?:os\s*\.\s*(?:replace|rename)|shutil\s*\.\s*move)\s*\(\s*('(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*")\s*,\s*('(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*")\s*\)""")


def listed_target(path: str, ctx: Ctx, here: str, mode: str) -> str | None:
    """The key of the path when it names something of the session's list (or a path this call creates); None when it does not.
    "?" when the path cannot be known (built at run time or a pattern) and the session has a list to protect."""
    exp = expand(path, ctx)
    if not path or unresolved(exp) or any(ch in exp for ch in "*?[{}"):
        return "?" if own_set(ctx) else None
    full = clean_path(joined(exp, here))
    if is_win_abs(full) and os.name != "nt":
        return "?" if own_set(ctx) else None
    key = own_key(full)
    return key if key in own_set(ctx) else None


def source_is_own(path: str, ctx: Ctx, here: str) -> bool:
    exp = expand(path, ctx)
    if not path or unresolved(exp) or any(ch in exp for ch in "*?[{}"):
        return False
    key = own_key(clean_path(joined(exp, here)))
    return key in own_set(ctx) and same_entry(key, ctx)


def check_replace(pairs: list[tuple[str, str]], ctx: Ctx, here: str, mode: str, shown: str) -> None:
    """Each pair is (source, destination) of a move or a rename. A destination that is on the session's list may only be replaced by a
    source that is on the list with the same identity: otherwise the next delete of that path would remove what the agent put there."""
    for src, dest in pairs:
        target = listed_target(dest, ctx, here, mode)
        if target is None:
            continue
        if source_is_own(src, ctx, here):
            continue
        raise Block("delete", shown)


LINK_TYPES = ("symboliclink", "junction", "hardlink")


def note_links(name: str, args: list["Word"], mode: str, run: "Run") -> None:
    """ln, mklink and New-Item -ItemType SymbolicLink|Junction|HardLink: the link name is noted so the same line cannot delete through it."""
    ctx = run.ctx
    if not ctx.session:
        return
    texts = [w.text for w in args]
    link = ""
    if name == "ln":
        operands = [x for x in texts if not x.startswith("-") or x == "-"]
        if operands:
            link = operands[-1] if len(operands) > 1 else os.path.basename(operands[0].rstrip("/\\"))
    elif name == "mklink":
        operands = [x for x in texts if not re.match(r"^/[A-Za-z]$", x)]
        link = operands[0] if operands else ""
    elif name in ("new-item", "ni"):
        low = " ".join(x.lower() for x in texts)
        if not any(t in low for t in LINK_TYPES):
            return
        link = "?"
        for i, x in enumerate(texts):
            if x.lower().lstrip("-").split(":")[0] in ("path", "name") and i + 1 < len(texts) and ":" not in x:
                link = texts[i + 1]
                break
        else:
            pos = [x for x in texts if not x.startswith("-")]
            link = pos[0] if pos else "?"
    else:
        return
    exp = expand(link, ctx)
    if not link or unresolved(exp) or any(ch in exp for ch in "*?[{}"):
        ctx.links.add("?")
        return
    ctx.links.add(own_key(clean_path(joined(exp, run.here))))


def guard_replace(name: str, args: list["Word"], mode: str, run: "Run", shown: str) -> None:
    """mv, ln -f, Move-Item, ren and their kin whose destination is a path of the session's list (1.3.3, second round)."""
    ctx = run.ctx
    if not ctx.session:
        return
    if name == "ln":
        operands = [w for w in args if not w.text.startswith("-") or w.text == "-"]
        flags = "".join(w.text for w in args if w.text.startswith("-") and not w.text.startswith("--")) + " ".join(
            w.text for w in args if w.text.startswith("--"))
        if len(operands) >= 2 and ("f" in flags or "--force" in flags or "--remove-destination" in flags):
            dest = operands[-1]
            if dest.dyn or listed_target(dest.text, ctx, run.here, mode) is not None:
                raise Block("delete", shown)
        return
    if name not in MOVE_NAMES and name not in RENAME_NAMES:
        return
    pos: list = []
    named: dict[str, str] = {}
    target_dir = ""
    i = 0
    while i < len(args):
        x = args[i].text
        i += 1
        low = x.lower()
        if mode == "ps" and x.startswith("-") and len(x) > 1 and not x[1].isdigit():
            key, _, val = low.lstrip("-").partition(":")
            if key in ("path", "literalpath", "destination", "newname"):
                if not val and i < len(args):
                    val, i = args[i].text, i + 1
                named["path" if key == "literalpath" else key] = val
            elif key in ("erroraction", "ea", "filter", "include", "exclude") and not val:
                i += 1
            continue
        if x in ("-t", "--target-directory") and i < len(args):
            target_dir, i = args[i].text, i + 1
        elif x.startswith("--target-directory="):
            target_dir = x.split("=", 1)[1]
        elif x.startswith("-") and x != "-" and mode != "cmd":
            continue
        elif mode == "cmd" and re.match(r"^/[A-Za-z]$", x):
            continue
        else:
            pos.append(x)
    srcs: list[str] = []
    dest = ""
    if name in RENAME_NAMES:
        src = named.get("path") or (pos[0] if pos else "")
        new = named.get("newname") or (pos[1] if len(pos) > 1 else "")
        if src and new:
            dest = os.path.join(os.path.dirname(src), new)
            srcs = [src]
    elif target_dir:
        srcs, dest = ([*pos] + ([named["path"]] if "path" in named else [])), target_dir
    else:
        if "path" in named:
            srcs = [named["path"]] + ([] if "destination" in named else pos[:-1])
            dest = named.get("destination") or (pos[-1] if pos else "")
        elif len(pos) >= 2:
            srcs, dest = pos[:-1], pos[-1]
            if "destination" in named:
                srcs, dest = pos, named["destination"]
    if not srcs or not dest:
        return
    exp = expand(dest, ctx)
    full = clean_path(joined(exp, run.here)) if not unresolved(exp) else ""
    pairs: list[tuple[str, str]] = []
    for src in srcs:
        into = os.path.join(dest, os.path.basename(src.rstrip("/\\"))) if (target_dir or (full and os.path.isdir(full))) else dest
        pairs.append((src, into))
    check_replace(pairs, ctx, run.here, mode, shown)


def check_python_replace(code: str, run: "Run") -> None:
    """os.replace, os.rename and shutil.move: with two literals the same rule as mv; with anything else, refused when the session has a list."""
    ctx = run.ctx
    if not ctx.session or not PY_MOVE.search(code):
        return
    lits = list(PY_MOVE_LIT.finditer(code))
    rest = PY_MOVE_LIT.sub(" ", code)
    if PY_MOVE.search(rest):
        if own_set(ctx):
            raise Block("delete", short(PY_MOVE.search(rest).group(0)))
        return
    pairs = []
    for m in lits:
        try:
            pairs.append((ast.literal_eval(m.group(1)), ast.literal_eval(m.group(2))))
        except (ValueError, SyntaxError):
            raise Block("delete", short(m.group(0)))
    if re.search(r"chdir", code) and own_set(ctx):
        raise Block("delete", "chdir")
    check_replace(pairs, ctx, run.here, "bash", short(code))


def note_creates(name: str, args: list["Word"], run: "Run") -> None:
    """The commands that create a file or a folder: what they create is noted, to be written down when the whole call is allowed."""
    ctx = run.ctx
    if not ctx.session:
        return
    texts = [w.text for w in args]
    operands = [w for w in args if not w.text.startswith("-") or w.text == "-"]
    skip = {"touch": ("-t", "-d", "-r", "--date", "--reference", "--time"), "mkdir": ("-m", "--mode", "-Z", "--context"),
            "cp": ("-t", "--target-directory"), "mv": ("-t", "--target-directory")}.get(name, ())
    if any(x in skip or any(x.startswith(s + "=") for s in skip if s.startswith("--")) for x in texts):
        return
    if name in ("tee", "touch", "mkdir"):
        for w in operands:
            note_create(ctx, w.text, run.here)
    elif name in ("cp", "mv", "copy", "move") and len(operands) >= 2:
        *sources, dest = operands
        if any(w.glob or w.dyn for w in sources) or dest.dyn:
            return
        dest_exp = expand(dest.text, ctx)
        dest_full = clean_path(joined(dest_exp, run.here)) if not unresolved(dest_exp) else ""
        moves = name in ("mv", "move")
        own = own_set(ctx) if moves else set()
        for w in sources:
            if moves:
                src_exp = expand(w.text, ctx)
                if unresolved(src_exp) or own_key(clean_path(joined(src_exp, run.here))) not in own:
                    continue                                    # a moved file keeps what it was: only the agent's own stays the agent's
            if dest_full and os.path.isdir(dest_full):
                note_create(ctx, os.path.join(dest_full, os.path.basename(w.text.rstrip("/\\"))), run.here)
            elif len(sources) == 1:
                note_create(ctx, dest.text, run.here)


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
        if delete_is_own(name, args, mode, run):
            return                                              # only what this same chat session created (1.3.3)
        raise Block("delete", shown)

    note_links(name, args, mode, run)
    guard_replace(name, args, mode, run, shown)
    integrity_writes(name, words, args, run)
    note_creates(name, args, run)
    if name == "tee" and stdin and stdin != "\0DECODER" and any(x.lower().endswith(PYTHON_FILES) for x in texts):
        check_sensitive_import(stdin)
    if name in ("sqlite3", "sqlite", "litecli", "sqlite-utils"):
        check_sqlite(name, args, stdin)
    if re.match(r"^pip[\d.]*$", name) or name in ("ensurepip", "easy_install", "uv"):
        check_pip(name, words, args, run)

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
                check_write_dest(w.text[3:], ctx, run.here)
        return


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
        check_python_gates(words, args, run)
        run_interpreter("py", args, cmd, run, depth, stdin)
        return
    if name in NODE_NAMES:
        run_interpreter("node", args, cmd, run, depth, stdin)
        return
    if name in ("ruby", "perl"):
        run_interpreter(name, args, cmd, run, depth, stdin)
        return

    if name in RUNNERS:                                        # the command after `run` or `exec`, after its own options
        rest = args[[x.lower() for x in texts].index("run") + 1:] if "run" in low else args[low.index("exec") + 1:] \
            if "exec" in low else args if name in ("uvx", "pipx") else []
        for k in range(len(rest)):
            if not rest[k].text.startswith("-"):
                sub = Cmd()
                sub.words, sub.redirs, sub.heredocs, sub.herestrs, sub.pipe_in = rest[k:], cmd.redirs, cmd.heredocs, \
                    cmd.herestrs, cmd.pipe_in
                analyze_cmd_words(sub, prev, mode, run, depth, stdin)

    # scripts run directly: ./do.sh, ./tool.py, script.ps1
    first = words[0]
    if (any(ch in first.text for ch in "/\\") or first.text.startswith(".")) and not unresolved(first.text):
        script_file(first.text, cmd, run, depth)



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
        check_python_replace(code, run)
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
    if ctx.host_re and ctx.host_re.search(code) or ctx.url_re and ctx.url_re.search(code):
        raise Block("real_money", "broker host")
    check_sensitive_import(code)
    check_code_integrity(code, ctx)


# --- the options of the python interpreter, read the way CPython reads them (getopt: short options combine in a group)
_PY_SHORT_VALUE = "WXQ"                                        # short options that take a value (glued to the group or the next word)
_PY_LONG_VALUE = ("--check-hash-based-pycs",)                  # long options that take the next word as their value
_PY_LAUNCHER = re.compile(r"^-(?:V:\S*|\d[\w.\-]*)$")           # `py -3`, `py -3.12`, `py -3-64`, `py -V:3.12`: options of the launcher


def parse_python_args(texts: list[str]) -> tuple[str, str | None, int]:
    """What a python command line runs: `("m", module, index)`, `("c", code, index)`, `("script", path, index)`,
    `("stdin", None, index)` or `("none", None, index)`; `index` is where the module, the code or the script word sits.
    A short group may combine flags (`-Im`, `-IBm`, `-Ic`, `-OOm`, `-Ssm`, `-uc`, `-Xdev`, `-Wdefault`): the group is read
    character by character; `c` and `m` take what is glued after them in the group or else the next word; `W`, `X` and `Q`
    swallow the rest of the group or else the next word. `-` is stdin, `--` ends the options, `--check-hash-based-pycs` takes a value."""
    i = 0
    while i < len(texts):
        a = texts[i]
        if a == "-":
            return "stdin", None, i
        if a == "--":
            return ("script", texts[i + 1], i + 1) if i + 1 < len(texts) else ("none", None, i)
        if not a.startswith("-"):
            return "script", a, i
        if _PY_LAUNCHER.match(a):
            i += 1
            continue
        if a.startswith("--"):
            i += 2 if a in _PY_LONG_VALUE else 1
            continue
        step = 1
        for j in range(1, len(a)):
            ch = a[j]
            if ch in "cm":
                glued = a[j + 1:]
                if glued:
                    return ch, glued, i
                return ch, (texts[i + 1] if i + 1 < len(texts) else ""), i + 1
            if ch in _PY_SHORT_VALUE:
                step = 1 if j + 1 < len(a) else 2
                break
        i += step
    return "none", None, len(texts)


RUNNERS = {"uv", "uvx", "pipx", "poetry", "pipenv", "pdm", "hatch", "rye", "conda", "mamba", "micromamba"}      # `uv run hokma engine`
def run_interpreter(kind: str, args: list[Word], cmd: Cmd, run: Run, depth: int, stdin: str | None) -> None:
    code, script = None, None
    i = 0
    if kind == "py":
        how, value, at = parse_python_args([w.text for w in args])
        if how == "c":
            code = value
        elif how == "script":
            script = value
        i = len(args)                                                              # the generic scan below is for the other languages
    while i < len(args):
        a = args[i].text
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



# ------------------------------------------------------------------------------------------------ the tools

FILE_TOOLS = guard.FILE_TOOLS
READ_TOOLS = {"Read", "Grep", "Glob"}
SHELL_TOOLS = {"Bash": "bash", "PowerShell": "ps"}
URL_TOOLS = {"WebFetch"}                                        # tools that take a URL: judged against the broker hosts
# An order verb next to `order`, in either order, with or without a separator, in any case: `place_order`, `order_place`,
# `placeOrder`, `orderPlace`, `ORDER-REPLACE`, `order_preview`, `order_amend`. An order book is data, not an order.
ORDER_VERBS = r"(?:place|submit|send|create|modify|replace|cancel|amend|preview)"
ORDER_TOOL = re.compile(ORDER_VERBS + r"[_-]?order(?!s?[_-]?book)|order[_-]?" + ORDER_VERBS, re.I)


def is_hokma_order_tool(tool: str, brokers: dict | None = None) -> bool:
    """True for the four exact MCP names of Hokma's own server in `hokma_order_tools` (SPEC-015 18.7): `evaluate` and the
    permission broker skip the ORDER_TOOL name pattern for them (the host and secret checks still run). The match is
    exact: `mcp__hokma__place_order`, the same tool of another server or a different case are not on the list."""
    names = (brokers if brokers is not None else load_brokers()).get("hokma_order_tools", [])
    return isinstance(tool, str) and isinstance(names, list) and tool in names


def check_file_tool(tool: str, tool_input: dict, ctx: Ctx, cwd: str) -> None:
    """Edit, Write and NotebookEdit: a store of keys is not written, by its text or by where it really leads. The hook files are `guard`'s;
    where else the file goes is the owner's business."""
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    check_write_dest(path, ctx, cwd)
    if isinstance(path, str) and path.lower().endswith(PYTHON_FILES):            # a Python file that imports the broker is the same code as one that runs it
        edits = tool_input.get("edits") if isinstance(tool_input.get("edits"), list) else []
        for text in (tool_input.get("content"), tool_input.get("new_string"), tool_input.get("new_source"),
                     *[e.get("new_string") for e in edits if isinstance(e, dict)]):
            if isinstance(text, str):
                check_sensitive_import(text)
    if isinstance(path, str):
        note_create(ctx, path, cwd)
        if ctx.session and os.path.lexists(path):                # an edit may replace the file: the next call takes its identity again
            key = own_key(clean_path(joined(expand(path, ctx), cwd)))
            if key in own_map(ctx):
                ctx.pending.add(key)


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
            check_text_markers(item, ctx)
            check_real_path(item, ctx)
            if tool != "Grep" or content:
                check_read_path(item, ctx, cwd, content=content)


# Commands that only read. A command line is read-only only when EVERY command in it is one of these; such a line is
# judged strictly where it touches Hokma's data folder.
LIST_COMMANDS = {"ls", "dir", "stat", "du", "pwd", "get-childitem", "gci", "get-item", "gi", "test-path",
                 "get-location", "realpath", "readlink", "basename", "dirname", "which", "where", "find", "echo", "printf",
                 "true", "false", "write-output", "write-host"}
CONTENT_READERS = {"cat", "head", "tail", "grep", "egrep", "fgrep", "rg", "type", "get-content", "gc", "select-string",
                   "sls", "strings", "cmp", "md5sum", "sha1sum", "sha256sum", "wc", "cut", "nl", "tac",
                   "get-filehash", "diff"}
LISTERS = {"ls", "dir", "get-childitem", "gci", "get-item", "gi", "find"}
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
    if under_any(cwd, [ctx.data]) or names_data_folder(raw, ctx):
        if lists_into_reader(command, mode):
            raise Block("secrets", short(command))             # `Get-ChildItem <folder> | Get-Content` reads what it lists
        if only_reads(command, mode):
            run.reading = True                                 # read-only: Hokma's folders may be read, not their secrets
    analyze_text(command, mode, run, 0)


def check_url_tool(url: object, ctx: Ctx) -> None:
    """A URL handed to a fetch tool: a host that places real orders, or a URL shape that does, is refused the same way as
    in a command. There is no exception here: `read_exceptions` is for Hokma's backend only."""
    text = url if isinstance(url, str) else ""
    if ctx.host_re and ctx.host_re.search(text) or ctx.url_re and ctx.url_re.search(text):
        raise Block("real_money", short(text))


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
    ctx.session, ctx.pending, ctx.gone, ctx._own, ctx.links = session_of(payload, ctx), set(), set(), None, set()
    own_map(ctx)                                                # takes the identity of what the session created in earlier calls, before anything can replace it
    try:
        if tool.startswith("mcp__"):
            if ORDER_TOOL.search(tool) and not is_hokma_order_tool(tool, ctx.brokers):
                raise Block("real_money", tool)
        elif tool in FILE_TOOLS:
            check_file_tool(tool, tool_input, ctx, cwd)
        elif tool in READ_TOOLS:
            check_read_tool(tool, tool_input, ctx, cwd)
        elif tool in SHELL_TOOLS:
            check_command(tool, tool_input.get("command"), cwd, ctx)
        elif tool in URL_TOOLS:
            check_url_tool(tool_input.get("url"), ctx)
    except Block as block:
        return block
    commit_created(ctx)
    return None


# ------------------------------------------------------------------------------------------------ the hook

MESSAGES = {
    "delete": lambda x: (t("hardblock.delete", target=x), t("hardblock.hint.delete")),
    "secrets": lambda x: (t("hardblock.secrets", target=x), t("hardblock.hint.secrets")),
    "real_money": lambda x: (t("hardblock.real_money", target=x), t("hardblock.hint.real_money")),
    "hokma_internal": lambda x: (t("hardblock.hokma_internal", target=x), t("hardblock.hint.hokma_internal")),
}


def log_block(ctx: Ctx, tool: str, block: Block) -> None:
    """One JSON line in `<data>/guard/blocks.jsonl`. Never fails the block: a log problem is ignored."""
    try:
        folder = os.path.join(ctx.data, "guard")
        os.makedirs(folder, exist_ok=True)
        line = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "rule": block.rule, "tool": tool,
                "target": short(block.target), "conversation_id": ctx.conversation_id}
        if block.reason:
            line["reason"] = block.reason
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
    guard.utf8_stdio()
    try:
        args = sys.argv[1:] if argv is None else argv
        ctx = build_ctx(args)
        payload = json.loads(guard.read_stdin_text())
        block = evaluate(payload, ctx)
        if block is None:
            if payload.get("tool_name") in ALWAYS_ASK:      # Claude Code in auto mode: this call still needs the owner
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask"}}))
            return 0
        log_block(ctx, str(payload.get("tool_name", "")), block)
        print(f"HOKMA_BLOCKED {block.rule}", file=sys.stderr)
        reason, hint = MESSAGES[block.reason if block.reason in MESSAGES else block.rule](short(block.target))
        print(reason, file=sys.stderr)
        print(hint, file=sys.stderr)
        return BLOCK
    except BaseException as exc:  # fail closed on anything unexpected
        try:
            print("HOKMA_BLOCKED error", file=sys.stderr)
            print(t("hardblock.error", error=type(exc).__name__), file=sys.stderr)
            print(f"{type(exc).__name__}: {exc}"[:500], file=sys.stderr)     # the real cause, for the log and the chat
        except BaseException:      # noqa: BLE001  printing must never turn a block into an allow
            pass
        return BLOCK


if __name__ == "__main__":
    raise SystemExit(main())

