#!/usr/bin/env bash
# shellcheck disable=SC1091
set -euo pipefail

# ──────────────────────────────────────────────────────────────────────────────
# Discord Notetaker Diagnostics (network/firewall extended)
# Exit codes:
#   0 OK
#   1 Likely network/UDP issue
#   2 Config issue
#   3 Container deps issue
# ──────────────────────────────────────────────────────────────────────────────

# Self-syntax check
if command -v bash >/dev/null 2>&1; then
  if ! bash -n "$0" 2>/dev/null; then
    echo "This script has a syntax issue (unexpected)."; exit 2
  fi
fi

PROJECT="${PROJECT:-discord_notetaker}"
COMPOSE="${COMPOSE_FILE:-docker-compose.yml}"

BOT_SVC="${BOT_SVC:-discord-bot}"
TRANSCRIBER_SVC="${TRANSCRIBER_SVC:-transcriber}"
OLLAMA_SVC="${OLLAMA_SVC:-ollama}"

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; CYAN=$'\033[36m'; NC=$'\033[0m'
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
info() { echo -e "${CYAN}[INFO]${NC} $*"; }
ok()   { echo -e "${GREEN}[OK] ${NC} $*"; }
bad()  { echo -e "${RED}[FAIL]${NC} $*"; }

need_fix=0
net_issue=0
dep_issue=0

# 0) Basic file checks
if [[ ! -f "$COMPOSE" ]]; then
  bad "No $COMPOSE found in $(pwd)"
  exit 2
fi
if [[ ! -f ".env" ]]; then
  bad "No .env file found. Create and populate required variables."
  exit 2
fi

# 1) Parse .env vars
set -a
# shellcheck source=/dev/null
source .env
set +a

missing=()
for v in DISCORD_BOT_TOKEN DISCORD_GUILD_ID DISCORD_POST_CHANNEL_ID OLLAMA_HOST LLM_MODEL; do
  [[ -z "${!v:-}" ]] && missing+=("$v")
