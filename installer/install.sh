#!/usr/bin/env bash
# Hokma installer for macOS and Linux. No administrator rights needed.
#
# Usage (from a checkout of the repo):
#   bash installer/install.sh [--dir <workdir>] [--lang es|en] [--no-doctor]
#
# Environment:
#   HOKMA_DIR        working folder (default: ~/Hokma)
#   HOKMA_DATA_DIR   runtime + Claude config, outside the working folder
#                    (Linux ${XDG_DATA_HOME:-~/.local/share}/hokma, macOS ~/Library/Application Support/Hokma)
#   HOKMA_LANG       language of the messages: es (default) or en
#   HOKMA_LOCK_FILE  alternative lock.json (tests)
#
# Staged steps, versions pinned with sha256, sanitised environment. Messages come from installer/messages.

set -euo pipefail

# Do not let the student's Python environment leak into the install.
unset PYTHONPATH PYTHONHOME || true
export UV_NO_CONFIG=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=installer/messages.sh
. "$SCRIPT_DIR/messages.sh"
LOCK="${HOKMA_LOCK_FILE:-$SCRIPT_DIR/lock.json}"
DIR="${HOKMA_DIR:-$HOME/Hokma}"
RUN_DOCTOR=1

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) [ $# -ge 2 ] || die inst.dir_missing; DIR="$2"; shift 2 ;;
    --lang)
      [ $# -ge 2 ] || die inst.dir_missing
      case "$2" in es|en) HOKMA_LANG_CODE="$2" ;; *) die inst.lang_invalid "$2" ;; esac
      shift 2 ;;
    --no-doctor) RUN_DOCTOR=0; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) die inst.unknown_option "$1" ;;
  esac
done

# ---- Stage 0: preconditions -------------------------------------------------
if [ "$(id -u)" = "0" ] && [ -z "${HOKMA_ALLOW_ROOT:-}" ]; then
  die inst.no_root
fi
[ -f "$LOCK" ] || die inst.no_lock "$LOCK"
[ -f "$SRC/pyproject.toml" ] || die inst.not_checkout

case "$DIR" in /*) ;; *) DIR="$PWD/$DIR" ;; esac
if [ -n "${HOKMA_DATA_DIR:-}" ]; then HOKMA_DATA="$HOKMA_DATA_DIR"
elif [ "$(uname -s)" = Darwin ]; then HOKMA_DATA="$HOME/Library/Application Support/Hokma"
else HOKMA_DATA="${XDG_DATA_HOME:-$HOME/.local/share}/hokma"; fi
case "$HOKMA_DATA/" in "$DIR"/*) die inst.data_inside_work "$HOKMA_DATA" "$DIR" ;; esac
case "$DIR/" in "$HOKMA_DATA"/*) die inst.work_inside_data "$HOKMA_DATA" ;; esac
BIN_DIR="$HOME/.local/bin"
CONFIG_DIR="$HOKMA_DATA/claude-config"

# ---- lock.json helpers (no jq, no python needed yet) ------------------------
lock_top() { # key -> string value of a top-level key
  awk -v k="\"$1\"" '$0 ~ "^ *" k ": *\"" { sub(/^[^:]*: *"/, ""); sub(/".*/, ""); print; exit }' "$LOCK"
}
lock_platform() { # platform field
  awk -v p="\"$1\"" -v f="\"$2\"" '
    index($0, p ": {") { inb = 1; next }
    inb && index($0, f ":") { sub(/^[^:]*: *"/, ""); sub(/".*/, ""); print; exit }
    inb && /}/ { exit }' "$LOCK"
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'
  else die inst.no_sha; fi
}

download() { # url dest
  if command -v curl >/dev/null 2>&1; then curl -fsSL --retry 3 -o "$2" "$1"
  elif command -v wget >/dev/null 2>&1; then wget -q -O "$2" "$1"
  else die inst.no_curl; fi
}

# ---- Stage 1: platform ------------------------------------------------------
case "$(uname -s)" in
  Linux)  OS=linux ;;
  Darwin) OS=macos ;;
  *) die inst.bad_os "$(uname -s)" ;;
esac
case "$(uname -m)" in
  x86_64|amd64) ARCH=x86_64 ;;
  aarch64|arm64) ARCH=aarch64 ;;
  *) die inst.bad_arch "$(uname -m)" ;;
