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
import urllib.parse
from datetime import datetime, timezone

from hokma.i18n import t
from hokma.kit import guard
from hokma.kit.guard import _fold, under_any
from hokma.permissions.modes import ALWAYS_ASK

BLOCK = 2
RULES = ("delete", "hokma_internal", "outside_workdir", "secrets", "real_money")
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


# --------------------------------------------------------------------- the broker service and its interpreter (SPEC-015 18.7)
# An agent never imports the order code of Hokma (`hokma.broker`, `hokma.schwab`, `hokma.connections`: they hold the
# Schwab session and send real orders) and never runs the interpreter of Hokma's own runtime (`<data>/runtime/<x>/venv`):
# it has them installed, so a script of the agent could place an order with the owner's keys. Both are `real_money`.
# 1.2.9: the lock names what touches real money, keys, the guard or the server, not the whole package. Everything else (hokma.factory, hokma.backtest, hokma.quant, hokma.data, rule_check...)
# can be imported and run: the strategy of an agent needs it. The runtime lock (core/processguard.py) keeps refusing these same packages in any process that is not Hokma.
HOKMA_MONEY_MODULES = ("broker", "schwab", "connections", "exec", "desks", "contracts", "backoffice", "lessons", "econ_calendar", "permissions", "kit", "core", "mail", "telegram",
                       "github_connect", "backup", "update", "setup", "scheduler", "rpc", "status")
_HOKMA_MODS = r"(?:" + "|".join(HOKMA_MONEY_MODULES) + r")"
_HOKMA_MODS_DB = r"(?:db|" + "|".join(HOKMA_MONEY_MODULES) + r")"            # the database module is named by import only: `hokma.db` as a text is the file of the base
HOKMA_MONEY_REASON = "hokma_money"
_HOKMA_DOT = r"\s*(?:\.|[/\\]+)\s*"                    # `hokma.broker`, `hokma/broker`, `hokma\\broker` (a path or a string)
HOKMA_MODULE_RE = re.compile(
    r"(?<![\w])hokma" + _HOKMA_DOT + _HOKMA_MODS + r"(?![\w])"            # import, -m, path, importlib, __import__
    r"|(?<![\w.])(?:import|from)(?:\s|\\)+hokma\.db(?![\w])"                       # the module of the database; the file hokma.db is judged by names_hokma_database
    r"|\bfrom\s+hokma\s+import\s+(?:\([^)]*?\b" + _HOKMA_MODS_DB + r"\b|[^\n;(]*?\b" + _HOKMA_MODS_DB + r"\b)"
    r"|[\"']hokma[\"']\s*[,/)]\s*(?:[\w.()\s]*[,/]\s*)?[\"']" + _HOKMA_MODS_DB + r"[\"']",   # join("hokma", "broker"), Path("hokma") / "broker"
    re.I)
_DYN_NAMES = (r"(?:import_module|__import__|run_module|run_path|find_spec|find_loader|load_module|load_source|load_dynamic|"
              r"resolve_name|get_loader|get_importer|"
              r"spec_from_file_location|spec_from_loader|SourceFileLoader|SourcelessFileLoader|ExtensionFileLoader)")
# A dynamic import that names hokma in its arguments, in any text (a command, a script, an MCP argument).
_HOKMA_DYN_CALL = re.compile(_DYN_NAMES + r"\s*\([^)\n]{0,300}?hokma" + _HOKMA_DOT + _HOKMA_MODS + r"(?![\w])", re.I)
_HOKMA_MOD_WORD = re.compile(r"\b" + _HOKMA_MODS + r"\b", re.I)
# A file inside `bin` or `Scripts` of a runtime venv of the data folder (the folder itself may be listed).
HOKMA_VENV_TEXT_RE = re.compile(r"(?:^|[/\s\"'=:;(])runtime/+[^/\s\"']+/+venv/+(?:bin|scripts)/+[^/\s\"']")


# The runtime mark that lets a process load the order modules (`hokma.core.processguard`): naming it is the same thing.
_PROCESSGUARD = re.compile(r"\bprocessguard\b", re.I)


def names_hokma_module(text: str) -> bool:
    """The text (a command, a script, code) imports the `hokma` package or names one of its order modules:
    `import hokma`, `import hokma.x`, `from hokma import x`, a dynamic import that names it, or the path or dotted name of
    `hokma.broker`, `hokma.schwab`, `hokma.connections` (any submodule counts: the agent has no reason to import Hokma
    at all). Third-party code with a similar name (`import schwab`, `import hokma_extras`) is not this. The one thing that
    stays free is `python -m hokma doctor` (see `run_interpreter`: it is not an import in the agent's own process)."""
    return bool(HOKMA_MODULE_RE.search(text) or _HOKMA_DYN_CALL.search(text) or _PROCESSGUARD.search(text))


def money_module_named(text: str) -> str:
    """What the block shows when the text names a module of money, keys, the guard or the server: the module as written."""
    found = HOKMA_MODULE_RE.search(text) or _HOKMA_DYN_CALL.search(text)
    return re.sub(r"\s+", "", found.group(0))[:80] if found else "processguard"


