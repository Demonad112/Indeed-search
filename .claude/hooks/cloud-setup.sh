#!/usr/bin/env bash
# Installs dependencies when a Claude Code cloud session starts (web, mobile, desktop cloud).
# Does nothing on local machines. Installer output goes to stderr to keep session context small.
[ "$CLAUDE_CODE_REMOTE" = "true" ] || exit 0
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
log() { echo "[cloud-setup] $*" >&2; }

if [ -f pnpm-lock.yaml ]; then
  corepack enable >/dev/null 2>&1
  corepack pnpm install --frozen-lockfile 1>&2 || log "pnpm install failed"
elif [ -f package-lock.json ]; then
  npm ci --no-audit --no-fund 1>&2 || log "npm ci failed"
elif [ -f yarn.lock ]; then
  corepack yarn install --frozen-lockfile 1>&2 || log "yarn install failed"
fi

if [ -f requirements.txt ]; then
  pip install -q -r requirements.txt 1>&2 || log "pip install failed"
fi

if [ -f Cargo.toml ] && command -v cargo >/dev/null 2>&1; then
  cargo fetch -q 1>&2 || log "cargo fetch failed"
fi

echo "cloud-setup: dependencies installed (see stderr for failures)"
exit 0
