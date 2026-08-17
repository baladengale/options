#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# OIE — Options Income Engine orchestrator (macOS launchd)
#
# Wrapper invoked by ~/Library/LaunchAgents/com.oie.engine.plist
#
# Logic:
#   1. Port-check heuristic distinguishes cold boot vs crash recovery:
#      - OpenD up (port 11111 open)  → crash-recovery → start engine NOW
#      - OpenD down (port closed)    → cold boot → sleep 10 min for
#        system to settle, then launch OpenD if not running
#   2. Launches OpenD via bundle ID (com.moomoo.opend) — the binary lives
#      in an AppTranslocation (quarantine) path, so a hardcoded path
#      would break after moomoo.app updates.
#   3. Activates the project venv, sets OPTIONS_HOME, starts the engine:
#        python3 scripts/oie_engine.py run --interval 60 --skip-closed
#   4. On crash, launchd restarts this wrapper (ThrottleInterval 60s);
#      since OpenD is likely still up, the fast path skips the sleep.
#
# Logs: logs/oie_launchd.log (via tee + launchd stderr/stdout redirect)
# ═══════════════════════════════════════════════════════════════
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PATH="$PROJECT_ROOT/.venv"
LOG_DIR="$PROJECT_ROOT/logs"
OPEND_BUNDLE_ID="com.moomoo.opend"
OPEND_HOST="127.0.0.1"
OPEND_PORT=11111
ENGINE_INTERVAL_MIN=60
COLD_BOOT_DELAY_SEC=600
OPEND_WAIT_ATTEMPTS=30   # 30 × 3s = 90s max wait
OPEND_WAIT_STEP_SEC=3

mkdir -p "$LOG_DIR"
LAUNCHD_LOG="$LOG_DIR/oie_launchd.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LAUNCHD_LOG"
}

# ── OpenD reachability check ─────────────────────────────────────
opend_running() {
    nc -z -w 2 "$OPEND_HOST" "$OPEND_PORT" >/dev/null 2>&1
}

# ── Launch OpenD via bundle ID + wait for it ─────────────────────
launch_opend() {
    log "OpenD not reachable on ${OPEND_HOST}:${OPEND_PORT} — launching bundle '$OPEND_BUNDLE_ID'"
    if ! open -b "$OPEND_BUNDLE_ID"; then
        log "WARNING: 'open -b $OPEND_BUNDLE_ID' failed — engine will retry its own connection"
        return 0
    fi

    for i in $(seq 1 "$OPEND_WAIT_ATTEMPTS"); do
        sleep "$OPEND_WAIT_STEP_SEC"
        if opend_running; then
            log "OpenD is up (port ${OPEND_PORT} reachable) after ~$((i * OPEND_WAIT_STEP_SEC))s"
            return 0
        fi
    done

    log "WARNING: OpenD still not reachable after $((OPEND_WAIT_ATTEMPTS * OPEND_WAIT_STEP_SEC))s — continuing anyway (engine handles its own retries)"
    return 0
}

# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

# Port-check heuristic:
#   OpenD up   → crash-recovery (or manual reload) → start engine immediately
#   OpenD down → cold boot → 10-min settle delay → ensure OpenD is running
if opend_running; then
    log "OpenD detected on port ${OPEND_PORT} — cold-boot delay skipped (fast restart path)"
else
    log "OpenD NOT detected on port ${OPEND_PORT} — cold-boot path: sleeping ${COLD_BOOT_DELAY_SEC}s"
    sleep "$COLD_BOOT_DELAY_SEC"
    launch_opend
fi

# ── Activate project + start engine ──────────────────────────────
cd "$PROJECT_ROOT" || { log "FATAL: cannot cd to $PROJECT_ROOT"; exit 1; }

if [ ! -x "$VENV_PATH/bin/python" ]; then
    log "FATAL: venv python not found at $VENV_PATH/bin/python"
    exit 1
fi

export OPTIONS_HOME="$PROJECT_ROOT"
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin:$PATH"

log "Starting OIE engine: python3 scripts/oie_engine.py run --interval ${ENGINE_INTERVAL_MIN} --skip-closed"
# -u (unbuffered): engine stdout flushes immediately to oie_launchd.log
exec "$VENV_PATH/bin/python" -u scripts/oie_engine.py run --interval "$ENGINE_INTERVAL_MIN" --skip-closed