done
if (( ${#missing[@]} )); then
  bad "Missing required .env vars: ${missing[*]}"
  need_fix=1
fi

TRANSCRIBER_URL="${TRANSCRIBER_URL:-http://transcriber:8000}"
info "TRANSCRIBER_URL = $TRANSCRIBER_URL"
info "OLLAMA_HOST     = ${OLLAMA_HOST:-unset}"

# 2) Compose network mode & ports
netmode="$(docker compose -f "$COMPOSE" config 2>/dev/null \
  | awk "/services:/,/^volumes:/{print}" \
  | awk "/${BOT_SVC}:/{f=1} f && /network_mode:/ {print \$2; exit}" || true)"
if [[ -z "$netmode" ]]; then
  warn "Bot service network_mode not explicitly set (bridge by default). Voice UDP can fail behind NAT."
else
  info "Bot service network_mode: $netmode"
fi

if [[ "$netmode" == "host" ]]; then
  transcriber_block="$(docker compose -f "$COMPOSE" config 2>/dev/null | awk "/${TRANSCRIBER_SVC}:/,/^[a-z]/")"
  if ! grep -qE '^\s*ports:\s*$' <<<"$transcriber_block" || ! grep -qE '^\s*-\s*"?8000:8000"?' <<<"$transcriber_block"; then
    bad "Transcriber is not published on 8000 while bot is in host mode. Add: ports: [\"8000:8000\"] to ${TRANSCRIBER_SVC}."
    need_fix=1
  else
    ok "Transcriber port appears published."
  fi
  if [[ "$TRANSCRIBER_URL" != http://127.0.0.1:8000 && "$TRANSCRIBER_URL" != http://localhost:8000 ]]; then
    warn "TRANSCRIBER_URL should be http://127.0.0.1:8000 when bot uses host networking."
  fi
else
  if [[ "$TRANSCRIBER_URL" != http://transcriber:8000 ]]; then
    warn "Bridge mode typically uses TRANSCRIBER_URL=http://transcriber:8000 (yours is $TRANSCRIBER_URL)."
  fi
fi

# 3) Find running containers
BOT_CTN="$(docker ps --format '{{.Names}}' | grep -E "^${PROJECT}-${BOT_SVC}-[0-9]+$" || true)"
TRANSCRIBER_CTN="$(docker ps --format '{{.Names}}' | grep -E "^${PROJECT}-${TRANSCRIBER_SVC}-[0-9]+$" || true)"
OLLAMA_CTN="$(docker ps --format '{{.Names}}' | grep -E "^${PROJECT}-${OLLAMA_SVC}-[0-9]+$" || true)"

[[ -n "$BOT_CTN" ]] && ok "Bot container: $BOT_CTN" || { bad "Bot container not running."; need_fix=1; }
[[ -n "$TRANSCRIBER_CTN" ]] && ok "Transcriber container: $TRANSCRIBER_CTN" || { bad "Transcriber container not running."; need_fix=1; }
[[ -n "$OLLAMA_CTN" ]] && ok "Ollama container: $OLLAMA_CTN" || { bad "Ollama container not running."; need_fix=1; }

# Early exit if core services down
if (( need_fix )); then
  echo "Fix the above and re-run."; exit 2
fi

# 4) Inside the bot: py-cord version & opus
if [[ -n "$BOT_CTN" ]]; then
  info "Checking py-cord / opus inside bot…"
  docker exec -i "$BOT_CTN" python - <<'PY' || true
import sys
print("python:", sys.version)
try:
    import discord
    print("discord (py-cord) version:", discord.__version__)
    print("has sinks:", hasattr(discord, "sinks"))
    import discord.opus as opus
    print("opus loaded:", opus.is_loaded())
except Exception as e:
    print("error:", e)
PY
  status=$?
  if (( status != 0 )); then
    bad "Unable to run Python inside bot container."
    dep_issue=1
  fi

  info "Attempting to load opus (libopus0)…"
  docker exec -i "$BOT_CTN" python - <<'PY' || true
import discord.opus as opus
try:
    if not opus.is_loaded():
        opus.load_opus('libopus.so.0')
    print("opus loaded:", opus.is_loaded())
except Exception as e:
    print("opus load error:", e)
PY
fi

# 5) Transcriber reachability (from bot context)
if [[ -n "$BOT_CTN" ]]; then
  info "Probing transcriber from bot (${TRANSCRIBER_URL})…"
  docker exec -i "$BOT_CTN" python - <<PY || true
import os, urllib.request
u=os.environ.get("TRANSCRIBER_URL","${TRANSCRIBER_URL}")
try:
    with urllib.request.urlopen(u, timeout=3) as r:
        print("HTTP OK:", r.status)
except Exception as e:
    print("HTTP probe error:", e)
PY
fi

# 6) Ollama reachability (from bot context)
if [[ -n "$BOT_CTN" ]]; then
  info "Probing Ollama from bot (${OLLAMA_HOST:-unset})…"
  docker exec -i "$BOT_CTN" python - <<PY || true
import os, urllib.request
base=os.environ.get("OLLAMA_HOST","${OLLAMA_HOST}")
try:
    with urllib.request.urlopen(base + "/api/tags", timeout=3) as r:
        print("HTTP OK:", r.status)
except Exception as e:
    print("HTTP probe error:", e)
PY
fi

# 7) Docker network mode of the running bot container
if [[ -n "$BOT_CTN" ]]; then
  runmode="$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$BOT_CTN" 2>/dev/null || true)"
  info "Running bot NetworkMode: ${runmode:-unknown}"
  if [[ "$runmode" != "host" ]]; then
    warn "Bot is NOT in host network. Voice UDP often fails behind Docker NAT."
    net_issue=1
  fi
fi

# 8) Host: UDP sanity checks
info "UDP quick checks from host…"
if command -v nc >/dev/null 2>&1; then
  if nc -vz -u 1.1.1.1 53 -w 2 >/dev/null 2>&1; then
    ok "Host UDP out (1.1.1.1:53) looks OK."
  else
    warn "Host UDP test to 1.1.1.1:53 failed. Firewall/NAT or exit-node could be blocking."
    net_issue=1
  fi
else
  warn "nc (netcat) not installed; skipping UDP smoke test."
fi

# 9) DNS and HTTPS reachability to discord.com
info "DNS/HTTPS reachability for discord.com…"
if command -v dig >/dev/null 2>&1; then
  dig +short discord.com || true
elif command -v getent >/dev/null 2>&1; then
  getent hosts discord.com || true
else
  warn "No dig/getent to test DNS resolution."
fi
if command -v curl >/dev/null 2>&1; then
  if curl -Is https://discord.com >/dev/null 2>&1; then
    ok "HTTPS to discord.com reachable."
  else
    warn "HTTPS to discord.com failed. Check DNS or outbound HTTPS from host."
    net_issue=1
  fi
else
  warn "curl not installed; skipping HTTPS probe."
fi

# 10) IPv6 posture (Discord voice can misbehave with broken v6)
info "Checking IPv6 posture…"
v6def="$(ip -6 route show default 2>/dev/null || true)"
if [[ -n "$v6def" ]]; then
  if sysctl net.ipv6.conf.all.disable_ipv6 2>/dev/null | grep -q '= 1'; then
    warn "IPv6 default route present, but sysctl says IPv6 disabled. Consider fully disabling IPv6 or removing the v6 default route."
  else
    info "IPv6 default route present."
  fi
else
  info "No IPv6 default route detected (that’s fine)."
fi

# 11) Tailscale status hints (if installed)
if command -v tailscale >/dev/null 2>&1; then
  info "Tailscale detected. Checking Exit Node use…"
  if tailscale status 2>/dev/null | grep -qi "Exit node:"; then
    warn "This machine appears to be using a Tailscale Exit Node. Outbound UDP to Discord may route oddly."
    net_issue=1
  fi
  if tailscale status 2>/dev/null | grep -qi "Subnets:"; then
    info "Tailscale subnet routes present."
  fi
else
  info "Tailscale not detected (ok)."
fi

# 12) UFW (if present)
if command -v ufw >/dev/null 2>&1; then
  ufw_status="$(ufw status verbose 2>/dev/null || true)"
  if grep -qi "Status: active" <<<"$ufw_status"; then
    info "UFW is active:"
    echo "$ufw_status" | sed 's/^/  /'
    if echo "$ufw_status" | grep -qi "Default: deny \(incoming\|outgoing\)"; then
      warn "UFW default policy is deny for some directions. Ensure outbound UDP is allowed."
      net_issue=1
    fi
  else
    info "UFW not active."
  fi
fi

# 13) nftables / iptables posture
if command -v nft >/dev/null 2>&1; then
  info "Inspecting nftables for UDP output drops…"
  nft list ruleset 2>/dev/null | grep -i "udp" | sed 's/^/  /' || true
fi
if command -v iptables >/dev/null 2>&1; then
  info "iptables OUTPUT policy:"
  iptables -S OUTPUT 2>/dev/null | sed 's/^/  /' || true
fi

# 14) Slash-command interaction safety: defer()?
if [[ -n "$BOT_CTN" ]]; then
  info "Checking bot.py for immediate ctx.defer() in startnotes…"
  if docker exec -i "$BOT_CTN" grep -n "async def startnotes" /app/bot.py >/dev/null 2>&1; then
    if docker exec -i "$BOT_CTN" grep -n "await ctx.defer" /app/bot.py >/dev/null 2>&1; then
      ok "startnotes uses ctx.defer() (good)."
    else
      warn "startnotes does not call ctx.defer(); you may hit 'Unknown interaction' on slow connects."
      need_fix=1
      echo "  Suggested patch:"
      echo "    • Add 'await ctx.defer(ephemeral=True)' as the first line inside startnotes()."
    fi
  fi
fi

echo
echo "──────────────────────────── Summary ────────────────────────────"
(( need_fix == 0 )) && ok "Config looks reasonable." || bad "Config issues detected (see above)."
(( dep_issue == 0 )) && ok "Python/voice deps look okay." || bad "Container dependency issue(s) detected."
if (( net_issue == 1 )); then
  bad "Likely NETWORK/UDP issue impacting Discord voice."
  echo "Remediation:"
  echo "  • Keep bot in network_mode: host (or try switching host↔bridge if already host)."
  echo "  • If host: TRANSCRIBER_URL=http://127.0.0.1:8000 and expose transcriber: '8000:8000'."
  echo "  • Set a Voice Channel Region Override (not Auto) in Discord."
  echo "  • Disable Tailscale Exit Node / full-tunnel while using Discord voice."
  echo "  • Ensure outbound UDP allowed in UFW/nftables/iptables."
  echo "  • Consider disabling IPv6 if your upstream has partial/broken IPv6."
else
  ok "No obvious network blockers found."
fi
echo "────────────────────────────────────────────────────────────────"

# Exit code preference: config > deps > network
if (( need_fix != 0 )); then exit 2; fi
if (( dep_issue != 0 )); then exit 3; fi
if (( net_issue != 0 )); then exit 1; fi
exit 0