esac
PLATFORM="$OS-$ARCH"
URL="$(lock_platform "$PLATFORM" url)"
EXPECTED="$(lock_platform "$PLATFORM" sha256)"
UV_VERSION="$(lock_top uv_version)"
PY_VERSION="$(lock_top python_version)"
[ -n "$URL" ] && [ -n "$EXPECTED" ] || die inst.no_platform "$PLATFORM"
info inst.platform "$PLATFORM" "$DIR"

# ---- Stage 2: dependencies (never installed behind the student's back) ------
if ! command -v git >/dev/null 2>&1; then
  if [ "$OS" = macos ]; then die inst.git_missing_mac; else die inst.git_missing_linux; fi
fi
if ! command -v claude >/dev/null 2>&1; then
  warn inst.claude_missing
  warn inst.claude_install_unix
  warn inst.claude_guide
fi

# ---- Stage 3: uv (sha256 verified before anything is executed) --------------
mkdir -p "$HOKMA_DATA/bin" "$HOKMA_DATA/tmp"
printf '%s\n' "$HOKMA_LANG_CODE" > "$HOKMA_DATA/language"
TMP="$(mktemp -d "$HOKMA_DATA/tmp/dl.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
ARCHIVE="$TMP/uv-archive"
info inst.download_uv "$UV_VERSION"
download "$URL" "$ARCHIVE" || die inst.download_failed "$URL"
ACTUAL="$(sha256_of "$ARCHIVE")"
if [ "$ACTUAL" != "$EXPECTED" ]; then
  die inst.sha_mismatch "$EXPECTED" "$ACTUAL"
fi
tar -xzf "$ARCHIVE" -C "$TMP"
UV_SRC="$(find "$TMP" -type f -name uv -perm -u+x | head -n 1)"
[ -n "$UV_SRC" ] || die inst.uv_not_in_package
cp "$UV_SRC" "$HOKMA_DATA/bin/uv"
chmod +x "$HOKMA_DATA/bin/uv"
UV="$HOKMA_DATA/bin/uv"

# ---- Stage 4: Python and Hokma app, built in the inactive slot --------------
# Blue-green runtime: <data>/runtime/a and /b. The new version is built in the slot that is NOT active
# and only then does `current` flip (atomic mv), so a failed install keeps the previous version.
ACTIVE=""
if [ -f "$HOKMA_DATA/current" ]; then ACTIVE="$(tr -d '[:space:]' < "$HOKMA_DATA/current")"; fi
if [ "$ACTIVE" = a ]; then SLOT=b; else SLOT=a; fi
SLOT_DIR="$HOKMA_DATA/runtime/$SLOT"
APP="$SLOT_DIR/app"
VENV="$SLOT_DIR/venv"

export UV_PYTHON_INSTALL_DIR="$HOKMA_DATA/python"
export UV_CACHE_DIR="$HOKMA_DATA/cache"
export UV_PYTHON_PREFERENCE=only-managed
export UV_PROJECT_ENVIRONMENT="$VENV"
info inst.install_python "$PY_VERSION"
"$UV" python install "$PY_VERSION"

info inst.copy_app
rm -rf "$SLOT_DIR"
mkdir -p "$APP"
tar -C "$SRC" --exclude=.git --exclude=node_modules --exclude=.venv --exclude=__pycache__ -cf - . | tar -C "$APP" -xf -
[ -f "$APP/dashboard/dist/index.html" ] || die inst.no_dashboard

info inst.install_deps
"$UV" sync --project "$APP" --frozen --no-dev --python "$PY_VERSION"

HOKMA_ENV=(HOKMA_HOME="$DIR" HOKMA_DATA_DIR="$HOKMA_DATA" CLAUDE_CONFIG_DIR="$CONFIG_DIR")
# Integrity manifest (sha256 of app/, which includes the settings template). hokma and doctor verify it.
env "${HOKMA_ENV[@]}" "$VENV/bin/hokma" write-manifest || die inst.manifest_failed
env "${HOKMA_ENV[@]}" "$VENV/bin/hokma" verify-install >/dev/null || die inst.verify_failed