# --- the package folder and the runtime folder: the agent must not put them on its import path, nor run python from them
def package_roots(ctx: "Ctx") -> list[str]:
    """Folders from where `import hokma` would work: the folder that holds the `hokma` package of this very file and the
    runtime folders of the data folder (venvs with every dependency, the Schwab client included)."""
    roots: list[str] = []
    kit = os.path.dirname(os.path.abspath(__file__))
    pkg = os.path.dirname(kit)
    if os.path.basename(kit) == "kit" and os.path.basename(pkg) == "hokma":
        roots.append(os.path.dirname(pkg))
    roots.append(os.path.join(ctx.data, "runtime"))
    out: list[str] = []
    for r in roots:
        for v in (r, os.path.realpath(r)):
            if len(v) > 3 and v not in out:
                out.append(v)
    return out


_LIT = r"""(?:[rR]?"[^"\\\n]*"|[rR]?'[^'\\\n]*')"""          # a plain string literal: no escapes, no prefix but r
_PATH_INJECT = re.compile(r"sys\s*\.\s*path\s*(?:\.\s*(?:insert|append|extend|__setitem__|__iadd__|__setslice__)\s*\(|\+=|=(?!=)|"
                          r"\[[^\]\n]*\]\s*\+?=(?!=))|\bPYTHONPATH\b|"             # sys.path[0] = x, sys.path[:] = x, sys.path[1:1] = x
                          r"\baddsitedir\b|\bsite\s*\.\s*main\b|\.pth\b", re.I)
_PATH_TARGET = re.compile(r"site-packages|dist-packages|[/\\]runtime[/\\][^/\\\s\"']+[/\\]venv", re.I)
_PATH_FUZZY = re.compile(r"venv|runtime|hokma|site-|packages|backend|lib[/\\]python|\.local|appdata|share", re.I)
_QUOTED = re.compile(r"\"([^\"\n]{1,300})\"|'([^'\n]{1,300})'")


def _under_roots(path: str, roots: list[str], here: str) -> bool:
    if not path or "\0" in path:
        return False
    full = clean_path(joined(path, here))
    return any(under_any(c, roots) for c in dict.fromkeys((full, os.path.realpath(full))))


def names_hokma_path_injection(text: str, ctx: "Ctx", here: str | None = None) -> bool:
    """`sys.path.insert/append/extend`, `sys.path = `, `PYTHONPATH=`, `site.addsitedir`, a `.pth` file that points at a
    venv's `site-packages`, at the folder of the `hokma` package or at Hokma's runtime. A path the text builds at run time is
    judged by what else the text says (a venv, a runtime, a package folder). A student's own `sys.path.insert(0, 'src')` is free."""
    here = here or ctx.cwd
    roots = [r for r in package_roots(ctx) if norm_text(r).rstrip("/").endswith("/runtime")]      # 1.2.9: the folder of the package is no longer a target; the runtime with its keys' libraries is
    for m in _PATH_INJECT.finditer(text):
        seg = text[m.start(): m.start() + 400].split("\n", 1)[0]
        whole = m.group(0).lower().endswith(".pth")
        if _PATH_TARGET.search(seg) or any(norm_text(r) in norm_text(seg) for r in roots):
            return True
        for q in _QUOTED.finditer(seg):
            lit = q.group(1) if q.group(1) is not None else q.group(2)
            for piece in re.split(r"[:;]", lit):
                if piece and _under_roots(expand(piece, ctx), roots, here):
                    return True
        if whole:
            if _PATH_TARGET.search(text):
                return True
            continue
        rest = re.sub(_LIT, "", seg[len(m.group(0)):].split(")", 1)[0])
        if "PYTHONPATH" in m.group(0).upper():
            built = "$" in seg or "\x60" in seg
        else:
            built = bool(re.search(r"[A-Za-z_$\x60]", rest))
        if built and _PATH_FUZZY.search(text):
            return True
    return False


# --- Hokma's own database: no agent opens it, reads or writes (SPEC-015 18.7). The file name is the one of `hokma.db.paths`.
_DB_NAME = re.compile(r"hokma\.db(?:-wal|-shm|-journal)?(?![\w])", re.I)
_DB_ACCESS = re.compile(
    r"sqlite|\bconnect\s*\(|\bopen\s*\(|\bpsql\b|\bpg_(?:dump|dumpall|restore)\b|duckdb|apsw|\bshelve\b|\bdbm\b|create_engine|"
    r"read_sql|litecli|db[\s_-]?browser|\.(?:read_bytes|read_text|write_bytes|write_text)\s*\(|\bfileinput\b|>|\btee\b|"
    r"\bsed\s+-\w*i|\b(?:cp|mv|dd|ln|install|rsync|truncate|copy|move|xcopy|robocopy|copy-item|move-item|set-content|"
    r"add-content|out-file|new-item|tee-object|write|import)\b", re.I)
_DB_ENGINE = re.compile(r"sqlite|duckdb|apsw|litecli|db[\s_-]?browser|\bconnect\s*\(|\bopen\s*\(", re.I)
_DB_FILE_KIND = re.compile(r"\.db\b|\.sqlite3?\b|sqlite", re.I)
_DB_PG = re.compile(r"\bpsql\b|\bpg_(?:dump|dumpall|restore)\b|\bpgcli\b|\bpg_isready\b", re.I)
_DB_CONNECT_BUILT = re.compile(r"(?<![\w])(?:sqlite3\s*\.\s*connect|aiosqlite\s*\.\s*connect|apsw\s*\.\s*Connection|"
                               r"duckdb\s*\.\s*connect)\s*\((?!\s*" + _LIT + r"\s*[,)])", re.I)
