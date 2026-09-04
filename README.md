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
cp .env.example .env
docker compose up -d
```

打开 `http://<本机IP>:8787`。

## 配置境外外网访问

1. 在页面「境外外网访问」里填入你的 HTTP 代理地址，例如：
   - `http://host:port`
   - `http://user:pass@host:port`
2. 点「保存并重启出口」。控制台会写入 `.env` 并重启 squid，栈内所有服务即可经该代理出外网。
3. 点「出网自检」确认返回 `204` 即正常。
4. 点「把 qB 代理指向出口」，让下载器也走代理（qB 不读环境变量，需这一步）。

> 留空 = 仅内网（不出外网，但内网/本地索引器仍可用）。
> 大多数代理客户端（Clash / v2ray / SS）都提供 HTTP 端口，直接用那个即可。若你只有 SOCKS5，在客户端里额外开一个 HTTP 端口即可。

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