# ---- Stage 5: working folder as a Git repo, kit (never overwriting the student), config
mkdir -p "$DIR" "$CONFIG_DIR"
env "${HOKMA_ENV[@]}" "$VENV/bin/hokma" kit-sync >/dev/null || die inst.kit_failed
if [ ! -f "$DIR/.gitignore" ]; then
  printf '%s\n' '*.db' '*.sqlite' '*.sqlite-wal' '*.sqlite-shm' '.env' > "$DIR/.gitignore"
fi
# git runs ONLY here, on a repo that does not exist yet: nothing can be planted in it. If anything named .git is
# there (folder, file, or link, even a broken one) it is the student's repo and Hokma never runs git on it.
if [ ! -e "$DIR/.git" ] && [ ! -L "$DIR/.git" ]; then
  git -C "$DIR" init -q
  git -C "$DIR" add -A >/dev/null 2>&1 || true
  GIT_ID=()
  if [ -z "$(git -C "$DIR" config user.name || true)" ]; then GIT_ID+=(-c user.name=Hokma -c user.email=hokma@localhost); fi
  git -C "$DIR" ${GIT_ID[@]+"${GIT_ID[@]}"} commit -q -m "Hokma: initial kit" >/dev/null 2>&1 || warn inst.commit_failed "$DIR" ""
fi

# ---- Stage 6: launcher, atomic switch to the new slot, settings, PATH --------
mkdir -p "$BIN_DIR"
LAUNCHER_ERROR="$(msg inst.launcher_error)"
cat > "$BIN_DIR/hokma" <<EOF
#!/usr/bin/env bash
# Generated by the Hokma installer. Do not edit.
export HOKMA_HOME="$DIR"
export HOKMA_DATA_DIR="$HOKMA_DATA"
export CLAUDE_CONFIG_DIR="$CONFIG_DIR"
SLOT="\$(tr -d '[:space:]' < "\$HOKMA_DATA_DIR/current" 2>/dev/null || true)"
case "\$SLOT" in
  a|b) ;;
  *) echo "[error] $LAUNCHER_ERROR" >&2; exit 1 ;;
esac
exec "\$HOKMA_DATA_DIR/runtime/\$SLOT/venv/bin/hokma" "\$@"
EOF
chmod +x "$BIN_DIR/hokma"
printf '%s\n' "$SLOT" > "$HOKMA_DATA/current.tmp"
mv -f "$HOKMA_DATA/current.tmp" "$HOKMA_DATA/current"
# Runtime and Claude config live OUTSIDE the working folder; lock them from the agent.
env "${HOKMA_ENV[@]}" "$VENV/bin/hokma" init-config >/dev/null || die inst.config_failed

add_path_block() {
  case ":$PATH:" in *":$BIN_DIR:"*) return 0 ;; esac
  local rc
  case "${SHELL:-}" in
    */zsh)  rc="$HOME/.zshrc" ;;
    */bash) if [ "$OS" = macos ]; then rc="$HOME/.bash_profile"; else rc="$HOME/.bashrc"; fi ;;
    *)      rc="$HOME/.profile" ;;
  esac
  if [ -f "$rc" ] && grep -q '# >>> hokma >>>' "$rc"; then return 0; fi
  {
    printf '\n# >>> hokma >>>\n'
    printf 'export PATH="%s:$PATH"\n' "$BIN_DIR"
    printf '# <<< hokma <<<\n'
  } >> "$rc"
  info inst.path_added "$BIN_DIR" "$rc"
}
add_path_block

# Shortcut: Hokma. Old Shut down and Restart shortcuts are removed. macOS: applications in ~/Applications/Hokma (no Terminal);
# Linux: .desktop files with Terminal=false. One implementation, shared with `hokma update`: `hokma shortcuts`
# (hokma.core.shortcuts). They point at the stable launcher, and there is NO start at login: Hokma starts when the
# owner opens the "Hokma" shortcut.
if env "${HOKMA_ENV[@]}" "$VENV/bin/hokma" shortcuts --force >/dev/null; then info inst.shortcuts_created; else warn inst.shortcuts_failed; fi

# ---- Stage 7: doctor --------------------------------------------------------
info inst.installed "$DIR"
if [ "$RUN_DOCTOR" = 1 ]; then
  info inst.running_doctor
  "$BIN_DIR/hokma" doctor || warn inst.doctor_problems
fi
info inst.done