_DB_OPEN_BUILT = re.compile(r"(?<![\w])open\s*\((?!\s*" + _LIT + r"\s*[,)])", re.I)
_DB_ENV_HINT = re.compile(r"environ|getenv|hokma|localappdata|appdata|\.local|share|expanduser|path\s*\.\s*home|\$env:", re.I)


def names_hokma_database(text: str, ctx: "Ctx") -> bool:
    """The text opens, connects to or writes Hokma's database. Reading it with `cat` and the like is `secrets` already (the
    file is among the data folder's secrets); this is the access by an engine or by `open()`, by name or built at run time."""
    low = norm_text(text)
    if _DB_NAME.search(low) and _DB_ACCESS.search(text):
        return True
    if names_data_folder(low, ctx) and _DB_ENGINE.search(text) and _DB_FILE_KIND.search(text):
        return True
    if _DB_PG.search(text) and re.search(r"hokma|database\.json", low):
        return True
    if _DB_CONNECT_BUILT.search(text) and _DB_ENV_HINT.search(text):
        return True
    return bool(_DB_OPEN_BUILT.search(text) and re.search(r"hokma", low))


# --- python that builds a name or code at run time (SPEC-015 18.7): judged by the way it is used, not by words
_DYN_NAME_RE = re.compile(r"(?<![\w])" + _DYN_NAMES + r"\b")
_FROM_IMPORT = re.compile(r"(?<![\w.])from[ \t]+([\w.]+)[ \t]+import[ \t]+(\([^)]*\)|[^\n;]*)")
_EXEC_RE = re.compile(r"(?<![\w.])(?:exec|eval|compile|execfile)\s*\(")
_BUILTINS_RE = re.compile(r"__builtins__|\bbuiltins\s*\.\s*(?:__dict__|__import__|exec|eval|compile)|"
                          r"\bgetattr\s*\(\s*(?:builtins|importlib|__import__|sys\b|runpy|imp\b)", re.I)
_SPAWN_BUILT = re.compile(r"[\"']-[mc][\"']\s*,(?!\s*" + _LIT + r"\s*[\],)])\s*\S|"                 # [py, "-c", code_built]
                          r"\bpython[\d.]*\s+-\w*c\s*[\"']?[^\n]*\{|"                                    # f"python -c '{x}'"
                          r"\bpython[\d.]*\s+-\w*c\s*[\"'][^\"'\n]*[\"']?\s*(?:\+|%|\.\s*(?:format|join))", re.I)
_FILE_IMPORTS = ("spec_from_file_location", "spec_from_loader", "sourcefileloader", "sourcelessfileloader",
                 "extensionfileloader", "run_path", "load_source", "load_dynamic")


def names_python_dynamic(code: str) -> str | None:
    """Why this python code is built or loaded at run time, or None. A dynamic import (`importlib.import_module`, `__import__`,
    `runpy`, `find_spec`, loaders by file) whose argument is not a plain string literal, a literal that names hokma, or
    `exec`/`eval`/`compile` of a string that is not a plain literal, is how a name is assembled from parts (concatenation, join,
    format, %, f-string, chr, bytes, base64, codecs, rot13). `import_module('json')` with a plain literal is free."""
    masked = code
    for m in _FROM_IMPORT.finditer(code):                      # `from importlib import import_module` is no use yet: the call is judged
        names = m.group(2)
        if re.search(_DYN_NAMES + r"\s+as\b", names) or ("*" in names and m.group(1).split(".")[0] in
                                                          ("importlib", "runpy", "builtins", "imp", "pkgutil")):
            return "aliased dynamic import"
        masked = masked[:m.start()] + " " * (m.end() - m.start()) + masked[m.end():]
    first = re.compile(r"\s*\(\s*(" + _LIT + r")\s*(?=[,)])")
    for m in _DYN_NAME_RE.finditer(masked):
        name = m.group(0).lower()
        if masked[max(0, m.start() - 4): m.start()].strip().endswith("def"):
            continue
        lit = first.match(masked, m.end())
        if lit is None:
            return "dynamic import: " + short(m.group(0))
        text = lit.group(1)
        body = text.lstrip("rR")[1:-1]
        if "hokma" in body.lower() or body.startswith("."):
            return "dynamic import of hokma"
        if name in _FILE_IMPORTS:
            args = re.match(r"\s*\(([^()\n]*)\)", masked[m.end():])
            pieces = [p.strip() for p in args.group(1).split(",")] if args else ["?"]
            ok = all(re.fullmatch(r"(?:\w+\s*=\s*)?(?:" + _LIT + r"|[\d.]+|None|True|False)", p) for p in pieces if p)
            if not ok or re.search(r"hokma|site-packages|runtime", args.group(1) if args else "", re.I):
                return "dynamic file import"
    for m in _EXEC_RE.finditer(masked):
        if masked[max(0, m.start() - 4): m.start()].strip().endswith("def"):
            continue
        if re.match(r"\s*\(\s*\)", masked[m.end() - 1:]):
            continue
        if first.match(masked, m.end() - 1) is None:
            return "exec of a built string"
    if _BUILTINS_RE.search(masked):
        return "builtins access"
    if _SPAWN_BUILT.search(masked):
        return "python launched with built code"
    return None


