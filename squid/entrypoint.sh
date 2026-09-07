#!/bin/sh
# 渲染 .env 里的 UPSTREAM_PROXY_* 为 squid 的 cache_peer / direct 指令
set -e

ENV_FILE=/opt/media/.env

get() {
  grep "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r'
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
  DIRECTIVE="never_direct allow all"
else
  PEER="# no upstream configured: allow direct egress"
  DIRECTIVE="always_direct allow all"
fi

# 仅替换「整行就是占位符」的行（^...$），避免模板注释里若出现占位符字样被误替换。
# 用 awk 而非 sed：PEER 含换行，sed 替换串里的换行会破坏命令，awk 无此问题。
awk -v peer="$PEER" -v directive="$DIRECTIVE" '
  /^__CACHE_PEER_LINES__$/ { print peer; next }
  /^__DIRECTIVE__$/        { print directive; next }
  { print }
' /etc/squid/squid.conf.template > /etc/squid/squid.conf

# 初始化缓存目录（首次或权限重置）
squid -z -N 2>/dev/null || true

# 解析校验，失败会非零退出（配合 set -e 不会启动坏配置）
squid -k parse
rm -f /run/squid.pid /var/run/squid.pid 2>/dev/null || true
exec squid -NYC
