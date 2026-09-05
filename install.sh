#!/usr/bin/env bash
# 影视下载台 media 栈 · 一键安装引导脚本
#
# 用法（通用 Linux 主机，需有 sudo 或 root）：
#   bash <(curl -sSL https://raw.githubusercontent.com/lfangmu/media-stack/main/install.sh)
#
# 私有仓库（默认私有）时带令牌：
#   GITHUB_TOKEN=ghp_xxx bash <(curl -sSL https://raw.githubusercontent.com/lfangmu/media-stack/main/install.sh)
#
# 本脚本只负责「把整套 docker-compose 栈拉起来」：
#   1) 安装 docker 与 docker compose 插件（缺失时，走 https://linuxmirrors.cn/docker.sh 镜像脚本）
#   2) 安装 git（克隆仓库用）
#   3) 把仓库克隆到 MEDIA_ROOT（默认 /opt/media）
#   4) 由 .env.example 生成 .env，并交互填入 TMDB Key / 出网代理 / 访问令牌 / 下载目录
#   5) 调用仓库内 deploy.sh（自动按宿主 uid/gid 统一 PUID/PGID + chown + docker compose up -d）
#
# 不做的事（刻意保持通用）：不碰 OpenClash VM、不配置 route-B NAT、不动 fnOS 防火墙。
#   这些是某台 NAS 的专属网络，通用主机用「直连出网」或「填一个 HTTP 代理」即可。
set -euo pipefail

MEDIA_ROOT="${MEDIA_ROOT:-/opt/media}"
MEDIA_REPO="${MEDIA_REPO:-https://github.com/lfangmu/media-stack.git}"
MEDIA_BRANCH="${MEDIA_BRANCH:-main}"
DRY_RUN=0
YES=0
INTERACTIVE=1
[ -t 0 ] || INTERACTIVE=0   # 非 TTY（管道/CI）视为非交互，全部用默认值

log()  { printf '\033[36m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<EOF
影视下载台 media 栈 一键安装

用法:
  bash <(curl -sSL https://raw.githubusercontent.com/lfangmu/media-stack/main/install.sh) [选项]

选项:
  -y, --yes        跳过确认，直接执行
      --dry-run    只打印将要做什么，不真正安装/克隆/起栈
      --root DIR   指定安装目录 (默认 /opt/media)
      --repo URL   指定仓库地址 (默认 $MEDIA_REPO)
  -h, --help       显示本帮助
EOF
}

# ---- 参数解析 ----
while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes) YES=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --root) shift; MEDIA_ROOT="$1" ;;
    --repo) shift; MEDIA_REPO="$1" ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数: $1 (用 -h 看帮助)" ;;
  esac
  shift
done

need_cmd() { command -v "$1" >/dev/null 2>&1 || die "需要命令: $1，请先安装或联系维护者"; }

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    log "[dry-run] $*"
  else
    eval "$@"
  fi
}

