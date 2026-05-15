#!/usr/bin/env bash
# healthcheck.sh — checks and optionally repairs bot, tunnel, and webhook
#
# Usage:
#   bash healthcheck.sh              — check only, report issues
#   bash healthcheck.sh --fix        — check and auto-repair all issues
#   bash healthcheck.sh --restart    — force-restart everything (bot + webhook)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
BOT_PORT=8080
BOT_LOG="/tmp/bot.log"
TUNNEL_LOG="/tmp/cloudflared-bot.log"

MODE="${1:-}"

# ── Load .env ────────────────────────────────────────────────────────────────
[[ -f "$ENV_FILE" ]] || { echo "❌  .env not found at $ENV_FILE"; exit 1; }
set -a; source "$ENV_FILE"; set +a
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_WEBHOOK_URL="${TELEGRAM_WEBHOOK_URL:-}"

# ── Colours ──────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✅  $*${NC}"; }
fail() { echo -e "${RED}❌  $*${NC}"; }
warn() { echo -e "${YELLOW}⚠️   $*${NC}"; }
info() { echo -e "    $*"; }
fixing() { echo -e "${CYAN}🔧  $*${NC}"; }

ISSUES=0

# ── Helpers ──────────────────────────────────────────────────────────────────
_kill_bot() {
    local pids
    pids=$(ps aux | grep "uvicorn app.main" | grep -v grep | awk '{print $2}' || true)
    [[ -n "$pids" ]] && kill -9 $pids 2>/dev/null && sleep 2 || true
}

_start_bot() {
    cd "$SCRIPT_DIR"
    nohup uvicorn app.main:fastapi_app --port "$BOT_PORT" > "$BOT_LOG" 2>&1 &
    sleep 10
}

_kill_tunnel() {
    local pids
    pids=$(ps aux | grep "cloudflared tunnel" | grep "$BOT_PORT" | grep -v grep | awk '{print $2}' || true)
    [[ -n "$pids" ]] && kill -9 $pids 2>/dev/null && sleep 2 || true
}

_start_tunnel() {
    nohup cloudflared tunnel --url "http://localhost:$BOT_PORT" --no-autoupdate > "$TUNNEL_LOG" 2>&1 &
    sleep 8
    # Extract the new URL from the log
    grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | tail -1 || echo ""
}

_register_webhook() {
    local url="$1"
    curl -sf "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook?url=${url}/webhook&allowed_updates=%5B%22message%22%5D" > /dev/null
}

_update_env() {
    local key="$1" val="$2"
    sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
}

# ── Handle --restart (force restart everything) ───────────────────────────────
if [[ "$MODE" == "--restart" ]]; then
    echo ""
    echo "══════════════════════════════════════"
    echo "  Restarting all services"
    echo "══════════════════════════════════════"
    echo ""

    fixing "Stopping bot..."
    _kill_bot
    ok "Bot stopped"

    fixing "Starting bot..."
    _start_bot
    BOT_PID=$(ps aux | grep "uvicorn app.main" | grep -v grep | awk '{print $2}' | head -1)
    [[ -n "$BOT_PID" ]] && ok "Bot started (PID $BOT_PID)" || { fail "Bot failed to start — check $BOT_LOG"; exit 1; }

    fixing "Stopping tunnel..."
    _kill_tunnel
    ok "Tunnel stopped"

    fixing "Starting tunnel..."
    NEW_URL=$(_start_tunnel)
    TUNNEL_PID=$(ps aux | grep "cloudflared tunnel" | grep "$BOT_PORT" | grep -v grep | awk '{print $2}' | head -1)
    if [[ -n "$TUNNEL_PID" && -n "$NEW_URL" ]]; then
        ok "Tunnel started (PID $TUNNEL_PID)"
        info "URL: $NEW_URL"
        if [[ "$NEW_URL" != "$TELEGRAM_WEBHOOK_URL" ]]; then
            fixing "Updating TELEGRAM_WEBHOOK_URL in .env..."
            _update_env "TELEGRAM_WEBHOOK_URL" "$NEW_URL"
            TELEGRAM_WEBHOOK_URL="$NEW_URL"
            ok ".env updated"
        fi
    else
        fail "Tunnel failed to start — check $TUNNEL_LOG"
    fi

    if [[ -n "$TELEGRAM_BOT_TOKEN" && -n "$TELEGRAM_WEBHOOK_URL" ]]; then
        fixing "Registering webhook..."
        _register_webhook "$TELEGRAM_WEBHOOK_URL"
        ok "Webhook registered: ${TELEGRAM_WEBHOOK_URL}/webhook"
    fi

    echo ""
    echo "══════════════════════════════════════"
    ok "All services restarted"
    echo "══════════════════════════════════════"
    echo ""

    exec bash "$0"   # run health check to confirm
    exit 0
