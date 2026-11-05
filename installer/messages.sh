# Message catalog loader shared by install.sh and uninstall.sh (source it, do not run it).
# Messages live in installer/messages/<lang>.properties as KEY=text, with {1}, {2}... placeholders.
# Language: --lang, then HOKMA_LANG, then Spanish.

MSG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/messages"
HOKMA_LANG_CODE="${HOKMA_LANG:-es}"
case "$HOKMA_LANG_CODE" in es|en) ;; *) HOKMA_LANG_CODE=es ;; esac

msg_lookup() { # key lang
  awk -v k="$1" 'index($0, k "=") == 1 { print substr($0, length(k) + 2); exit }' "$MSG_DIR/$2.properties"
}

msg() { # key [args...]
  local key="$1" text i=1 arg
  shift
  text="$(msg_lookup "$key" "$HOKMA_LANG_CODE")"
  [ -n "$text" ] || text="$(msg_lookup "$key" en)"
  [ -n "$text" ] || text="$key"
  for arg in "$@"; do
    text=${text//\{$i\}/"$arg"}
    i=$((i + 1))
  done
  printf '%s' "$text"
}

info() { printf '\033[0;36m%s\033[0m %s\n' "$(msg tag.info)" "$(msg "$@")"; }
warn() { printf '\033[0;33m%s\033[0m %s\n' "$(msg tag.warn)" "$(msg "$@")"; }
die()  { printf '\033[0;31m%s\033[0m %s\n' "$(msg tag.error)" "$(msg "$@")" >&2; exit 1; }
