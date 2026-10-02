#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$(basename "${BASH_SOURCE[0]}")"
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
  error "Deploy failed during '${STAGE}' at ${BASH_SOURCE[0]}:${line} (exit ${code})"
  echo "  The deployment is in whatever state that line left it — nothing was rolled back." >&2
  exit "$code"
}
trap 'on_err $LINENO' ERR

# The `|| true` is load-bearing: grep exits 1 when the key is absent, and under
# `set -e` that would abort the deploy instead of returning "not set".
_env_get() {
  local key="$1"
  [[ -f .env ]] && grep -E "^${key}=" .env | head -1 | cut -d= -f2- || true
}

usage() { echo "Usage: ./${SELF} [--skip-git] [--branch <name>]" >&2; }

SKIP_GIT=false
BRANCH=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-git) SKIP_GIT=true; shift ;;
    --branch)
      if [[ $# -lt 2 || -z "$2" ]]; then
        error "--branch needs a branch name"; usage; exit 1
      fi
      BRANCH="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) error "Unknown argument: $1"; usage; exit 1 ;;
  esac
done

header "Preflight"
if [[ ! -f .env ]]; then
  error ".env not found — run ./setup.sh first"
  exit 1
fi
if [[ -z "$(_env_get PLEX_TOKEN)" ]]; then
  error "PLEX_TOKEN is not set in .env — run ./setup.sh"
  exit 1
fi
PORT="$(_env_get PORT)"
PORT="${PORT:-8300}"

# The baseline for the handover and the dependency diff: the commit this process
# was read from, or the one a handed-over run carries. Captured once, before
# anything below can move the tree.
OLD_HEAD="${DEPLOY_OLD_HEAD:-$(git rev-parse HEAD)}"

if $SKIP_GIT; then
  [[ "${DEPLOY_RESUMED:-0}" = 1 ]] || info "Skipping git (--skip-git)"
else
  header "Updating Code"
  git fetch --prune -q origin

  # The reset below may run only when it cannot destroy work.
  if ! git diff --quiet HEAD --; then
    error "Uncommitted changes to tracked files; refusing to reset:"
    git status --short --untracked-files=no >&2
    error "Commit and push them, or discard them by hand, then redeploy."
    exit 1
  fi
  if git show-ref --verify --quiet refs/heads/main; then
    UNPUSHED="$(git rev-list --count origin/main..refs/heads/main)"
    if [[ "$UNPUSHED" -gt 0 ]]; then
      error "Local main has ${UNPUSHED} commit(s) not on origin; refusing to reset. Push or drop them first."
      exit 1
    fi
  fi
  UNREACHABLE="$(git rev-list --count HEAD --not --branches --remotes)"
  if [[ "$UNREACHABLE" -gt 0 ]]; then
    error "Detached HEAD has ${UNREACHABLE} commit(s) no branch holds; refusing to switch away."
    exit 1
  fi

  git checkout -q main
  git reset --hard -q origin/main

  AHEAD=()
  while IFS= read -r ref; do
    if [[ "$(git rev-list --count "origin/main..${ref}")" -gt 0 ]]; then
      AHEAD+=("${ref#origin/}")
    fi
  done < <(git for-each-ref --sort=-committerdate --format='%(refname:short)' refs/remotes/origin/claude/)

  TARGET=""
  if [[ -n "$BRANCH" ]]; then
    for b in "${AHEAD[@]}"; do
      if [[ "$b" == "$BRANCH" ]]; then TARGET="$b"; fi
    done
    if [[ -z "$TARGET" ]]; then
      error "Branch '${BRANCH}' is not ahead of origin/main — nothing to merge."
      exit 1
    fi
  elif [[ ${#AHEAD[@]} -eq 0 ]]; then
    info "Nothing to merge — deploying origin/main"
  elif [[ ${#AHEAD[@]} -eq 1 ]]; then
    TARGET="${AHEAD[0]}"
  else
    if [[ ! -t 0 ]]; then
      error "No terminal to prompt on — pass --branch <name> to choose one."
      exit 1
    fi
    for i in "${!AHEAD[@]}"; do echo "  $((i + 1))) ${AHEAD[$i]}"; done
    read -rp "Which branch do you want to merge and deploy? [1]: " _CHOICE
    if [[ ! "${_CHOICE:-1}" =~ ^[0-9]+$ ]] || (( ${_CHOICE:-1} < 1 || ${_CHOICE:-1} > ${#AHEAD[@]} )); then
      error "Not a choice: ${_CHOICE}"
      exit 1
    fi
    TARGET="${AHEAD[$(( ${_CHOICE:-1} - 1 ))]}"
  fi

  if [[ -n "$TARGET" ]]; then
    info "Merging ${TARGET}"
    git merge --no-ff -q "origin/${TARGET}" -m "deploy: merge ${TARGET}"
    git push -q origin main
  fi
fi

if [[ "${DEPLOY_RESUMED:-0}" = 1 ]]; then
  info "Continuing as the merged ${SELF}"
elif ! git diff --quiet "$OLD_HEAD" HEAD -- "$SELF"; then
  info "${SELF} changed in the git section — restarting with the merged version …"
  DEPLOY_RESUMED=1 DEPLOY_OLD_HEAD="$OLD_HEAD" exec "${REPO_ROOT}/${SELF}" --skip-git
fi

header "Building"
BUILD_FLAGS=""
# grep reads to the end: `grep -q` would exit at the first match, git would die of
# SIGPIPE, and under pipefail a match on a large diff reads as a miss.
if git diff --name-only "$OLD_HEAD" HEAD 2>/dev/null \
    | grep -E '(^|/)(requirements[^/]*\.txt|pyproject\.toml|uv\.lock|poetry\.lock|Pipfile(\.lock)?|package(-lock)?\.json|yarn\.lock|pnpm-lock\.yaml|bun\.lockb?|go\.(mod|sum)|Cargo\.(toml|lock)|Gemfile(\.lock)?|composer\.(json|lock))$' >/dev/null; then
  warn "Dependencies changed — rebuilding without cache..."
  BUILD_FLAGS="--no-cache"
fi
docker compose build $BUILD_FLAGS

header "Starting"
docker compose up -d --remove-orphans

header "Health checks"
wait_for() {
  local name="$1" cmd="$2" max="${3:-30}"
  local i=0
  info "Waiting for ${name}..."
  while ! eval "$cmd" &>/dev/null; do
    i=$((i + 1))
    if [[ $i -ge $max ]]; then
      error "${name} did not become ready after ${max}s"
      exit 1
    fi
    sleep 1
  done
  success "${name} is ready"
}
# /health is liveness only and does not touch Plex, so the deploy succeeds while Plex
# is down; the page shows that error itself. Plex is checked once, below, as a warning.
wait_for "App" "curl -sf http://localhost:${PORT}/health" 60
if curl -sf "http://localhost:${PORT}/health/deps" >/dev/null; then
  success "Plex is reachable with the configured token"
else
  warn "Plex is not reachable right now, or rejected the token; the dashboard will show the error (detail: curl http://localhost:${PORT}/health/deps)"
fi

header "Done"
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')" || LAN_IP=""
success "Plex Library Audit is running!"
echo "  URL:       http://${LAN_IP:-localhost}:${PORT}"
echo "  Logs:      docker compose logs -f"
echo "  Stop:      docker compose down"
echo "  Redeploy:  ./deploy.sh --skip-git"
