#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${BLUE}▶${NC} $*"; }
success() { echo -e "${GREEN}✓${NC} $*"; }
warn()    { echo -e "${YELLOW}⚠${NC} $*"; }
error()   { echo -e "${RED}✗${NC} $*" >&2; }
header()  { STAGE="$*"; echo -e "\n${BOLD}═══ $* ═══${NC}"; }

STAGE="startup"
on_err() {
  local code=$? line=$1
  # -E hands this trap to $(…), <(…) and ( ) subshells, whose exit ends only the
  # subshell. A status the parent sees fires it again there; report only from the top.
  (( BASH_SUBSHELL == 0 )) || exit "$code"
  error "Setup failed during '${STAGE}' at ${BASH_SOURCE[0]}:${line} (exit ${code})"
  exit "$code"
}
trap 'on_err $LINENO' ERR

# The `|| true` is load-bearing: grep exits 1 when the key is absent, and under
# `set -e` that would abort the caller instead of returning "not set".
_env_get() {
  local key="$1"
  [[ -f .env ]] && grep -E "^${key}=" .env | head -1 | cut -d= -f2- || true
}

_env_set() {
  local key="$1" val="$2"
  [[ -f .env ]] || touch .env
  if grep -q "^${key}=" .env; then
    local escaped
    escaped=$(printf '%s\n' "$val" | sed 's/[\/&]/\\&/g')
    sed -i "s|^${key}=.*|${key}=${escaped}|" .env
  else
    printf '%s=%s\n' "$key" "$val" >> .env
  fi
}

DEFAULT_PORT=8300
PROJECT="${COMPOSE_PROJECT_NAME:-$(basename "$REPO_ROOT" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')}"

# Host ports published by a "docker ps" Ports column, one per line. grep alone is
# guarded: no published ports is an ordinary answer, not a failure.
_published() { { grep -oE ':[0-9]+->' || true; } | sed -E 's/^:([0-9]+)->$/\1/'; }

# Host ports already taken: ports other containers publish, plus listening sockets.
# This project's own container is excluded from both, so a re-run does not find its
# own port taken.
used_ports() {
  local ps_out ours ss_out
  # A failure here must stop setup: an empty list would offer a port that is taken.
  if ! ps_out="$(docker ps --format '{{.Label "com.docker.compose.project"}}|{{.Ports}}')"; then
    error "docker ps failed; cannot tell which ports are in use"
    exit 1
  fi
  # Not knowing our own port only means a re-run suggests a different free one.
  ours="$(awk -F'|' -v me="$PROJECT" '$1 == me {print $2}' <<<"$ps_out" | _published | tr '\n' ' ')" || ours=""
  awk -F'|' -v me="$PROJECT" '$1 != me {print $2}' <<<"$ps_out" | _published
  if command -v ss >/dev/null; then
    if ! ss_out="$(ss -Htln)"; then
      error "ss failed; cannot tell which ports are in use"
      exit 1
    fi
    awk -v ours=" ${ours} " '{ n = split($4, a, ":"); if (index(ours, " " a[n] " ") == 0) print a[n] }' <<<"$ss_out"
  fi
}

port_taken() { grep -qx "$1" <<<"$USED_PORTS"; }

header "Prerequisites"
if ! command -v docker >/dev/null; then
  error "docker is not installed"
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  error "Docker Compose v2 (docker compose) is required"
  exit 1
fi
if ! docker info &>/dev/null; then
  error "Docker daemon is not running, or this user cannot reach it"
  exit 1
fi
success "Docker, Compose v2 and the daemon are available"

header "Configuration"
[[ -f .env ]] || touch .env
chmod 600 .env  # it holds the Plex token

# Plex token: prompt on first run, offer to replace on re-run, never echo it.
if [[ -z "$(_env_get PLEX_TOKEN)" ]]; then
  read -rsp "Plex token (X-Plex-Token): " TOKEN; echo
  if [[ -z "$TOKEN" ]]; then
    error "A Plex token is required"
    exit 1
  fi
  _env_set PLEX_TOKEN "$TOKEN"
  success "Plex token saved"
else
  read -rp "A Plex token is already set. Replace it? [y/N]: " REPLACE
  if [[ "$REPLACE" =~ ^[Yy]$ ]]; then
    read -rsp "New Plex token: " TOKEN; echo
    if [[ -n "$TOKEN" ]]; then
      _env_set PLEX_TOKEN "$TOKEN"
      success "Plex token updated"
    else
      warn "Empty input; keeping the existing token"
    fi
  fi
fi

# Plex URL: no default, since it differs on every network. localhost would point the
# container at itself, so it is refused.
if [[ -z "$(_env_get PLEX_BASE_URL)" ]]; then
  while true; do
    read -rp "Plex server URL, as reached from this host (e.g. http://192.168.1.10:32400): " PLEX_URL
    PLEX_URL="${PLEX_URL%/}"
    if [[ ! "$PLEX_URL" =~ ^https?://[^/]+ ]]; then
      error "Enter a URL starting with http:// or https://"
    elif [[ "$PLEX_URL" =~ ^https?://(localhost|127\.[0-9.]+)(:|/|$) ]]; then
      error "localhost would mean the container itself; use the host's LAN IP or name"
    else
      break
    fi
  done
  _env_set PLEX_BASE_URL "$PLEX_URL"
fi

# Port: keep the one already chosen; otherwise find the first free port from 8300.
CURRENT_PORT="$(_env_get PORT)"
USED_PORTS="$(used_ports)"
if [[ -n "$CURRENT_PORT" ]]; then
  if port_taken "$CURRENT_PORT"; then
    warn "Port ${CURRENT_PORT} (from .env) is in use by something else; edit PORT in .env if the deploy fails to bind it"
  else
    info "Keeping port ${CURRENT_PORT} from .env"
  fi
else
  SUGGESTED="$DEFAULT_PORT"
  while port_taken "$SUGGESTED"; do
    SUGGESTED=$((SUGGESTED + 1))
  done
  if [[ "$SUGGESTED" != "$DEFAULT_PORT" ]]; then
    info "Port ${DEFAULT_PORT} is taken; the first free port is ${SUGGESTED}"
  fi
  while true; do
    read -rp "Port for the dashboard [${SUGGESTED}]: " CHOSEN
    CHOSEN="${CHOSEN:-$SUGGESTED}"
    if [[ ! "$CHOSEN" =~ ^[0-9]+$ ]] || (( CHOSEN < 1 || CHOSEN > 65535 )); then
      error "Not a valid port: ${CHOSEN}"
    elif port_taken "$CHOSEN"; then
      error "Port ${CHOSEN} is already in use"
    else
      break
    fi
  done
  _env_set PORT "$CHOSEN"
  success "Using port ${CHOSEN}"
fi

if [[ -z "$(_env_get CACHE_TTL)" ]]; then
  _env_set CACHE_TTL 300
fi

success "Configuration written to .env"
exec ./deploy.sh --skip-git
