#!/usr/bin/env bash
# Monitor Discord voice connect flaps and optionally collect packets + TLS probe
#
# - Tails docker logs of the bot container and counts 4006 cycles.
# - When 3+ flaps occur within 120s, triggers:
#   * openssl s_client handshake to the last /voice_endpoint host
#   * optional tcpdump capture for 45s (requires sudo/root)
#
# Usage:
#   ./voice_monitor.sh            # autodetect container
#   sudo ./voice_monitor.sh cap   # also do a 45s tcpdump capture

set -euo pipefail

PROJECT=${PROJECT:-discord_notetaker}
BOT_SVC=${BOT_SVC:-discord-bot}
CAPTURE=${1:-}

RED='\033[31m'; YEL='\033[33m'; GRN='\033[32m'; BLU='\033[34m'; NC='\033[0m'
log() { echo -e "${BLU}[voice-monitor]${NC} $*"; }
warn() { echo -e "${YEL}[warn]${NC} $*"; }
err() { echo -e "${RED}[error]${NC} $*"; }
ok()  { echo -e "${GRN}[ok]${NC} $*"; }

BOT_CTN=$(docker ps --format '{{.Names}}' | grep -E "^${PROJECT}-${BOT_SVC}-[0-9]+$" || true)
if [[ -z "$BOT_CTN" ]]; then
  err "Bot container not found. Ensure containers are running."
  exit 1
fi
ok "Watching container: $BOT_CTN"

declare -a flap_times=()

last_endpoint_host=""
last_endpoint_port=2087

get_last_endpoint() {
  local json_file="./data/voice_last.json"
  if [[ -f "$json_file" ]]; then
    local ep=""
    if command -v jq >/dev/null 2>&1; then
      ep=$(jq -r '.endpoint // empty' "$json_file" 2>/dev/null || true)
    else
      ep=$(python3 - <<'PY' 2>/dev/null || true
import json,sys
try:
    d=json.load(open(sys.argv[1],'r'))
    print(d.get('endpoint') or '')
except Exception:
    pass
PY
"$json_file")
    fi
    if [[ -n "$ep" ]]; then
      last_endpoint_host=${ep%%:*}
      last_endpoint_port=${ep##*:}
    fi
  fi
}

do_tls_probe() {
  get_last_endpoint
  if [[ -z "$last_endpoint_host" ]]; then
    warn "No endpoint recorded yet in data/voice_last.json (run /joinvoice once)."
    return
  fi
  log "TLS probe: ${last_endpoint_host}:${last_endpoint_port}"
  if command -v openssl >/dev/null 2>&1; then
    # -brief to reduce noise; ignore EOF
    openssl s_client -connect "${last_endpoint_host}:${last_endpoint_port}" -servername "$last_endpoint_host" -tls1_2 -brief < /dev/null || true
  else
    warn "openssl not installed; skipping TLS probe."
  fi
}

do_tcpdump() {
  get_last_endpoint
  if [[ -z "$last_endpoint_host" ]]; then
    warn "No endpoint recorded yet in data/voice_last.json; skipping capture."
    return
  fi
  if [[ "$(id -u)" != 0 ]]; then
    warn "tcpdump capture requires root. Re-run with sudo if desired."
    return
  fi
  local ip
  if command -v getent >/dev/null 2>&1; then
    ip=$(getent hosts "$last_endpoint_host" | awk '{print $1; exit}')
  else
    ip=$(dig +short "$last_endpoint_host" | head -n1)
  fi
  if [[ -z "$ip" ]]; then
    warn "Could not resolve ${last_endpoint_host}; skipping capture."
    return
  fi
  local out="/tmp/discord_voice_$(date +%s).pcap"
  log "Capturing 45s of traffic to ${ip}:2087 + UDP RTP to $out"
  timeout 45s tcpdump -ni any "(host ${ip} and tcp port ${last_endpoint_port}) or (udp portrange 50000-65535)" -w "$out" || true
  ok "Saved capture: $out"
}

log "Looking for handshake/close events… (press Ctrl+C to stop)"
docker logs -f "$BOT_CTN" 2>&1 | while IFS= read -r line; do
  # Normalize timeline
  now=$(date +%s)
  # Track endpoint if printed in logs too
  if grep -q "VOICE_SERVER_UPDATE" <<<"$line"; then
    get_last_endpoint
  fi

  if grep -q "Voice handshake complete" <<<"$line"; then
    ok "Handshake complete $(date -d @${now} +%T). Endpoint: ${last_endpoint_host:-unknown}"
  fi

  if grep -q "WSMessage(type=<WSMsgType.CLOSE: 8>, data=4006" <<<"$line"; then
    flap_times+=("$now")
    # drop entries older than 120s
    tmp=()
    for t in "${flap_times[@]}"; do
      (( now - t <= 120 )) && tmp+=("$t")
    done
    flap_times=("${tmp[@]}")
    warn "Voice WS closed 4006 at $(date -d @${now} +%T). Recent flaps: ${#flap_times[@]}"
    if (( ${#flap_times[@]} >= 3 )); then
      warn "3+ flaps within 120s — running TLS probe${CAPTURE:+ + capture}"
      do_tls_probe
      if [[ "$CAPTURE" == "cap" ]]; then
        do_tcpdump
      fi
      flap_times=()
    fi
  fi
done