# --- more ways to reach Hokma's code from python without writing its name (SPEC-015 18.7, security review of 5280c87)
_LIT_PATH = r"""(?:[rR]'[^'\n]*'|[rR]"[^"\n]*"|'(?:[^'\\\n]|\\\\)*'|"(?:[^"\\\n]|\\\\)*")"""   # a path literal: raw, or with doubled backslashes
_CHDIR = re.compile(r"(?<![\w.])(?:(?:os|contextlib)\s*\.\s*)?f?chdir\s*\(")
_CHDIR_OWN_FILE = re.compile(r"(?:os\s*\.\s*path\s*\.\s*)?(?:dirname|abspath|realpath)\s*\(|(?:pathlib\s*\.\s*)?Path\s*\(|"
                             r"\.\s*(?:parent|resolve)\s*(?:\(\s*\))?|__file__|[\s().,]")
_PICKLE_LOAD = re.compile(r"(?<![\w])(?:c?pickle|_pickle|marshal|dill|cloudpickle|shelve)\s*\.\s*(?:loads?|Unpickler|open)\b|"
                          r"\bfrom\s+(?:c?pickle|_pickle|marshal|dill|cloudpickle|shelve)\s+import\s+[^\n]*\b(?:loads?|Unpickler|open)\b", re.I)
# What makes a load of a pickle one that can name Hokma's code: the word hokma, a runtime or site-packages path, or a
# payload that is decoded or assembled by hand (a hand made pickle names its module in bytes nobody can read as text).
_PICKLE_PAYLOAD = re.compile(r"hokma|site-packages|dist-packages|runtime[/\\][^/\\\s\"']+[/\\]venv|b(?:64|32)decode|a85decode|"
                             r"\bfromhex\b|unhexlify|a2b_|codecs\s*\.\s*(?:decode|encode)|(?:zlib|bz2|lzma|gzip)\s*\.\s*decompress|"
                             r"\bchr\s*\(|\bbyte(?:s|array)\s*\(|\\x[0-9a-f]{2}|\bb[\"']{2}\s*\.\s*join\s*\(", re.I)
_OPERATOR_CALLER = re.compile(r"(?<![\w.])(?:operator\s*\.\s*)?(?:attrgetter|methodcaller)\s*\(", re.I)
_MARSHAL = re.compile(r"(?<![\w.])(?:import\s+(?:[\w.]+(?:\s+as\s+\w+)?\s*,\s*)*|from\s+)marshal\b|(?<![\w])marshal\s*\.\s*loads?\b|"
                      r"[\"']marshal[\"']")          # marshal runs code when read and has no use in a student's script: always real money
_DYN_FUNC = r"(?:(?:builtins|__builtins__|importlib)\s*\.\s*)?(?:getattr|eval|exec|__import__|import_module)"
_DYN_FUNC_NAME = re.compile(r"^\s*(?:(?:builtins|__builtins__|importlib)\s*\.\s*)?(getattr|eval|exec|__import__|import_module)\s*$")
_PARTIAL_FORMS = ("partial", "partialmethod")
_HOKMA_ENTRY = re.compile(r"\bhokma_cli\b|\bengine_main\b|\btrash_mcp\b", re.I)     # the CLI and the two server entry points
_DANGEROUS_NAME = re.compile(r"importlib|__import__|import_module|\beval\b|\bexec\b|\bcompile\b|__builtins__|\bbuiltins\b|__globals__|"
                             r"__subclasses__|__loader__|__spec__|\brun_module\b|\brun_path\b|\bhokma\b", re.I)