fi

FIX_MODE=false
[[ "$MODE" == "--fix" ]] && FIX_MODE=true

# ═══════════════════════════════════════════════════════════════════════════════
echo ""
echo "══════════════════════════════════════"
echo "  Multimodal RAG Bot — Health Check"
echo "══════════════════════════════════════"
echo ""

# ── 1. Bot process ────────────────────────────────────────────────────────────
echo "▶ Bot process"
BOT_PID=$(ps aux | grep "uvicorn app.main" | grep -v grep | awk '{print $2}' | head -1)
if [[ -n "$BOT_PID" ]]; then
    ok "Running (PID $BOT_PID)"
else
    fail "Not running"
    ((ISSUES++))
    if $FIX_MODE; then
        fixing "Starting bot..."
        _start_bot
        BOT_PID=$(ps aux | grep "uvicorn app.main" | grep -v grep | awk '{print $2}' | head -1)
        if [[ -n "$BOT_PID" ]]; then
            ok "Started (PID $BOT_PID)"
            ((ISSUES--))
        else
            fail "Failed to start — check $BOT_LOG"
        fi
    fi
fi
echo ""

# ── 2. Bot HTTP health endpoint ───────────────────────────────────────────────
echo "▶ Bot HTTP (localhost:$BOT_PORT)"
HTTP_RESP=$(curl -sf --max-time 5 "http://localhost:$BOT_PORT/health" 2>/dev/null || echo "")
if [[ -n "$HTTP_RESP" ]]; then
    STATUS=$(echo "$HTTP_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','?'))" 2>/dev/null || echo "?")
    STORE=$(echo  "$HTTP_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('store','?'))"  2>/dev/null || echo "?")
    ok "Responding (status=$STATUS)"
    info "Store: $STORE"
else
    fail "Not responding on port $BOT_PORT"
    ((ISSUES++))
    if $FIX_MODE; then
        fixing "Restarting bot (process exists but HTTP is dead)..."
        _kill_bot
        _start_bot
        HTTP_RESP=$(curl -sf --max-time 5 "http://localhost:$BOT_PORT/health" 2>/dev/null || echo "")
        if [[ -n "$HTTP_RESP" ]]; then
            ok "Bot restarted and responding"
            ((ISSUES--))
        else
            fail "Still not responding — check $BOT_LOG"
        fi
    fi
fi
echo ""

# ── 3. Cloudflared tunnel process ─────────────────────────────────────────────
echo "▶ Cloudflared tunnel"
TUNNEL_PID=$(ps aux | grep "cloudflared tunnel" | grep "$BOT_PORT" | grep -v grep | awk '{print $2}' | head -1)
if [[ -n "$TUNNEL_PID" ]]; then
    ok "Running (PID $TUNNEL_PID)"
    TUNNEL_URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | tail -1 || echo "")
    [[ -n "$TUNNEL_URL" ]] && info "URL: $TUNNEL_URL"
    if [[ -n "$TUNNEL_URL" && "$TUNNEL_URL" != "$TELEGRAM_WEBHOOK_URL" ]]; then
        warn "Tunnel URL differs from .env — webhook may be broken"
        info "  .env has:   $TELEGRAM_WEBHOOK_URL"
        info "  Tunnel has: $TUNNEL_URL"
        ((ISSUES++))
        if $FIX_MODE; then
            fixing "Updating .env and re-registering webhook..."
            _update_env "TELEGRAM_WEBHOOK_URL" "$TUNNEL_URL"
            TELEGRAM_WEBHOOK_URL="$TUNNEL_URL"
            _register_webhook "$TELEGRAM_WEBHOOK_URL"
            ok ".env updated and webhook re-registered: ${TELEGRAM_WEBHOOK_URL}/webhook"
            ((ISSUES--))
        fi
    fi
else
    fail "Not running"
    ((ISSUES++))
    if $FIX_MODE; then
        fixing "Starting cloudflared tunnel..."
        NEW_URL=$(_start_tunnel)
        TUNNEL_PID=$(ps aux | grep "cloudflared tunnel" | grep "$BOT_PORT" | grep -v grep | awk '{print $2}' | head -1)
        if [[ -n "$TUNNEL_PID" ]]; then
            ok "Started (PID $TUNNEL_PID)"
            info "URL: $NEW_URL"
            ((ISSUES--))
            if [[ -n "$NEW_URL" && "$NEW_URL" != "$TELEGRAM_WEBHOOK_URL" ]]; then
                fixing "Updating .env with new tunnel URL..."
                _update_env "TELEGRAM_WEBHOOK_URL" "$NEW_URL"
                TELEGRAM_WEBHOOK_URL="$NEW_URL"
                ok ".env updated: $NEW_URL"
            fi
        else
            fail "Failed to start tunnel — is cloudflared installed?"
        fi
    fi
