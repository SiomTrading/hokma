#!/usr/bin/env bash
# Hokma uninstaller for macOS and Linux.
# Removes the runtime, the `hokma` command and the PATH entry.
# The student's working folder (and the Claude session kept in Hokma's config dir)
# are NEVER deleted without confirmation.
#
# Usage: bash installer/uninstall.sh [--dir <workdir>] [--lang es|en] [--purge]
#   --purge   also delete the working folder and Hokma's Claude config (explicit confirmation:
#             type the word shown in the prompt, or set HOKMA_CONFIRM_PURGE to it when not interactive)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=installer/messages.sh
. "$SCRIPT_DIR/messages.sh"
DIR="${HOKMA_DIR:-$HOME/Hokma}"
if [ -n "${HOKMA_DATA_DIR:-}" ]; then DATA="$HOKMA_DATA_DIR"
elif [ "$(uname -s)" = Darwin ]; then DATA="$HOME/Library/Application Support/Hokma"
else DATA="${XDG_DATA_HOME:-$HOME/.local/share}/hokma"; fi
PURGE=0
BIN_DIR="$HOME/.local/bin"

# Same language as the installation unless told otherwise.
if [ -z "${HOKMA_LANG:-}" ] && [ -f "$DATA/language" ]; then
  saved="$(tr -d '[:space:]' < "$DATA/language")"
  case "$saved" in es|en) HOKMA_LANG_CODE="$saved" ;; esac
fi

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) [ $# -ge 2 ] || die inst.dir_missing; DIR="$2"; shift 2 ;;
    --lang)
      [ $# -ge 2 ] || die inst.dir_missing
      case "$2" in es|en) HOKMA_LANG_CODE="$2" ;; *) die inst.lang_invalid "$2" ;; esac
      shift 2 ;;
    --purge) PURGE=1; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) die inst.unknown_option "$1" ;;
  esac
done

# If the launcher knows the folders, trust it over the defaults.
if [ -f "$BIN_DIR/hokma" ]; then
  if [ -z "${HOKMA_DIR:-}" ] && [ "$DIR" = "$HOME/Hokma" ]; then
    FROM_LAUNCHER="$(sed -n 's/^export HOKMA_HOME="\(.*\)"$/\1/p' "$BIN_DIR/hokma" | head -n 1)"
    if [ -n "$FROM_LAUNCHER" ]; then DIR="$FROM_LAUNCHER"; fi
  fi
  if [ -z "${HOKMA_DATA_DIR:-}" ]; then
    FROM_LAUNCHER="$(sed -n 's/^export HOKMA_DATA_DIR="\(.*\)"$/\1/p' "$BIN_DIR/hokma" | head -n 1)"
    if [ -n "$FROM_LAUNCHER" ]; then DATA="$FROM_LAUNCHER"; fi
  fi
fi

rm -f "$BIN_DIR/hokma"
info un.command_removed

for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile"; do
  if [ -f "$rc" ] && grep -q '# >>> hokma >>>' "$rc"; then
    tmp="$(mktemp)"
    awk '/# >>> hokma >>>/ {skip=1} !skip {print} /# <<< hokma <<</ {skip=0}' "$rc" > "$tmp"
    cat "$tmp" > "$rc"
    rm -f "$tmp"
    info un.path_removed "$rc"
  fi
done

# Ask for the destructive confirmation once.
CONFIRMED=0
PURGE_WORD="$(msg un.purge_word)"
if [ "$PURGE" = 1 ]; then
  if [ -t 0 ]; then
    printf '%s ' "$(msg un.purge_prompt "$DIR" "$PURGE_WORD")"
    read -r answer
    [ "$answer" = "$PURGE_WORD" ] && CONFIRMED=1
  elif [ "${HOKMA_CONFIRM_PURGE:-}" = "$PURGE_WORD" ]; then
    CONFIRMED=1
  fi
fi

if [ -d "$DATA" ]; then
  find "$DATA" -mindepth 1 -maxdepth 1 ! -name claude-config -exec rm -rf {} +
  info un.runtime_removed "$DATA"
  if [ "$CONFIRMED" = 1 ]; then
    rm -rf "$DATA"
  else
    info un.session_kept "$DATA/claude-config"
  fi
fi

if [ -d "$DIR" ]; then
  if [ "$CONFIRMED" = 1 ]; then
    rm -rf "$DIR"
    info un.workdir_removed
  else
    info un.workdir_kept "$DIR"
  fi
fi
info un.done
