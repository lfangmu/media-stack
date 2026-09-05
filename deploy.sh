#!/usr/bin/env bash
# 影视下载台 media 栈 · 一键部署
# 关键：自动把 PUID/PGID 设为「宿主用户」的 uid/gid，并把整个项目目录 chown 成同一身份，
# 使 Radarr/Sonarr/Prowlarr/QBittorrent 容器（linuxserver 镜像以 PUID/PGID 写库）与卷属主一致，
# 避免 "attempt to write a readonly database" 的 500 报错。换任何机器部署都无需手改。
set -euo pipefail
cd "$(dirname "$0")"

# 1) 宿主用户身份
PUID=$(id -u)
PGID=$(id -g)
echo ">> 宿主用户 uid=$PUID gid=$PGID"

# 2) 准备 .env
if [ ! -f .env ]; then
  echo ">> 由 .env.example 生成 .env"
  cp .env.example .env
fi

# 3) 把 PUID/PGID 写进 .env（若已有则覆盖，确保与宿主一致）
if grep -q '^PUID=' .env; then
  sed -i "s/^PUID=.*/PUID=$PUID/" .env
else
  printf 'PUID=%s\n' "$PUID" >> .env
fi
if grep -q '^PGID=' .env; then
  sed -i "s/^PGID=.*/PGID=$PGID/" .env
else
  printf 'PGID=%s\n' "$PGID" >> .env
fi

# 4) 整个项目目录 chown 成该用户，使容器（PUID）与卷属主一致
echo ">> 统一项目目录属主为 $PUID:$PGID（必要时会请求 sudo）"
if [ -w . ]; then
  chown -R "$PUID:$PGID" . 2>/dev/null || sudo chown -R "$PUID:$PGID" .
else
  sudo chown -R "$PUID:$PGID" .
fi

# 5) 拉起（PUID/PGID 同时以环境变量传入，确保覆盖 .env 默认值）
echo ">> docker compose up -d"
PUID="$PUID" PGID="$PGID" docker compose up -d

echo ">> 完成。访问 http://localhost:8787 （或在页面「出网配置」填境外 HTTP 代理后刷新）"