fi
echo ""

# ── 4. Tunnel reachability ─────────────────────────────────────────────────────
echo "▶ Tunnel reachability"
if [[ -n "$TELEGRAM_WEBHOOK_URL" ]]; then
    TUNNEL_HEALTH=$(curl -sf --max-time 10 "$TELEGRAM_WEBHOOK_URL/health" 2>/dev/null || echo "")
    if [[ -n "$TUNNEL_HEALTH" ]]; then
        ok "Reachable ($TELEGRAM_WEBHOOK_URL)"
    else
        fail "Cannot reach $TELEGRAM_WEBHOOK_URL/health"
        ((ISSUES++))
        warn "Tunnel URL may have changed — run with --restart to reset everything"
    fi
else
    warn "TELEGRAM_WEBHOOK_URL not set in .env"
    ((ISSUES++))
fi
echo ""

# ── 5. Telegram webhook ────────────────────────────────────────────────────────
echo "▶ Telegram webhook"
if [[ -z "$TELEGRAM_BOT_TOKEN" ]]; then
    warn "TELEGRAM_BOT_TOKEN not set — skipping"
else
    WH_RESP=$(curl -sf --max-time 10 \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo" 2>/dev/null || echo "")
    if [[ -z "$WH_RESP" ]]; then
        fail "Could not reach Telegram API"
        ((ISSUES++))
    else
        WH_URL=$(echo     "$WH_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['url'])" 2>/dev/null || echo "")
        WH_PENDING=$(echo "$WH_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['result'].get('pending_update_count',0))" 2>/dev/null || echo "0")
        WH_ERROR=$(echo   "$WH_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['result'].get('last_error_message',''))" 2>/dev/null || echo "")

        EXPECTED_URL="${TELEGRAM_WEBHOOK_URL%/}/webhook"

        if [[ -z "$WH_URL" ]]; then
            fail "Webhook not registered (URL is empty)"
            ((ISSUES++))
            if $FIX_MODE; then
                fixing "Registering webhook..."
                _register_webhook "$TELEGRAM_WEBHOOK_URL"
                ok "Webhook registered: $EXPECTED_URL"
                ((ISSUES--))
            else
                info "Run with --fix to register automatically"
            fi
        elif [[ "$WH_URL" != "$EXPECTED_URL" ]]; then
            warn "Webhook URL mismatch"
            info "  Registered: $WH_URL"
            info "  Expected:   $EXPECTED_URL"
            ((ISSUES++))
            if $FIX_MODE; then
                fixing "Re-registering webhook..."
                _register_webhook "$TELEGRAM_WEBHOOK_URL"
                ok "Webhook updated: $EXPECTED_URL"
                ((ISSUES--))
            fi
        else
            ok "Registered: $WH_URL"
        fi

        [[ "$WH_PENDING" -gt 5 ]] && warn "$WH_PENDING pending update(s) queued"
        [[ -n "$WH_ERROR" ]]      && warn "Last Telegram error: $WH_ERROR"
    fi
fi
echo ""

# ── 6. Gemini store ────────────────────────────────────────────────────────────
echo "▶ Gemini File Search Store"
STORE_RESP=$(curl -sf --max-time 10 "http://localhost:$BOT_PORT/store/info" 2>/dev/null || echo "")
if [[ -n "$STORE_RESP" ]]; then
    DOC_COUNT=$(echo "$STORE_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('document_count',0))" 2>/dev/null || echo "?")
    STORE_NAME=$(echo "$STORE_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('store_name','?'))" 2>/dev/null || echo "?")
    ok "$DOC_COUNT document(s) indexed"
    info "Store: $STORE_NAME"
else
    warn "Could not query store info (bot may not be running)"
fi
echo ""

# ── Summary ────────────────────────────────────────────────────────────────────
echo "══════════════════════════════════════"
if [[ $ISSUES -eq 0 ]]; then
    ok "All checks passed"
else
    fail "$ISSUES issue(s) found"
    if ! $FIX_MODE; then
        echo ""
        echo -e "  ${CYAN}bash healthcheck.sh --fix${NC}      repair issues automatically"
        echo -e "  ${CYAN}bash healthcheck.sh --restart${NC}  force-restart everything"
    fi
fi
echo "══════════════════════════════════════"
echo ""
exit $ISSUES