def names_python_escape(code: str, ctx: "Ctx", here: str | None = None) -> str | None:
    """Python that reaches Hokma's order code without writing its name (SPEC-015 18.7), or None:
    - `os.chdir(x)` (also `contextlib.chdir`, `fchdir`) with an argument that is not a plain literal: the new current folder is
      what `import` searches next. A literal is free unless it points into Hokma's package, its runtime or a venv. The idiom
      that only climbs from the script's own file (`os.chdir(os.path.dirname(__file__))`) is free.
    - `pickle|cPickle|marshal|dill|cloudpickle|shelve` `load`/`loads`/`Unpickler`/`open`: a pickle runs code when read and can
      name `hokma.broker` in its own bytes. Refused only together with a sign that Hokma is the target: the word `hokma`,
      a runtime or site-packages path, or a payload decoded or assembled by hand (base64, hex, zlib, chr, bytes, `\\x` escapes,
      a bytes `b''.join`). Loading the student's own model file (`pickle.load(open('model.pkl', 'rb'))`, `joblib.load`) stays
      free: an absolute block would break every sklearn project. Residual risk: a pickle file made elsewhere with Hokma's name inside.
    - `operator.attrgetter` / `methodcaller` with an argument that is not a plain literal, or a literal that names importlib,
      `__import__`, `eval`, `exec`, `compile`, builtins, `__globals__`, `__subclasses__` or hokma; and
      `functools.partial(getattr|eval|exec|__import__ ...)` unless it carries a harmless plain literal name."""
    here = here or ctx.cwd
    roots = package_roots(ctx)
    for m in _CHDIR.finditer(code):
        if code[max(0, m.start() - 4): m.start()].strip().endswith("def"):
            continue
        arg = code[m.end(): m.end() + 300].split("\n", 1)[0]
        lit = re.match(r"\s*(" + _LIT_PATH + r")\s*\)", arg)
        if lit:
            raw = lit.group(1)[:1] in "rR"
            body = lit.group(1).lstrip("rR")[1:-1]
            body = body if raw else body.replace("\\\\", "\\")
            if _PATH_TARGET.search(body + "/") or _under_roots(expand(body, ctx), roots, here) or names_hokma_venv(body, ctx):
                return "chdir into the folders of hokma"
            continue
        inner = arg
        depth = 1
        for k, ch in enumerate(arg):                           # the argument is what stands up to the closing parenthesis
            depth += (ch == "(") - (ch == ")")
            if depth == 0:
                inner = arg[:k]
                break
        if inner.strip() and re.fullmatch(r"(?:" + _CHDIR_OWN_FILE.pattern + r")+", inner.strip()) and "__file__" in inner:
            continue
        return "chdir with a built path"
    if _PICKLE_LOAD.search(code) and _PICKLE_PAYLOAD.search(code):
        return "pickle load with a payload that can name hokma"
    for m in _OPERATOR_CALLER.finditer(code):
        if code[max(0, m.start() - 4): m.start()].strip().endswith("def"):
            continue
        args = code[m.end(): m.end() + 300].split("\n", 1)[0]
        lit = re.match(r"\s*(" + _LIT + r")\s*([,)])", args)
        if lit is None:
            return "attrgetter or methodcaller on a built name"
        if _DANGEROUS_NAME.search(lit.group(1)):
            return "attrgetter or methodcaller on a dangerous name"
    if _MARSHAL.search(code):
        return "marshal load"
    if _HOKMA_ENTRY.search(code):
        return "hokma command line or server entry"
    if partial_of_dynamic(code):
        return "partial of getattr or eval"
    return None


def _call_args(code: str, start: int) -> list[str]:
    """The top-level arguments of the call whose opening parenthesis sits just before `start` (read to 600 characters)."""
    args, depth, cur, quote = [], 1, "", ""
    for ch in code[start: start + 600]:
        if quote:
            cur += ch
            quote = "" if ch == quote else quote
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                break
        elif ch == "," and depth == 1:
            args.append(cur.strip())
            cur = ""
            continue
        cur += ch
    if cur.strip():
        args.append(cur.strip())
    return args


def partial_of_dynamic(code: str) -> bool:
    """`functools.partial` or `partialmethod` (any spelling, any alias) that wraps getattr, eval, exec, __import__ or
    import_module. `partial(getattr, obj, 'x')` with a plain harmless literal name stays free. A partial built from a
    star argument or a `func=` keyword is refused: its first argument cannot be read."""
    callees = set(_PARTIAL_FORMS)
    dyn = {x: x for x in ("getattr", "eval", "exec", "__import__", "import_module")}       # name -> the function it stands for
    for m in re.finditer(r"\bfrom\s+functools\s+import\s+[^\n;]*", code):
        for a in re.finditer(r"\b(?:partial|partialmethod)\s+as\s+(\w+)", m.group(0)):
            callees.add(a.group(1))
    for m in re.finditer(r"(?<![\w.])(\w+)\s*=\s*(?:\w+\s*\.\s*)?(?:partial|partialmethod)\b(?!\s*\()", code):
        callees.add(m.group(1))
    for m in re.finditer(r"(?<![\w.])(\w+)\s*=\s*(?:(?:builtins|__builtins__|importlib)\s*\.\s*)?(getattr|eval|exec|__import__|import_module)\b(?![ \t]*[(\w])", code):
        dyn[m.group(1)] = m.group(2)
    for m in re.finditer(r"\bfrom\s+(?:builtins|importlib)\s+import\s+[^\n;]*?\b(getattr|eval|exec|__import__|import_module)\s+as\s+(\w+)", code):
        dyn[m.group(2)] = m.group(1)
    call = re.compile(r"(?<![\w])(?:" + "|".join(re.escape(x) for x in sorted(callees)) + r")\s*\(")
    for m in call.finditer(code):
        args = _call_args(code, m.end())
        if not args:
            continue
        first = args[0]
        if first.startswith("*") or re.match(r"func\s*=", first):
            return True
        fm = _DYN_FUNC_NAME.match(first)
        name = fm.group(1) if fm else dyn.get(first)
        if name is None:
            continue
        if name == "getattr":
            plain = len(args) in (3, 4) and re.fullmatch(_LIT, args[2]) and not _DANGEROUS_NAME.search(args[2])
            if plain:
                continue
        return True
    return False


# The reasons of `python_block_reason` that name Hokma itself. Every other one is code assembled at run time (`dynamic_code`).
_NAMES_HOKMA = ("dynamic import of hokma", "chdir into the folders of hokma")
DYNAMIC_CODE = "dynamic_code"


def python_block(why: str) -> "Block":
    """The `real_money` block for a reason of `python_block_reason`. Same rule and same verdict for every reason; a
    reason that is code built at run time carries `reason="dynamic_code"`, which only changes the text the agent reads."""
    return Block("real_money", why, "" if why in _NAMES_HOKMA else DYNAMIC_CODE)


