#!/usr/bin/env bash
# shellcheck disable=SC1091
set -euo pipefail

# Voice Handshake Diagnostics (host + container)
# Runs network and system checks relevant to Discord voice connect timeouts.
# Non‑interactive and non‑destructive (does not change sysctls or firewall).
#
# Exit codes:
#   0 OK
#   1 Likely network/UDP issue
#   2 Config/compose issue

PROJECT="${PROJECT:-discord_notetaker}"
COMPOSE="${COMPOSE_FILE:-docker-compose.yml}"
BOT_SVC="${BOT_SVC:-discord-bot}"

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; CYAN=$'\033[36m'; NC=$'\033[0m'
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
info() { echo -e "${CYAN}[INFO]${NC} $*"; }
ok()   { echo -e "${GREEN}[OK] ${NC} $*"; }
bad()  { echo -e "${RED}[FAIL]${NC} $*"; }

need_fix=0
net_issue=0

if [[ ! -f "$COMPOSE" ]]; then
  bad "No $COMPOSE found in $(pwd)"; exit 2
fi
if [[ ! -f .env ]]; then
  bad "No .env present. Copy .env.example to .env and fill it."; exit 2
fi

set -a; source .env; set +a

# Resolve running container name
BOT_CTN="$(docker ps --format '{{.Names}}' | grep -E "^${PROJECT}-${BOT_SVC}-[0-9]+$" || true)"
[[ -n "$BOT_CTN" ]] && ok "Bot container: $BOT_CTN" || { bad "Bot container not running."; need_fix=1; }

# Check bot NetworkMode
if [[ -n "$BOT_CTN" ]]; then
  runmode="$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$BOT_CTN" 2>/dev/null || true)"
  info "Bot NetworkMode: ${runmode:-unknown}"
  if [[ "$runmode" != host ]]; then
    warn "Bot is NOT in host network. Voice UDP often fails behind Docker NAT."
    net_issue=1
  fi
fi

# DNS + HTTPS to discord.com
info "DNS/HTTPS reachability for discord.com…"
if command -v dig >/dev/null 2>&1; then
  dig +short discord.com || true
elif command -v getent >/dev/null 2>&1; then
  getent hosts discord.com || true
else
  warn "No dig/getent to show DNS resolution."
fi
if command -v curl >/dev/null 2>&1; then
  if curl -Is https://discord.com >/dev/null 2>&1; then
    ok "HTTPS to discord.com reachable."
  else
    warn "HTTPS to discord.com failed. Check outbound HTTPS/DNS."
    net_issue=1
  fi
fi

# UDP smoketests (host)
info "UDP smoketest from host…"
if command -v nc >/dev/null 2>&1; then
  if nc -vz -u 1.1.1.1 53 -w 2 >/dev/null 2>&1; then
    ok "Host UDP out (1.1.1.1:53) looks OK."
  else
    warn "Host UDP to 1.1.1.1:53 failed. Check firewall/NAT/overlay."
    net_issue=1
  fi
else
  warn "nc not found; skipping quick UDP smoketest."
fi

# STUN reflection from host
info "STUN UDP reflection from host…"
python3 - <<'PY' 2>/dev/null || true
import socket, os, struct
def stun_binding(host='stun.l.google.com', port=19302, timeout=3):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    tid = os.urandom(12)
    msg = struct.pack('!HHI12s', 0x0001, 0, 0x2112A442, tid)
    s.sendto(msg, (host, port))
    try:
        data, _ = s.recvfrom(2048)
    except socket.timeout:
        print('[WARN] Host: No STUN response')
    else:
        print('[OK ] Host: STUN response bytes =', len(data))
stun_binding()
PY

# STUN reflection from inside bot container
if [[ -n "$BOT_CTN" ]]; then
  info "STUN UDP reflection from bot container…"
  docker exec -i "$BOT_CTN" python - <<'PY' 2>/dev/null || true
import socket, os, struct
def stun_binding(host='stun.l.google.com', port=19302, timeout=3):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    tid = os.urandom(12)
    msg = struct.pack('!HHI12s', 0x0001, 0, 0x2112A442, tid)
    s.sendto(msg, (host, port))
    try:
        data, _ = s.recvfrom(2048)
    except socket.timeout:
        print('Bot: No STUN response')
    else:
        print('Bot: STUN response bytes =', len(data))
stun_binding()
PY
fi

# IPv6 posture
info "IPv6 posture…"
v6def="$(ip -6 route show default 2>/dev/null || true)"
if [[ -n "$v6def" ]]; then
  echo "$v6def"
  if sysctl net.ipv6.conf.all.disable_ipv6 2>/dev/null | grep -q '= 1'; then
    warn "IPv6 default route present, but sysctl says IPv6 disabled (inconsistent)."
  fi
else
  info "No IPv6 default route detected."
fi

# Tailscale hints
if command -v tailscale >/dev/null 2>&1; then
  info "Tailscale detected; checking Exit Node and netfilter posture…"
  if tailscale status 2>/dev/null | grep -qi "Exit node:"; then
    warn "Using a Tailscale Exit Node may impact UDP to Discord voice."
  fi
fi

# Firewall posture snippets
if command -v ufw >/dev/null 2>&1; then
  if ufw status 2>/dev/null | grep -qi "Status: active"; then
    warn "UFW active — ensure outbound UDP allowed."
  fi
fi
if command -v nft >/dev/null 2>&1; then
  info "nftables rules mentioning udp:"
  nft list ruleset 2>/dev/null | grep -i udp | sed 's/^/  /' || true
fi

echo
echo "──────── Suggested next steps ────────"
echo "• If STUN shows no response, check firewall/NAT/overlay (e.g., Tailscale)"
echo "• Try setting a specific voice channel region (not Auto) and retry"
echo "• If IPv6 present, test temporarily disabling IPv6 (sysctl) and retry"
echo "• Packet capture during /joinvoice:"
echo "    sudo tcpdump -ni any '(udp port 3478) or (udp portrange 50000-65535)'"
echo "  Look for bidirectional UDP with the Discord voice endpoint."
echo "──────────────────────────────────────"

if (( need_fix != 0 )); then exit 2; fi
if (( net_issue != 0 )); then exit 1; fi
exit 0

