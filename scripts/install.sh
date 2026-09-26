#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./scripts/install.sh <profile>

Examples:
  ./scripts/install.sh code-agent
  ./scripts/install.sh codex-agent
  ./scripts/install.sh default

The installer copies only runtime files, vendors the pinned Codex SDK/CLI
inside the plugin directory, validates the staged plugin, and enables it
without built-in tool override permission.
EOF
}

if [[ $# -ne 1 || "$1" == "-h" || "$1" == "--help" ]]; then
  usage
  [[ $# -eq 1 ]] && exit 0 || exit 2
fi

PROFILE="$1"
if [[ ! "$PROFILE" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "Invalid profile name: $PROFILE" >&2
  exit 2
fi

if ! command -v hermes >/dev/null 2>&1; then
  echo "Hermes CLI is not installed or not on PATH." >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required." >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$PROFILE" == "default" ]]; then
  PROFILE_HOME="${HOME}/.hermes"
else
  PROFILE_HOME="${HOME}/.hermes/profiles/${PROFILE}"
fi
if [[ ! -f "$PROFILE_HOME/config.yaml" ]]; then
  echo "Hermes profile not found: $PROFILE_HOME" >&2
  exit 1
fi

PLUGINS_DIR="$PROFILE_HOME/plugins"
mkdir -p "$PLUGINS_DIR"
DEST="$PLUGINS_DIR/hermes-codex"
STAGE="$(mktemp -d "$PLUGINS_DIR/.hermes-codex-stage.XXXXXX")"
BACKUP=""
cleanup() {
  if [[ -d "$STAGE" ]]; then
    rm -rf "$STAGE"
  fi
  return 0
}
trap cleanup EXIT

install -m 0644 "$ROOT/__init__.py" "$ROOT/plugin.yaml" "$ROOT/LICENSE" "$ROOT/NOTICE" "$STAGE/"
cp -a "$ROOT/hermes_codex" "$STAGE/hermes_codex"
find "$STAGE" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$STAGE" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete

# Keep dependencies plugin-local. --no-deps deliberately avoids replacing
# Hermes' pinned pydantic/packaging stack; those core packages are already present.
python3 -m pip install --disable-pip-version-check --no-input --no-deps \
  --target "$STAGE" \
  "openai-codex==0.157.0" \
  "openai-codex-cli-bin==0.157.0"

find "$STAGE" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$STAGE" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
HERMES_HOME="$PROFILE_HOME" hermes plugins validate "$STAGE"
HERMES_HOME="$PROFILE_HOME" hermes plugins doctor "$STAGE" --ci

if [[ -e "$DEST" ]]; then
  BACKUP="${DEST}.backup.$(date -u +%Y%m%dT%H%M%SZ)"
  mv "$DEST" "$BACKUP"
fi
if ! mv "$STAGE" "$DEST"; then
  [[ -n "$BACKUP" && -e "$BACKUP" ]] && mv "$BACKUP" "$DEST"
  exit 1
fi

if ! HERMES_HOME="$PROFILE_HOME" hermes plugins enable --no-allow-tool-override hermes-codex; then
  rm -rf "$DEST"
  [[ -n "$BACKUP" && -e "$BACKUP" ]] && mv "$BACKUP" "$DEST"
  exit 1
fi

HERMES_HOME="$PROFILE_HOME" hermes plugins doctor "$DEST" --ci
printf '\nInstalled hermes-codex for profile %s.\n' "$PROFILE"
printf 'Restart that profile gateway, then start a new session with /reset.\n'
printf 'Previous installation backup: %s\n' "${BACKUP:-none}"
