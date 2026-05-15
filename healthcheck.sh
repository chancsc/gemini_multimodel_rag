#!/usr/bin/env bash
# healthcheck.sh — checks bot process, cloudflared tunnel, and Telegram webhook
# Usage: bash healthcheck.sh [--fix]   (--fix attempts to repair issues found)

set -euo pipefail

# ── Config ──────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
BOT_PORT=8080
FIX_MODE=false
[[ "${1:-}" == "--fix" ]] && FIX_MODE=true

# Load .env
if [[ -f "$ENV_FILE" ]]; then
    set -a; source "$ENV_FILE"; set +a
else
    echo "❌  .env not found at $ENV_FILE"; exit 1
fi

TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_WEBHOOK_URL="${TELEGRAM_WEBHOOK_URL:-}"

# ── Colours ──────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✅  $*${NC}"; }
fail() { echo -e "${RED}❌  $*${NC}"; }
warn() { echo -e "${YELLOW}⚠️   $*${NC}"; }
info() { echo -e "    $*"; }

ISSUES=0

echo ""
echo "══════════════════════════════════════"
echo "  Multimodal RAG Bot — Health Check"
echo "══════════════════════════════════════"
echo ""

# ── 1. Bot process ───────────────────────────────────────────────────────────
echo "▶ Bot process"
BOT_PID=$(ps aux | grep "uvicorn app.main" | grep -v grep | awk '{print $2}' | head -1)
if [[ -n "$BOT_PID" ]]; then
    ok "Running (PID $BOT_PID)"
else
    fail "Not running"
    ((ISSUES++))
    if $FIX_MODE; then
        warn "Starting bot..."
        cd "$SCRIPT_DIR"
        nohup uvicorn app.main:fastapi_app --port $BOT_PORT > /tmp/bot.log 2>&1 &
        sleep 8
        BOT_PID=$(ps aux | grep "uvicorn app.main" | grep -v grep | awk '{print $2}' | head -1)
        [[ -n "$BOT_PID" ]] && ok "Started (PID $BOT_PID)" || fail "Failed to start — check /tmp/bot.log"
    fi
fi
echo ""

# ── 2. Bot HTTP health endpoint ──────────────────────────────────────────────
echo "▶ Bot HTTP (localhost:$BOT_PORT)"
HTTP_RESP=$(curl -sf --max-time 5 "http://localhost:$BOT_PORT/health" 2>/dev/null || echo "")
if [[ -n "$HTTP_RESP" ]]; then
    STATUS=$(echo "$HTTP_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','?'))" 2>/dev/null || echo "?")
    STORE=$(echo "$HTTP_RESP"  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('store','?'))"  2>/dev/null || echo "?")
    ok "Responding (status=$STATUS)"
    info "Store: $STORE"
else
    fail "Not responding on port $BOT_PORT"
    ((ISSUES++))
fi
echo ""

# ── 3. Cloudflared tunnel ────────────────────────────────────────────────────
echo "▶ Cloudflared tunnel"
TUNNEL_PID=$(ps aux | grep "cloudflared tunnel" | grep "$BOT_PORT" | grep -v grep | awk '{print $2}' | head -1)
if [[ -n "$TUNNEL_PID" ]]; then
    ok "Running (PID $TUNNEL_PID)"
    # Find the active tunnel URL from logs
    TUNNEL_LOG=$(ls -t /tmp/cloudflared*.log 2>/dev/null | head -1 || echo "")
    if [[ -n "$TUNNEL_LOG" ]]; then
        TUNNEL_URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | tail -1 || echo "")
        if [[ -n "$TUNNEL_URL" ]]; then
            info "URL: $TUNNEL_URL"
            if [[ "$TUNNEL_URL" != "$TELEGRAM_WEBHOOK_URL" ]]; then
                warn "Tunnel URL differs from TELEGRAM_WEBHOOK_URL in .env"
                info "  .env has:   $TELEGRAM_WEBHOOK_URL"
                info "  Tunnel has: $TUNNEL_URL"
            fi
        fi
    fi
else
    fail "Not running (no cloudflared process on port $BOT_PORT)"
    ((ISSUES++))
    warn "Start with: cloudflared tunnel --url http://localhost:$BOT_PORT --no-autoupdate > /tmp/cloudflared-bot.log 2>&1 &"
fi
echo ""

# ── 4. Tunnel reachability ───────────────────────────────────────────────────
echo "▶ Tunnel reachability"
if [[ -n "$TELEGRAM_WEBHOOK_URL" ]]; then
    TUNNEL_HEALTH=$(curl -sf --max-time 10 "$TELEGRAM_WEBHOOK_URL/health" 2>/dev/null || echo "")
    if [[ -n "$TUNNEL_HEALTH" ]]; then
        ok "Reachable ($TELEGRAM_WEBHOOK_URL)"
    else
        fail "Cannot reach $TELEGRAM_WEBHOOK_URL/health"
        ((ISSUES++))
        warn "Tunnel may have changed URL — update TELEGRAM_WEBHOOK_URL in .env and re-register webhook"
    fi
else
    warn "TELEGRAM_WEBHOOK_URL not set in .env"
    ((ISSUES++))
fi
echo ""

# ── 5. Telegram webhook ──────────────────────────────────────────────────────
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
        WH_URL=$(echo "$WH_RESP"     | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['url'])" 2>/dev/null || echo "")
        WH_PENDING=$(echo "$WH_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['result'].get('pending_update_count',0))" 2>/dev/null || echo "0")
        WH_ERROR=$(echo "$WH_RESP"   | python3 -c "import sys,json; print(json.load(sys.stdin)['result'].get('last_error_message',''))" 2>/dev/null || echo "")

        EXPECTED_URL="${TELEGRAM_WEBHOOK_URL%/}/webhook"

        if [[ -z "$WH_URL" ]]; then
            fail "Webhook not registered (URL is empty)"
            ((ISSUES++))
            if $FIX_MODE; then
                warn "Registering webhook..."
                curl -sf "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook?url=${EXPECTED_URL}&allowed_updates=%5B%22message%22%5D" > /dev/null
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
                warn "Re-registering webhook..."
                curl -sf "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook?url=${EXPECTED_URL}&allowed_updates=%5B%22message%22%5D" > /dev/null
                ok "Webhook updated: $EXPECTED_URL"
                ((ISSUES--))
            fi
        else
            ok "Registered: $WH_URL"
        fi

        [[ "$WH_PENDING" -gt 0 ]] && warn "$WH_PENDING pending update(s) queued"
        [[ -n "$WH_ERROR" ]]      && warn "Last error: $WH_ERROR"
    fi
fi
echo ""

# ── 6. Gemini store ──────────────────────────────────────────────────────────
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

# ── Summary ──────────────────────────────────────────────────────────────────
echo "══════════════════════════════════════"
if [[ $ISSUES -eq 0 ]]; then
    ok "All checks passed"
else
    fail "$ISSUES issue(s) found"
    if ! $FIX_MODE; then
        echo -e "    Run ${YELLOW}bash healthcheck.sh --fix${NC} to attempt auto-repair"
    fi
fi
echo "══════════════════════════════════════"
echo ""
exit $ISSUES
