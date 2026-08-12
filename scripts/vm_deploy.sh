#!/bin/bash
# Runs on the VM every morning (systemd timer). Pulls whatever's new on
# origin/main, tests it in an isolated staging checkout, and only if tests
# pass applies the changed CODE files to the live tree and restarts the bot.
#
# Never touches live-learned state: strategy.json, the skill files, the
# shadow paper-trading logs, and the strategy-rewrite queue are excluded from
# every apply, in either direction — the VM's live copy always wins. Only
# files that actually differ between the old and new commit are touched, so
# gitignored runtime state (trade_log.json, logs/, control/, research/ picks,
# postmortems/, data/) is never at risk from this script.
#
# Safe by construction: if anything goes wrong before the final apply step,
# the live tree is untouched and the bot keeps running on the old code.
set -uo pipefail

LIVE_DIR="/home/opc/trading-bot"
STAGING_DIR="/home/opc/trading-bot-staging"
LOG_FILE="/home/opc/trading-bot-deploy.log"
BACKUP_DIR="/home/opc/trading-bot-backups"
LOCK_FILE="/home/opc/trading-bot-deploy.lock"
ALERT_WEBHOOK_FILE="$LIVE_DIR/.alert_webhook_url"

# Files that live-diverge on the VM via the bot's own learning loop.
# The VM's copy always wins; the deploy never overwrites these.
PROTECTED_PATHS=(
    "strategy/strategy.json"
    "skills/skill_0_orchestrator.md"
    "skills/skill_1_research.md"
    "skills/skill_2_execution.md"
    "skills/skill_3_midweek.md"
    "skills/skill_4_postmortem.md"
    "skills/skill_4b_victory.md"
    "skills/skill_5_strategy_rewriter.md"
    "skills/skill_6_pattern_detector.md"
    "shadow/options_shadow_log.json"
    "shadow/rx3_paper.json"
    "shadow/rx4_paper.json"
    "shadow/momentum_last_scan.json"
    "research/strategy_rewrite_queue.md"
)

log() { echo "$(date '+%Y-%m-%d %H:%M:%S UTC') $*" | tee -a "$LOG_FILE"; }

alert() {
    local msg="$1"
    log "ALERT: $msg"
    if [ -f "$ALERT_WEBHOOK_FILE" ]; then
        local url
        url=$(cat "$ALERT_WEBHOOK_FILE")
        curl -s -m 10 -X POST "$url" \
            -H 'Content-Type: application/json' \
            -d "{\"title\":\"trading-bot deploy\",\"message\":$(printf '%s' "$msg" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))'),\"text\":$(printf '%s' "$msg" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')}" \
            >/dev/null 2>&1 || log "webhook alert failed (non-fatal)"
    fi
}

is_protected() {
    local path="$1"
    for p in "${PROTECTED_PATHS[@]}"; do
        [ "$path" = "$p" ] && return 0
    done
    return 1
}

exec 200>"$LOCK_FILE"
flock -n 200 || { log "another deploy is already running, skipping"; exit 0; }

cd "$LIVE_DIR" || { log "FATAL: cannot cd to $LIVE_DIR"; exit 1; }

log "checking for updates"
if ! git fetch origin main >>"$LOG_FILE" 2>&1; then
    alert "git fetch failed on the VM — deploy skipped, bot untouched"
    exit 1
fi

OLD_SHA=$(git rev-parse HEAD)
NEW_SHA=$(git rev-parse origin/main)

if [ "$OLD_SHA" = "$NEW_SHA" ]; then
    log "up to date at $OLD_SHA, nothing to do"
    exit 0
fi

log "new commit available: $OLD_SHA -> $NEW_SHA"

# Figure out exactly which tracked files changed, and split into safe
# (apply) vs protected/irrelevant (skip). --no-renames keeps this a plain
# add/modify/delete list instead of rename pairs.
mapfile -t CHANGES < <(git diff --no-renames --name-status "$OLD_SHA" "$NEW_SHA")