def python_block_reason(candidate: str, ctx: "Ctx", here: str | None = None) -> str | None:
    """Every textual reason to refuse python code: a name or code built at run time, or another way into Hokma's code."""
    return names_python_dynamic(candidate) or names_python_escape(candidate, ctx, here)


def names_hokma_venv(text: str, ctx: Ctx) -> bool:
    """The text names a file of the interpreter folder of Hokma's own runtime, by its written form or expanded."""
    return any(HOKMA_VENV_TEXT_RE.search(norm_text(c)) for c in (text, expand(text, ctx)))


def in_hokma_venv(word: str, ctx: Ctx, here: str) -> bool:
    """A command word that is, or resolves (relative path, `..`, a link, `$HOKMA_DATA`) to, a file inside
    `<data>/runtime/<x>/venv/bin` or `Scripts`, or an interpreter inside the folder of the `hokma` package.
    A student's own `.venv` or `venv` is not this."""
    if names_hokma_venv(word, ctx):
        return True
    expanded = expand(word, ctx)
    if not any(ch in expanded for ch in "/\\") or "\0" in expanded:
        return False
    data = {norm_text(ctx.data).rstrip("/"), norm_text(os.path.realpath(ctx.data)).rstrip("/")}
    full = clean_path(joined(expanded, here))
    seen = {norm_text(full)}
    if not (is_win_abs(full) and os.name != "nt"):
        seen.add(norm_text(os.path.realpath(full)))
    if any(re.match(re.escape(d) + r"/runtime/[^/]+/venv/(?:bin|scripts)/+[^/]", s) for d in data for s in seen):
        return True
    pkg = [norm_text(r).rstrip("/") for r in package_roots(ctx) if not norm_text(r).rstrip("/").endswith("/runtime")]
    return any(re.match(re.escape(r) + r"/(?:[^/]+/)*(?:bin|scripts)/+python[\d.]*(?:\.exe)?$", s) for r in pkg for s in seen)


def runtime_python_is_free(word: str, words: list) -> bool:
    """1.2.9: the interpreter of the runtime may run what an agent writes (its tests, rule_check, a backtest): the lock of the modules and the one of the process (core/processguard.py) judge
    what it imports. What stays refused is using it to change the runtime: pip, ensurepip, venv, and any other file of its folder (pip, activate...)."""
    if not re.match(r"python[\d.]*(?:\.exe)?$", base_name(word.strip(_QUOTES)), re.I):
        return False
    return not any(a.text.strip(_QUOTES).lower() in ("pip", "pip3", "ensurepip", "venv", "-mpip", "-mensurepip") for a in words[1:])


def python_runs_from_hokma(here: str, ctx: Ctx) -> bool:
    """Python started with the package folder, the runtime or one of their folders as the current one: the current
    folder is on its import path, so `import hokma` would work from there."""
    return _under_roots(here, package_roots(ctx), here)


def check_hokma_code(text: str, ctx: Ctx, here: str | None = None, python: bool = False) -> None:
    """`real_money` for code or a command that imports Hokma, names its order modules, opens its database, puts its
    folders on the import path or runs the runtime interpreter. `python=True`: the text is python code, so a name
    or a piece of code built at run time is refused too."""
    for candidate in (text, expand(text, ctx)):
        if names_hokma_module(candidate):
            raise Block("real_money", money_module_named(candidate), HOKMA_MONEY_REASON)
        if names_hokma_path_injection(candidate, ctx, here):
            raise Block("real_money", "hokma import path")
        if names_hokma_database(candidate, ctx):
            raise Block("real_money", "hokma database")
        if python:
            why = python_block_reason(candidate, ctx, here)
            if why:
                raise python_block(why)
    if names_hokma_venv(text, ctx) and re.search(r"(?<![/\\\w.])(?:pip3?|ensurepip|venv)(?![/\\\w])|[/\\](?:pip3?|activate)(?![\w])", text, re.I):      # python of the runtime is free; changing the runtime is not
        raise Block("real_money", "hokma runtime interpreter")


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


