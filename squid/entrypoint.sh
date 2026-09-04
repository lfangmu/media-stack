#!/bin/sh
# 影视下载台 · squid 入口：从挂载的 .env 读取 UPSTREAM_PROXY_*，生成 cache_peer
set -e

ENV_FILE=/opt/media/.env

get() {
  grep "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-
}

UP_HOST=$(get UPSTREAM_PROXY_HOST)
UP_PORT=$(get UPSTREAM_PROXY_PORT)
UP_AUTH=$(get UPSTREAM_PROXY_AUTH)

if [ -n "$UP_HOST" ] && [ -n "$UP_PORT" ]; then
  LINE="cache_peer $UP_HOST parent $UP_PORT 0 no-query default"
  if [ -n "$UP_AUTH" ]; then
    LINE="$LINE login=$UP_AUTH"
  fi
  PEER=$(printf '%s\ncache_peer_access %s allow all' "$LINE" "$UP_HOST")
else
  PEER="# no upstream configured: internal-only"
fi

sed "s|__CACHE_PEER_LINES__|$PEER|" /etc/squid/squid.conf.template > /etc/squid/squid.conf

squid -k parse
exec squid -NYC
