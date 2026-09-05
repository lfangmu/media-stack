# 影视下载台 · media 栈

一套可一键部署的媒体下载栈（Radarr / Sonarr / Prowlarr / qBittorrent / FlareSolverr），自带一个 Web 控制台。
**部署完成后，在页面上填入你的境外 HTTP 代理地址即可出外网；不填则仅内网。**

## 包含的服务

| 服务 | 端口 | 作用 |
|---|---|---|
| 影视控制台 (autopilot) | 8787 | Web UI，配置出网代理 |
| proxy-forwarder (squid) | 3128 | HTTP 出口转发器 |
| flaresolverr | 8191 | 绕过 Cloudflare |
| prowlarr | 9696 | 索引器管理 |
| radarr | 7878 | 电影 |
| sonarr | 8989 | 剧集 |
| qbittorrent | 8085 / 6881 | 下载 |

## 快速部署

```bash
git clone <本仓库> media-stack
cd media-stack
./deploy.sh          # 推荐：自动取宿主 uid/gid、统一目录属主、拉起服务
```

`deploy.sh` 会自动把 `PUID`/`PGID` 设为「持有本目录的宿主用户」身份，并把整个项目目录 `chown` 成同一身份——这样容器内 Radarr/Sonarr/Prowlarr/QBittorrent 才能正常写库。**换任何机器直接 `./deploy.sh` 即可，无需手改。**

若不想用脚本，也可手动：

```bash
cp .env.example .env
# 编辑 .env，把 PUID/PGID 改成你的 `id -u` / `id -g`（默认 1000）
docker compose up -d
```

打开 `http://<本机IP>:8787`。

## 权限（很重要，避免 500 报错）

Radarr / Sonarr / Prowlarr / QBittorrent 是 linuxserver 镜像，以 `PUID`/`PGID` 指定的用户身份运行并写库。
**必须让 `PUID`/`PGID` 与「持有本项目目录的宿主用户」uid/gid 一致**，否则容器写不进卷 →
`attempt to write a readonly database` → 添加影片等写操作报 HTTP 500。

- `./deploy.sh` 已自动处理（取宿主 uid/gid + chown），正常不会遇到。
- 手动部署务必先 `id -u` / `id -g` 填进 `.env` 的 `PUID`/`PGID`；默认 `1000` 仅在宿主用户正好是 uid 1000 时有效。
- 常见翻车场景：用 `sudo` 跑过 docker、或手动 `chown` 过项目目录，导致卷属主与容器 uid 对不上。统一成同一个 uid 即可恢复。

## 配置境外外网访问

1. 在页面「境外外网访问」里填入你的 HTTP 代理地址，例如：
   - `http://host:port`
   - `http://user:pass@host:port`
2. 点「保存并重启出口」。控制台会写入 `.env` 并重启 squid，栈内所有服务即可经该代理出外网。
3. 点「出网自检」确认返回 `204` 即正常。
4. 点「把 qB 代理指向出口」，让下载器也走代理（qB 不读环境变量，需这一步）。

> 留空 = 仅内网（不出外网，但内网/本地索引器仍可用）。
> 大多数代理客户端（Clash / v2ray / SS）都提供 HTTP 端口，直接用那个即可。若你只有 SOCKS5，在客户端里额外开一个 HTTP 端口即可。

## 发现墙（搜索 / 筛选 / 一键下载）

「发现」标签页可按 **类型 / 类型 / 国家 / 年代 / 最低评分 / 最长时长** 筛选影片，或直接搜片名，再把选中的片子一键加入 Radarr（电影）或 Sonarr（剧集）。

使用前先在「出网配置 → 媒体库设置」填好：
- **TMDB API Key**（https://www.themoviedb.org/settings/api 免费申请）—— 发现墙的筛选、海报、简介都来自 TMDB。
- **Radarr / Sonarr 地址与 API Key**（在各自 WebUI 的 Settings → General 获取）—— 用于一键添加下载。

这些均可留空：不填 TMDB 发现墙不可用，但出网配置与下载栈照常工作。

## 工作原理（一句话）

所有服务的出网都指向 `proxy-forwarder`(squid)；squid 再把流量转发到你填的代理。
改代理只改一处（页面），不用逐个服务配置。宿主本身不需要任何代理或 VPN。

## 目录结构

```
docker-compose.yml        # 整套栈定义
.env.example              # 配置模板（复制为 .env）
squid/                    # squid 模板与入口脚本
autopilot/                # 影视控制台源码（Python，标准库，无第三方依赖）
data/                     # 下载与媒体目录（自动创建）
```