def check_real_path(path: str, ctx: Ctx, *, reading: bool = False, here: str | None = None, guarded: bool = True) -> None:
    """Resolve a path (relative to the call's folder) and refuse the secret places and, unless the command only reads,
    the hook files by identity. `guarded=False`: the hook files are judged elsewhere (`guard.decide`)."""
    if unresolved(path) and not path.startswith("~"):
        return
    full = clean_path(joined(expand(path, ctx), here or ctx.cwd))
    if is_win_abs(full) and os.name != "nt":
        return                                                  # a Windows path on another system: text markers only
    # Both spellings of the path (as written and by real path), always in this order and the secrets of BOTH before the
    # hook files: the set this replaces made the rule that was reported depend on the hash seed of the process.
    candidates = list(dict.fromkeys((full, os.path.realpath(full))))
    for candidate in candidates:
        if SECRET_RE.search(norm_text(candidate)) or (
                under_any(candidate, [ctx.data]) and not in_workfolder_only(candidate, ctx) and read_hidden(candidate, ctx, content=False)):
            raise Block("secrets", short(path))
    for candidate in candidates:
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
        if under_any(candidate, [ctx.data]) and not in_workfolder_only(candidate, ctx) and read_hidden(candidate, ctx, content=content):
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
    for w in words:                                          # the broker modules and the runtime interpreter: before anything else
        check_hokma_code(w.text, ctx, here=run.here)
        if in_hokma_venv(w.text, ctx, run.here) and not runtime_python_is_free(w.text, words):
            raise Block("real_money", "hokma runtime interpreter")
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

    if name in RUNNERS:                                        # the command after `run` or `exec`, after its own options
        rest = args[[x.lower() for x in texts].index("run") + 1:] if "run" in low else args[low.index("exec") + 1:] \
            if "exec" in low else args if name in ("uvx", "pipx") else []
        for k in range(len(rest)):
            if not rest[k].text.startswith("-"):
                sub = Cmd()
                sub.words, sub.redirs, sub.heredocs, sub.herestrs, sub.pipe_in = rest[k:], cmd.redirs, cmd.heredocs, \
                    cmd.herestrs, cmd.pipe_in
                analyze_cmd_words(sub, prev, mode, run, depth, stdin)
    if is_hokma_cli_word(words[0].text):                      # `hokma engine`, `/x/hokma.exe start`, `./hokma db migrate`
        check_hokma_cli(texts, shown)
    if is_hokma_server_file(words[0].text):                    # `./engine_main.py`: the server entry point by its path
        raise Block("real_money", shown)

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
    check_hokma_code(code, ctx, here=run.here)
    if kind in ("sh", "ps", "cmd"):
        analyze_text(code, {"sh": "bash", "ps": "ps", "cmd": "cmd"}[kind], Run(ctx, run.here, strict=False), depth)
        return
    check_code_markers(code, ctx)
    if kind == "py":
        for candidate in (code, expand(code, ctx)):               # after the secrets: a keyring name built by parts is `secrets`
            why = python_block_reason(candidate, ctx, run.here)
            if why:
                raise python_block(why)
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
        for m in re.finditer(r"[\[(]\s*(" + _LIT + r"(?:\s*,\s*" + _LIT + r")*)", code):      # an argument list: ["hokma", "engine"]
            items = [x.strip()[1:-1] for x in re.findall(_LIT, m.group(1))]
            if items and is_hokma_cli_word(items[0]):
                if code[m.end():].lstrip()[:1] not in ("]", ")"):          # more follows, built at run time: cannot be read
                    raise Block("real_money", short(" ".join(items)))
                check_hokma_cli(items[1:], " ".join(items))
        for m in STR_LITERAL.finditer(code):
            literal = m.group(1) if m.group(1) is not None else m.group(2)
            parts = literal.split()
            if len(parts) > 1 and is_hokma_cli_word(parts[0]):
                check_hokma_cli(parts[1:], literal)
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


# --- the command line of Hokma and its two server entry points (bug 1228): an agent that starts its own engine
# (`printf 'token\n' | hokma engine --port N &`) gets a server of its own and with it the broker, the orders and the owner.
# Only the read-only commands of the CLI stay free. A list of what is allowed, never of what is refused: a new subcommand is
# refused until it is listed here. The text of the block is the normal `real_money` one.
HOKMA_CLI_NAMES = {"hokma", "hokma_cli", "hokma-cli", "hokma_cli.py"}
HOKMA_SERVER_FILES = {"engine_main.py", "trash_mcp.py"}
CLI_FREE_ALONE = {(), ("doctor",), ("version",), ("--help",), ("-h",), ("--version",), ("-v",)}       # no subcommand, or only a flag: argparse prints and exits
RUNNERS = {"uv", "uvx", "pipx", "poetry", "pipenv", "pdm", "hatch", "rye", "conda", "mamba", "micromamba"}      # `uv run hokma engine`
CLI_FREE_PREFIX = {("ext", "request"), ("ext", "requests")}
_QUOTES = "\"'\x60"


def hokma_cli_free(args: list[str]) -> bool:
    """True for the arguments of a `hokma` command that only read: `doctor`, `version`, `ext request ...`, `ext requests`,
    and a lone `--help` or `-h`. Everything else (engine, start, restart, stop, db, update, ext add, kit-sync, ...) is not."""
    low = tuple(x.strip(_QUOTES).lower() for x in args)
    return low in CLI_FREE_ALONE or low[:2] in CLI_FREE_PREFIX


def check_hokma_cli(args: list[str], shown: str) -> None:
    if not hokma_cli_free(args):
        raise Block("real_money", short(shown))


def is_hokma_cli_word(text: str) -> bool:
    return base_name(text.strip(_QUOTES)) in HOKMA_CLI_NAMES or re.split(r"[\\/]", text.strip(_QUOTES))[-1].lower() in HOKMA_CLI_NAMES


def is_hokma_server_file(text: str) -> bool:
    return re.split(r"[\\/]", text.strip(_QUOTES))[-1].lower() in HOKMA_SERVER_FILES


