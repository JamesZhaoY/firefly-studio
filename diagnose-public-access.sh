#!/usr/bin/env bash
# Run as root on the ECS while another network requests http://PUBLIC_IP:19999/.
set -u

APP_CONTAINER="${APP_CONTAINER:-firefly-app}"
PORT="${PORT:-19999}"
SECONDS="${SECONDS:-15}"

require() {
  command -v "$1" >/dev/null 2>&1 || {
    printf 'Missing required command: %s\n' "$1" >&2
    exit 1
  }
}

require docker
require ip
require ss

printf '== Service ==\n'
docker ps --filter "name=^/${APP_CONTAINER}$" --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
ss -lntp "( sport = :${PORT} )" || true
docker exec "$APP_CONTAINER" ss -lnt "( sport = :${PORT} )" || true

printf '\n== Routing ==\n'
ip rule show
ip route show table main
ip route show table 2022 2>/dev/null || true
ip -4 addr show mihomo 2>/dev/null || true

printf '\n== Docker NAT ==\n'
iptables -t nat -L DOCKER -n -v | grep "dpt:${PORT}" || true
iptables -t mangle -L -n -v

printf '\n== Nginx before request ==\n'
docker exec "$APP_CONTAINER" tail -n 5 /var/log/nginx/access.log 2>/dev/null || true

printf '\nAsk a client on another network to request http://PUBLIC_IP:%s/ now. Capturing %ss...\n' "$PORT" "$SECONDS"
if command -v tcpdump >/dev/null 2>&1; then
  timeout "$SECONDS" tcpdump -nn -i any "tcp port ${PORT}" 2>/dev/null || true
else
  printf 'tcpdump is not installed; skipping packet capture.\n'
fi

printf '\n== Nginx after request ==\n'
docker exec "$APP_CONTAINER" tail -n 20 /var/log/nginx/access.log 2>/dev/null || true
docker exec "$APP_CONTAINER" tail -n 20 /var/log/nginx/error.log 2>/dev/null || true

printf '\n== Connections ==\n'
ss -nt "( sport = :${PORT} or dport = :${PORT} )" || true
command -v conntrack >/dev/null 2>&1 && conntrack -L -p tcp --dport "$PORT" 2>/dev/null || true

printf '\n== Fix hints ==\n'
PUBIP=$(curl -s --max-time 5 http://100.100.100.200/latest/meta-data/public-ipv4 2>/dev/null || true)
case "${PUBIP}" in
  ""|*[!0-9.]*) PUBIP="<ECS_PUBLIC_IP>" ;;
esac
printf '%s\n' "If external clients see TCP RST or Empty reply while the host curl works:"
printf '%s\n' "  1. Enable docker MASQUERADE so SYN-ACK leaves with the public source IP:"
printf '       sudo iptables -t nat -I POSTROUTING 1 -s 172.20.0.0/16 -d 101.90.148.0/24 -j SNAT --to-source %s\n' "$PUBIP"
printf '%s\n' "  2. Pin the mihomo TUN to skip the ECS public IP / VPC subnet so reply packets use the default route:"
printf '       route-exclude-address:\n'
printf '         - %s/32\n' "$PUBIP"
printf '         - 172.26.128.0/20\n'
printf '         - 100.64.0.0/10\n'
printf '         - 172.20.0.0/16\n'
printf '         - 198.18.0.0/30\n'
