#!/bin/bash
# update.sh — Refresh LLM pricing data from upstream repository
# Called by weekly cron. Also safe to run manually.
# Supports write-access with GITHUB_TOKEN env var.

LOG_FILE="/var/log/llmpricing-update.log"
REPO_DIR="/app/repo"
WWW_DIR="/var/www/html"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

log "=== Update started ==="

# --- Configure git credentials (if token provided) ---
if [ -n "${GITHUB_TOKEN:-}" ]; then
    # Set up credential helper so git pull/push works
    git config --global credential.helper 'store --file=/tmp/git-creds'
    echo "https://git:${GITHUB_TOKEN}@github.com" > /tmp/git-creds
    chmod 600 /tmp/git-creds
    log "GitHub token configured for authenticated operations"
fi

# Apply git identity from env (overrides build-time values)
if [ -n "${GIT_USER_NAME:-}" ]; then
    git config --global user.name "$GIT_USER_NAME"
fi
if [ -n "${GIT_USER_EMAIL:-}" ]; then
    git config --global user.email "$GIT_USER_EMAIL"
fi

# --- 1. Clone or pull ---
log "Fetching latest repository data..."

if [ -d "$REPO_DIR/.git" ]; then
    # Existing repo — pull latest
    cd "$REPO_DIR"
    REMOTE_URL=$(git remote get-url origin)
    log "Remote: $REMOTE_URL"
    git pull --ff-only origin master 2>&1 | tee -a "$LOG_FILE"
    PULL_EXIT=${PIPESTATUS[0]}
    if [ "$PULL_EXIT" -ne 0 ]; then
        log "WARNING: git pull failed (exit $PULL_EXIT) — will try clone fallback"
        cd / && rm -rf "$REPO_DIR"
    fi
fi

if [ ! -d "$REPO_DIR/.git" ]; then
    # No repo — clone fresh
    if [ -n "${GITHUB_TOKEN:-}" ] && [ -n "${FORK_OWNER:-}" ]; then
        # Clone the user's fork with auth
        CLONE_URL="https://git:${GITHUB_TOKEN}@github.com/${FORK_OWNER}/llmpricing.git"
        log "Cloning fork: ${FORK_OWNER}/llmpricing..."
        git clone --depth 1 "$CLONE_URL" "$REPO_DIR" 2>&1 | tee -a "$LOG_FILE"
    else
        # No fork info — clone upstream public
        log "Cloning upstream sanand0/llmpricing..."
        git clone --depth 1 https://github.com/sanand0/llmpricing.git "$REPO_DIR" 2>&1 | tee -a "$LOG_FILE"
    fi
    cd "$REPO_DIR"
fi

GIT_HASH=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
GIT_REMOTE=$(git remote get-url origin 2>/dev/null || echo "none")
log "At $GIT_HASH from $GIT_REMOTE"

# --- 2. Fetch fresh pricing from OpenRouter and merge ---
log "Fetching fresh LLM pricing from OpenRouter..."
cd "$REPO_DIR"
python3 /app/fetch-data.py --repo-dir "$REPO_DIR" --commit 2>&1 | tee -a "$LOG_FILE"
FETCH_EXIT=${PIPESTATUS[0]}
if [ "$FETCH_EXIT" -ne 0 ]; then
    log "WARNING: fetch-data.py exited with code $FETCH_EXIT — continuing with existing data"
fi
cd "$REPO_DIR"

# --- 3. Copy site files to web root ---
log "Syncing to web root..."
find "$REPO_DIR" -maxdepth 1 -type f \( -name "*.html" -o -name "*.js" -o -name "*.json" -o -name "*.csv" -o -name "*.md" \) -exec cp {} "$WWW_DIR/" \;

# --- 3. Verify the site is intact ---
log "Verifying site integrity..."
SITE_OK=true
for f in index.html elo.csv narrative.json script.js README.md; do
    if [ -f "$WWW_DIR/$f" ]; then
        SIZE=$(stat -c%s "$WWW_DIR/$f" 2>/dev/null)
        log "  ✓ $f ($SIZE bytes)"
    else
        log "  ✗ $f MISSING!"
        SITE_OK=false
    fi
done

if [ "$SITE_OK" = true ]; then
    log "=== Update complete — site healthy ==="
else
    log "=== Update complete — some files missing! ==="
fi