def check_python_module(module_args: list[str]) -> None:
    """`python -m <module> ...`: a module of the package `hokma` is real money; only the owner's `hokma doctor` stays free."""
    module_args = [x.strip().strip("\"'") for x in module_args]       # `-m"hokma.db"` reaching here with its quotes still on
    if module_args and re.match(r"hokma_cli(?:\.main)?$", module_args[0], re.I):       # the CLI: only its read-only commands
        check_hokma_cli(module_args[1:], "python -m " + module_args[0])
    if module_args and re.match(r"(?:[\w.]*\.)?(?:engine_main|trash_mcp)$|hokma_cli[./\\:](?!main$)", module_args[0], re.I):
        raise Block("real_money", short("python -m " + module_args[0]))
    if any(re.match(r"hokma\s*[./\\:]\s*(?:db|" + _HOKMA_MODS + r")(?![\w])", x, re.I) for x in module_args):
        raise Block("real_money", short("python -m " + " ".join(module_args[:2])), HOKMA_MONEY_REASON)
    if any(re.fullmatch(r"hokma", x, re.I) for x in module_args) and not (
            module_args[:2] and [x.lower() for x in module_args[:2]] == ["hokma", "doctor"]):
        raise Block("real_money", "python -m hokma")


def run_interpreter(kind: str, args: list[Word], cmd: Cmd, run: Run, depth: int, stdin: str | None) -> None:
    code, script = None, None
    i = 0
    if kind == "py" and python_runs_from_hokma(run.here, run.ctx):
        raise Block("real_money", "python run from the folder of hokma")        # its current folder is on the import path
    if kind == "py":
        how, value, at = parse_python_args([w.text for w in args])
        if how == "m":
            check_python_module([value or ""] + [w.text for w in args[at + 1:]])
        elif how == "c":
            code = value
        elif how == "script":
            script = value
            if script is not None and is_hokma_server_file(script):
                raise Block("real_money", short("python " + script))
            if script is not None and is_hokma_cli_word(script):
                check_hokma_cli([w.text for w in args[at + 1:]], "python " + script)
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
    if kind == "py" and code is not None and ("\0" in code or re.match(r"\s*(?:\$|\x60)", code)):
        raise Block("real_money", "python code built by the shell", DYNAMIC_CODE)             # `python -c "$(...)"`: code nobody can read
    if kind == "py" and script is not None and script != "-" and not unresolved(expand(script, run.ctx)) and \
            _under_roots(expand(script, run.ctx), package_roots(run.ctx), run.here):
        raise Block("real_money", "python script of the hokma folder")
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
    if not (isinstance(target, str) and os.path.splitext(target)[1].lower() not in CODE_SUFFIXES | {""}):
        suffix = os.path.splitext(target)[1].lower() if isinstance(target, str) else ""
        for text in _written_texts(tool_input):                  # a script that imports Hokma is refused when it is written
            if suffix == ".ipynb":
                text = text.replace('\\"', '"').replace("\\n", "\n")      # the JSON of a notebook escapes its code
            check_hokma_code(text, ctx, here=cwd, python=_is_python_text(target, text))


CODE_SUFFIXES = set(SCRIPT_SUFFIX) | {".pyw", ".ipynb", ".psm1", ".zsh"}
PY_SUFFIXES = {".py", ".pyw", ".ipynb"}
# A command that writes a python file (redirection, tee, cp, mv, install): its text is python code too.
PY_WRITE_RE = re.compile(r"(?:>>?|\btee\b(?:\s+-\w+)*|\b(?:cp|mv|install)\b[^\n;|&]*?)\s*[\"']?[^\s\"'|;&<>]*\.pyw?\b", re.I)


def _is_python_text(target: object, text: str) -> bool:
    """A file the agent writes is python when its suffix says so, or when it has no suffix and no other interpreter's line."""
    suffix = os.path.splitext(target)[1].lower() if isinstance(target, str) else ""
    if suffix in PY_SUFFIXES:
        return True
    if suffix:
        return False
    head = text.lstrip()[:120].splitlines()[0] if text.strip() else ""
    return not (head.startswith("#!") and "python" not in head)


def _written_texts(tool_input: dict) -> list[str]:
    """The text a Write, Edit, MultiEdit or NotebookEdit call puts into a file."""
    out: list[str] = []
    for key in ("content", "new_string", "new_source"):
        if isinstance(tool_input.get(key), str):
            out.append(tool_input[key])
    for item in tool_input.get("edits") if isinstance(tool_input.get("edits"), list) else []:
        if isinstance(item, dict) and isinstance(item.get("new_string"), str):
            out.append(item["new_string"])
    return out


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
    check_hokma_code(command, ctx, here=cwd)                   # also what a here-document writes into a script
    if PY_WRITE_RE.search(command):                            # a script written by the command: judged as python when written
        check_hokma_code(command, ctx, here=cwd, python=True)
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
    return None


# ------------------------------------------------------------------------------------------------ the hook

MESSAGES = {
    "delete": lambda x: (t("hardblock.delete", target=x), t("hardblock.hint.delete")),
    "hokma_internal": lambda x: (t("hardblock.hokma_internal", target=x), t("hardblock.hint.hokma_internal")),
    "outside_workdir": lambda x: (t("hardblock.outside_workdir", target=x), t("hardblock.hint.outside_workdir")),
    "secrets": lambda x: (t("hardblock.secrets", target=x), t("hardblock.hint.secrets")),
    "real_money": lambda x: (t("hardblock.real_money", target=x), t("hardblock.hint.real_money")),
    # Same block as real_money (the rule stays `real_money` in the log, in guard_events and in the verdict), another text.
    "dynamic_code": lambda x: (t("hardblock.dynamic_code", target=x), t("hardblock.hint.dynamic_code")),
    HOKMA_MONEY_REASON: lambda x: (t("hardblock.hokma_money", target=x), t("hardblock.hint.hokma_money")),
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