confirm() {
  [ "$YES" -eq 1 ] && return 0
  [ "$INTERACTIVE" -eq 0 ] && return 0
  local prompt="$1 (y/N) "; read -r -p "$prompt" ans
  case "$ans" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# 在 .env 中设置/追加一个键（幂等）
set_env() {
  local key="$1" val="$2" file="$MEDIA_ROOT/.env"
  if grep -q "^${key}=" "$file" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$file"
  else
    printf '%s=%s\n' "$key" "$val" >> "$file"
  fi
}

# 交互读取一个带默认值与环境预填的键
prompt_env() {
  local key="$1" label="$2" default="$3"
  local cur=""
  if [ -f "$MEDIA_ROOT/.env" ]; then
    cur=$(grep "^${key}=" "$MEDIA_ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2-)
  fi
  : "${cur:=$default}"
  # 若已通过同名环境变量传入，直接采用，不再询问
  if [ -n "${!key:-}" ]; then
    set_env "$key" "${!key}"
    log "  $label = ${!key} (来自环境变量)"
    return
  fi
  if [ "$INTERACTIVE" -eq 0 ]; then
    set_env "$key" "$cur"
    log "  $label = $cur (默认值/非交互)"
    return
  fi
  local input
  read -r -p "  $label [$cur]: " input
  input="${input:-$cur}"
  set_env "$key" "$input"
}

# ---- 1) docker ----
ensure_docker() {
  if command -v docker >/dev/null 2>&1; then
    log "docker 已存在: $(docker --version 2>/dev/null | head -1)"
  else
    log "未检测到 docker，安装中（linuxmirrors.cn 镜像脚本）…"
    confirm "将安装 docker，继续？" || die "已取消"
    run "bash <(curl -sSL https://linuxmirrors.cn/docker.sh)"
  fi
  # compose 插件
  if docker compose version >/dev/null 2>&1; then
    log "docker compose 插件已存在"
  else
    log "未检测到 docker compose 插件，安装中…"
    run "bash <(curl -fsSL https://raw.githubusercontent.com/docker/compose-switches/main/install.sh) || (apt-get update -y && apt-get install -y docker-compose-plugin)"
  fi
}

# ---- 2) git ----
ensure_git() {
  command -v git >/dev/null 2>&1 && return 0
  log "未检测到 git，安装中…"
  if command -v apt-get >/dev/null 2>&1; then
    run "sudo apt-get update -y && sudo apt-get install -y git"
  elif command -v dnf >/dev/null 2>&1; then
    run "sudo dnf install -y git"
  elif command -v pacman >/dev/null 2>&1; then
    run "sudo pacman -S --noconfirm git"
  else
    die "无法自动安装 git，请手动安装后重试"
  fi
}

# ---- 3) 克隆仓库 ----
get_repo() {
  ensure_git
  local url="$MEDIA_REPO"
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    # 私有仓库：用令牌克隆（仅用于 https 克隆鉴权，不落盘凭据）
    url="https://${GITHUB_TOKEN}@${MEDIA_REPO#https://}"
  fi
  if [ -d "$MEDIA_ROOT/.git" ]; then
    log "已存在仓库，拉取最新 ($MEDIA_ROOT)…"
    ( cd "$MEDIA_ROOT" && run "git pull --ff-only" )
  else
    if [ "$DRY_RUN" -eq 0 ] && [ -d "$MEDIA_ROOT" ] && [ -n "$(ls -A "$MEDIA_ROOT" 2>/dev/null)" ]; then
      die "$MEDIA_ROOT 非空且非 git 仓库，请换一个空目录 (--root)"
    fi
    log "克隆仓库到 $MEDIA_ROOT …"
    run "sudo mkdir -p $(dirname "$MEDIA_ROOT")"
    run "git clone --depth 1 --branch $MEDIA_BRANCH $url $MEDIA_ROOT"
  fi
}

# ---- 4) 生成 .env 并交互填值 ----
setup_env() {
  cd "$MEDIA_ROOT"
  if [ ! -f .env ]; then
    [ -f .env.example ] || die "仓库缺少 .env.example"
    cp .env.example .env
    log "已用 .env.example 生成 .env"
  else
    log ".env 已存在，仅补充缺失键"
  fi

  echo ""
  log "=== 配置 media 栈（直接回车用默认值；也可先 export 同名环境变量）==="
  prompt_env DATA_DIR          "宿主机数据目录（容器 /data 映射到这里，下载与媒体库落盘位置；如 /mnt/media）" "./data"
  prompt_env TMDB_API_KEY      "TMDB API Key（发现墙/海报，必填，https://www.themoviedb.org/settings/api）" ""
  prompt_env EGRESS_PROXY      "出网 HTTP 代理（留空=直连；Clash/v2ray/SS 的 HTTP 端口）" ""
  prompt_env AUTOPILOT_TOKEN   "页面访问令牌（留空=不鉴权）" ""
  prompt_env AUTOPILOT_WEBHOOK_URL "抓取完成通知 Webhook（留空=关闭）" ""
  prompt_env QB_SAVE_PATH      "qB 下载/做种目录（须落在 DATA_DIR 映射的宿主机目录内）" "/data/downloads"
  prompt_env MOVIE_ROOT        "电影库默认根目录" "/data/movies"
  prompt_env TV_ROOT           "剧集库默认根目录" "/data/tv"
  log ".env 配置完成"
}

# ---- 5) 起栈 ----
deploy() {
  cd "$MEDIA_ROOT"
  [ -f deploy.sh ] || die "仓库缺少 deploy.sh"
  chmod +x deploy.sh
  log "运行 deploy.sh（自动 PUID/PGID + chown + docker compose up -d）…"
  run "./deploy.sh"
}

main() {
  log "影视下载台 media 栈 一键安装  (root=$MEDIA_ROOT, repo=$MEDIA_REPO)"
  if [ "$DRY_RUN" -eq 1 ]; then
    log "== DRY RUN：仅展示计划，不执行任何变更 =="
    log "  1) 确保 docker + compose 插件"
    log "  2) 确保 git"
    log "  3) 克隆 $MEDIA_REPO -> $MEDIA_ROOT"
    log "  4) 由 .env.example 生成 .env 并交互填值"
    log "  5) 运行 deploy.sh 起栈"
    log "完成。去掉 --dry-run 即真正执行。"
    exit 0
  fi
  need_cmd curl
  confirm "将在本机安装 docker 并部署 media 栈到 $MEDIA_ROOT，继续？" || die "已取消"
  ensure_docker
  get_repo
  setup_env
  deploy
  echo ""
  log "== 完成 =="
  log "打开 http://<本机IP>:8787  （影视下载台）"
  log "如需出网代理，页面「出网配置」里填一个境外 HTTP 代理后刷新即可。"
}

main "$@"