SAFE_ENTRIES=()
for entry in "${CHANGES[@]}"; do
    status="${entry%%$'\t'*}"
    path="${entry#*$'\t'}"
    if is_protected "$path"; then
        log "skip (protected, VM version wins): $path"
        continue
    fi
    if [[ "$path" == "RL Trading Bot/"* ]]; then
        continue  # Mac/iOS app, not relevant to the VM
    fi
    SAFE_ENTRIES+=("$status"$'\t'"$path")
done

if [ "${#SAFE_ENTRIES[@]}" -eq 0 ]; then
    log "only protected/irrelevant files changed, fast-forwarding pointer with no file changes"
    git reset "$NEW_SHA" >>"$LOG_FILE" 2>&1
    exit 0
fi

# Build an isolated checkout of the new commit to test against, without
# touching the live tree at all.
rm -rf "$STAGING_DIR"
git worktree prune >>"$LOG_FILE" 2>&1
if ! git worktree add --detach "$STAGING_DIR" "$NEW_SHA" >>"$LOG_FILE" 2>&1; then
    alert "git worktree add failed — deploy aborted, bot untouched"
    exit 1
fi

# Seed the staging checkout with the VM's real current live-learned state so
# tests run against reality, not a stale git snapshot.
for p in "${PROTECTED_PATHS[@]}"; do
    if [ -f "$LIVE_DIR/$p" ]; then
        mkdir -p "$STAGING_DIR/$(dirname "$p")"
        cp "$LIVE_DIR/$p" "$STAGING_DIR/$p"
    fi
done

log "running test suite in staging"
if (cd "$STAGING_DIR" && python3 -m unittest test_agent test_dashboard) >>"$LOG_FILE" 2>&1; then
    log "tests passed"
else
    alert "deploy tests FAILED for $NEW_SHA — leaving VM on $OLD_SHA, see $LOG_FILE"
    git worktree remove --force "$STAGING_DIR" >>"$LOG_FILE" 2>&1
    exit 1
fi

# Backup before touching anything live.
mkdir -p "$BACKUP_DIR"
tar czf "$BACKUP_DIR/pre-deploy-$(date +%Y%m%d-%H%M%S).tar.gz" -C /home/opc trading-bot 2>>"$LOG_FILE"
ls -1t "$BACKUP_DIR"/pre-deploy-*.tar.gz 2>/dev/null | tail -n +8 | xargs -r rm -f

# Apply only the safe files, one by one — never a wholesale directory sync,
# so gitignored runtime state (logs/, control/, data/, trade_log.json,
# research/ picks, postmortems/) can never be touched or deleted.
for entry in "${SAFE_ENTRIES[@]}"; do
    status="${entry%%$'\t'*}"
    path="${entry#*$'\t'}"
    case "$status" in
        A|M)
            mkdir -p "$LIVE_DIR/$(dirname "$path")"
            cp "$STAGING_DIR/$path" "$LIVE_DIR/$path"
            log "applied ($status): $path"
            ;;
        D)
            rm -f "$LIVE_DIR/$path"
            log "applied (D): $path"
            ;;
        *)
            log "unhandled git status '$status' for $path — skipped, review manually"
            ;;
    esac
done

# Move HEAD/index to the new commit without touching the working tree — the
# protected files stay exactly as they are on disk and will show as
# "modified" vs HEAD from here on, which is the correct, expected state.
git reset --mixed "$NEW_SHA" >>"$LOG_FILE" 2>&1

git worktree remove --force "$STAGING_DIR" >>"$LOG_FILE" 2>&1

log "restarting services"
if sudo systemctl restart trading-bot.service trading-bot-dashboard.service >>"$LOG_FILE" 2>&1; then
    log "deploy complete: now on $NEW_SHA"
else
    alert "code deployed to $NEW_SHA but service restart FAILED — check systemctl status by hand"
    exit 1
fi
