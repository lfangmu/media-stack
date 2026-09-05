#!/bin/sh
# 影视下载台 · squid 入口：从挂载的 .env 读取 UPSTREAM_PROXY_*，生成 cache_peer
set -e

ENV_FILE=/opt/media/.env

get() {
  # 去掉行尾 \r，避免 CRLF 的 .env 把 \r 带进变量导致误判非空
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
  # 用字面 \n，sed 会解释为换行，避免把真实换行塞进替换串导致 "unterminated s command"
  PEER=$(printf '%s\\ncache_peer_access %s allow all' "$LINE" "$UP_HOST")
  DIRECTIVE="never_direct allow all"
else
  PEER="# no upstream configured: allow direct egress"
  DIRECTIVE="always_direct allow all"
fi

sed -e "s|__CACHE_PEER_LINES__|$PEER|" -e "s|__DIRECTIVE__|$DIRECTIVE|" /etc/squid/squid.conf.template > /etc/squid/squid.conf

# 初始化交换目录（幂等，缺失则创建；已存在则忽略）
squid -z -N 2>/dev/null || true

squid -k parse
# 清掉残留 pid 文件：容器被 docker daemon 重启（restart 策略）后，旧层里的
# /run/squid.pid（写着 PID 1）会残留，squid 误判"已在运行"而 FATAL 退出进入重启循环。
# 启动时先删，使 entrypoint 对容器重启幂等。
rm -f /run/squid.pid /var/run/squid.pid 2>/dev/null || true
exec squid -NYC
