# 影视下载台 · media 栈

一套自托管的「**发片名 → 自动下载 → 自动入库**」媒体下载栈。7 个容器、一条命令装完，自带一个中文 Web 控制台。
装好后在页面上填一个境外 HTTP 代理即可出网；不填则走直连（宿主机能上外网时也完全可用）。

- **一个控制台管全部**：顶部搜索框 + 8 个页签（发现墙、下载队列、媒体库、上映日历、抓取历史、索引器、系统状态、配置），电影 / 剧集双模式切换。
- **一条命令装完**：缺 docker / git 会自动补，`.env` 交互生成，容器与卷的 uid/gid 自动对齐，无需手改配置。
- **出网只配一处**：所有需要翻墙的服务都指向栈内的 squid 转发器，填一个代理全栈生效；改代理只重启这一个容器。
- **装完即用**：安装阶段就把 *arr 管理员账号建好、关掉首启向导、界面切中文、自动接好 qBittorrent 下载客户端与 Prowlarr→Radarr/Sonarr 应用连接；预置的公共索引器由控制台启动后自动补齐（约 1~3 分钟，期间可在「索引器」页看进度）。
- **零第三方依赖**：控制台是纯 Python 标准库的单文件程序，不需要 pip / npm，也不需要 Node 或数据库。

---

## 一键安装

**通用 Linux 主机（Debian/Ubuntu/CentOS/Arch/群晖/fnOS 等，需要 root 或 sudo）：**

```bash
bash <(curl -sSL https://raw.githubusercontent.com/lfangmu/media-stack/main/install.sh)
```

脚本会依次完成：安装 docker 与 compose 插件（缺失时）→ 安装 git → 克隆仓库到 `/opt/media` → 由 `.env.example` 生成 `.env` 并交互询问几项配置 → 前置自检 → 调用仓库内 `deploy.sh` 拉起整套栈。

装完访问 **`http://<本机IP>:8787`**。

### 常用选项

| 选项 | 说明 |
|---|---|
| `-y, --yes` | 跳过所有确认，全自动执行（适合脚本/无人值守） |
| `--dry-run` | 只打印将要做什么，不安装、不克隆、不起栈 |
| `--root DIR` | 指定安装目录（默认 `/opt/media`） |
| `--repo URL` | 指定仓库地址（用于 fork 或内网镜像） |
| `-h, --help` | 查看帮助 |

### 非交互安装（先把值交给环境变量）

`install.sh` 会优先采用同名环境变量，不再逐项询问：

```bash
DATA_DIR=/mnt/media \
TMDB_API_KEY=你的key \
EGRESS_PROXY=http://user:pass@1.2.3.4:7890 \
AUTOPILOT_TOKEN=换成你自己的口令 \
bash <(curl -sSL https://raw.githubusercontent.com/lfangmu/media-stack/main/install.sh) -y
```

可用的环境变量：`MEDIA_ROOT`（安装目录）、`MEDIA_REPO` / `MEDIA_BRANCH`（仓库与分支）、`DATA_DIR`、`TMDB_API_KEY`、`EGRESS_PROXY`、`AUTOPILOT_TOKEN`、`AUTOPILOT_WEBHOOK_URL`。
敏感项（TMDB Key / 代理 / 令牌 / Webhook）在回显时会自动脱敏，不会把明文打到屏幕或日志里。

**私有 fork 或私有镜像**：加 `GITHUB_TOKEN`（仅用于克隆鉴权，不落盘）。

```bash
GITHUB_TOKEN=ghp_xxx bash <(curl -sSL https://raw.githubusercontent.com/lfangmu/media-stack/main/install.sh) -y
```

### 手动安装（不用引导脚本）

```bash
git clone https://github.com/lfangmu/media-stack.git /opt/media
cd /opt/media
cp .env.example .env      # 按需编辑（至少确认 DATA_DIR / EGRESS_PROXY）
./deploy.sh               # 自动对齐 PUID/PGID、建数据目录、docker compose up -d
```

`deploy.sh` 就是**幂等**的：重复执行只是把 PUID/PGID 重新对齐、目录补齐、再 `up -d` 一次。

### 已装好的机器怎么更新

```bash
cd /opt/media && git pull --ff-only && ./deploy.sh
```

改了 `autopilot/app.py` 或 `docker-compose.yml` 后重建栈（`app.py` 是挂载进容器的，重建一次即可让改动生效）：

```bash
cd /opt/media && docker compose up -d --build autopilot
```

---

## 包含的服务

| 服务（compose 名） | 容器名 | 镜像 | 端口 | 作用 |
|---|---|---|---|---|
| `autopilot` | `media-autopilot` | 本地构建（`./autopilot`） | `8787` | 影视下载台控制台：搜索、发现墙、队列、媒体库、日历、历史、索引器、系统状态、配置；同时做 *arr / qB 的同源反代与直登 |
| `proxy-forwarder` | `media-proxy-forwarder` | `ubuntu/squid` | `3128` | 全栈唯一出网闸口：按 `.env` 决定走上游代理还是直连；只转发不缓存 |
| `radarr` | `media-radarr` | `linuxserver/radarr` | `7878` | 电影管理：画质档位、种子筛选、导入与改名入库 |
| `sonarr` | `media-sonarr` | `linuxserver/sonarr` | `8989` | 剧集管理：追更、整季监控、缺集补漏 |
| `prowlarr` | `media-prowlarr` | `linuxserver/prowlarr` | `9696` | 索引器聚合：集中维护站点与分类，同步给 Radarr / Sonarr |
| `flaresolverr` | `media-flaresolverr` | `flaresolverr/flaresolverr` | `8191` | 过 Cloudflare 人机验证（部分站点必需） |
| `qbittorrent` | `media-qbittorrent` | `linuxserver/qbittorrent` | `8085` WebUI / `6881` 种子 | 真正下载：接收 *arr 投递的种子，下完留在 `/data/downloads` |

**默认账号**：qBittorrent WebUI 是 `admin` / `MediaFn2026`（compose 里的默认值，改动时记得同步 `.env` 的 `QBITTORRENT_PASS`，否则控制台探测不到它）。Radarr / Sonarr / Prowlarr 的**用户名**固定 `admin`，**密码**在首次启动时随机生成并写入 `.env` 的 `ARR_ADMIN_PASS`——平时不需要记它，控制台「系统状态 → 打开 ↗」是免登录直登。

**数据流向**：`autopilot` 下指令 → `radarr` / `sonarr` 决定要哪一版 → `prowlarr`（必要时借 `flaresolverr` 过盾）找种子 → `qbittorrent` 下载 → 归档到 `/data/movies`、`/data/tv`。需要出网的请求统一从 `proxy-forwarder` 出去。

---

## 控制台能做什么

| 页签 | 功能 |
|---|---|
| 搜索（顶部） | 输入片名搜候选（电影走 Radarr lookup、剧集走 Sonarr lookup），选画质档位后一键添加；支持一次贴多行批量添加、`仅预览` 试跑 |
| 🎯 发现 | 发现墙数据源可切换：**TMDB**（热门 / 热映 / 即将上映 / 高分，支持类型、国别、年代、最低评分、时长筛选排序）与 **豆瓣**（热门 / 豆瓣高分 / 最新 / 华语 + 热门剧集 / 高分剧集）。点开详情页看简介、海报、**演职表与相似推荐**，直接加入下载。豆瓣卡片经 TMDB 按片名解析后复用同一套 Radarr/Sonarr 下载链 |
| ⬇️ 下载队列 | 队列进度、速度、ETA，可取消；支持 电影 / 剧集 / 全部 三种视图 |
| 🎞️ 媒体库 | 海报墙；按 全部 / 已下载 / 下载中 / 待源 / 未监控 过滤；可重新搜索或移除条目 |
| 📅 日历 | 电影上映与剧集播出日历（Radarr `releaseDate` + Sonarr `airDate` 合并按日排序） |
| 📜 抓取历史 | 抓取 / 入库 / 失败的最近记录，分页加载 |
| 🛰️ 索引器 | Prowlarr 索引器健康总览；一键「扫描添加」补齐预置公共索引器，失效的可批量重新启用（受 Cloudflare 保护的会自动打上 cf 标签交给 FlareSolverr） |
| 📊 系统状态 | 磁盘、索引器数量、各服务版本与连通性；每行的「打开 ↗」经 autopilot 同源反代直达 *arr / qB，免登录 |
| 🛠 配置 | 出网代理、TMDB Key、访问令牌、Webhook、媒体库目录；保存即写 `.env` 并热生效（代理变化只重启出口容器） |

顶部还有 **🎬 电影 / 📺 剧集** 模式切换：搜索、队列、媒体库三块跟着切数据源。

---

## 出网配置（唯一通常需要改的地方）

所有需要翻墙的服务都把 HTTP 代理指向栈内的 `proxy-forwarder`：

```
radarr / sonarr / prowlarr / flaresolverr
        └─ HTTP_PROXY=http://proxy-forwarder:3128
             └─ squid：按 .env 的 UPSTREAM_PROXY_* 决定
                  ├─ 有上游：cache_peer 转发到你的代理（never_direct）
                  └─ 无上游：直连出网（always_direct）
```

- **配置方式**：页面「配置 → 代理链接」填 `http://host:port` 或 `http://user:pass@host:port`，保存即可。写入 `.env` 的 `EGRESS_PROXY` 与 `UPSTREAM_PROXY_*`，只重启出口容器，*arr / qB / FlareSolverr 都不用动。
- **留空 = 直连**：宿主机本身能上外网时不用填，`autopilot` 与 squid 都会直连。
- **验证**：页面「测试外网连通性」返回 `204` 即正常；「测试 TMDB 连接」验证发现墙数据源。
- **qBittorrent 不在其中**：BT 流量走代理既慢又容易被节点掐，默认直连。确实需要时，在 qB 自己的 WebUI（Settings → Connection → Proxy）里配，本项目**不会**替它改这项设置。
- *arr 之间、*arr 与 qB 之间的**内网互访已通过 `NO_PROXY` 绕开 squid**（否则应用连接测试会 502）。如果你自定义了服务名，记得同步 `docker-compose.yml` 里的 `NO_PROXY`。

> 大多数代理客户端（Clash / v2ray / SS / sing-box）都会开一个 HTTP 端口，用那个即可。只有 SOCKS5 端口的话，在客户端里额外开一个 HTTP 端口。

---

## 数据目录与落盘

容器内的 `/data` 映射到宿主机的 `${DATA_DIR}`（默认项目下的 `./data`，建议改到大盘，如 `/mnt/media`、`/vol1/1000/娱乐`）：

| 宿主机路径 | 容器内路径 | 用途 |
|---|---|---|
| `$DATA_DIR/downloads` | `/data/downloads` | qBittorrent 下载与做种目录 |
| `$DATA_DIR/movies` | `/data/movies` | Radarr 电影库（导入后落盘） |
| `$DATA_DIR/tv` | `/data/tv` | Sonarr 剧集库 |

- 三个子目录由 `deploy.sh` 自动创建。**手动部署时务必自己建**，否则 *arr 添加根目录会报「路径在容器内不存在」。
- `QB_SAVE_PATH` 与 `MOVIE_ROOT` / `TV_ROOT` 必须落在**同一个挂载点**（都在 `$DATA_DIR` 下），这样 *arr 导入时走硬链接而不是复制，既快又不占双份空间。
- 在页面里改 `DATA_DIR` 会触发整栈重新挂载（volume 映射变了，必须重建容器），约十几秒，完成后刷新页面。

---

## 权限（避免 `attempt to write a readonly database`）

Radarr / Sonarr / Prowlarr / QBittorrent 都是 linuxserver 镜像，以 `PUID` / `PGID` 指定的身份运行并写数据库。**这两个值必须等于「持有本项目目录的宿主用户」的 uid/gid**，否则容器写不进卷，添加影片等写操作会返回 HTTP 500。

- `./deploy.sh` 会自动取当前宿主用户的 uid/gid 写进 `.env`，并把项目目录 `chown` 成同一身份——换任何机器都不用改。
- 手动部署请先 `id -u` / `id -g`，把结果填进 `.env` 的 `PUID` / `PGID`（默认 `1000` 只在宿主用户恰好是 uid 1000 时正确）。
- 常见翻车：用 `sudo` 跑过 docker、或手动 `chown` 过项目目录，导致卷属主与容器 uid 不一致。统一成同一个 uid 即可恢复。

---

## `.env` 配置项

| 键 | 默认 | 说明 |
|---|---|---|
| `AUTOPILOT_PORT` | `8787` | 控制台对外端口 |
| `AUTOPILOT_TOKEN` | 空 | 填写后访问需带 `?token=该值`；留空则不鉴权 |
| `PUID` / `PGID` | `1000` | 容器运行身份，须与宿主用户一致 |
| `EGRESS_PROXY` | 空 | 出网代理（autopilot 实际读取的键）；空 = 直连 |
| `UPSTREAM_PROXY_HOST/PORT/AUTH` | 空 | 由页面保存时自动解析生成，一般不用手改 |
| `AUTOPILOT_WEBHOOK_URL` | 空 | 抓取完成通知（飞书/企业微信/Discord 机器人等） |
| `DATA_DIR` | `./data` | 容器 `/data` 的宿主落点，下载与媒体库的根 |
| `QB_SAVE_PATH` | `/data/downloads` | qB 下载目录（容器内路径） |
| `MOVIE_ROOT` / `TV_ROOT` | `/data/movies` / `/data/tv` | 电影 / 剧集库根目录（容器内路径） |
| `QBITTORRENT_USER` / `QBITTORRENT_PASS` | `admin` / `MediaFn2026` | 探测 qB 用的账号，须与 compose 里 qB 的 `WEBUI_PASSWORD` 一致 |
| `TMDB_API_KEY` | 空 | 发现墙 / 海报 / 简介数据源，[免费申请](https://www.themoviedb.org/settings/api)；豆瓣卡片添加 / 详情也依赖它按片名解析 TMDB id |
| `DOUBAN_ENABLED` | `1` | 发现墙「豆瓣」数据源开关；置 `0`/`false`/`no` 关闭 |
| `DOUBAN_PROXY` | 空（回退 `PROXY_URL`） | 豆瓣请求专用代理：`direct`/`none`/`0` 走容器直连（豆瓣为国内服务，直连通常更快），留空则与全局代理一致 |
| `RADARR_URL` / `SONARR_URL` | `http://media-radarr:7878` 等 | 容器内互访地址，一般不用改 |
| `RADARR_API_KEY` / `SONARR_API_KEY` | 空 | 留空时自动从各容器挂进来的 `config.xml` 读取 |

---

## HTTP API

控制台自己的接口（另有 `/p/<svc>/…` 与 `/api/v1|v3/…` 是同源反向代理，转发给 *arr / qB）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 控制台页面 |
| POST | `/api/search` | 搜索候选，`{term, kind}` |
| POST | `/api/movie` | 查片 → 添加 → 触发搜索，`{name 或 tmdbId 或 imdbId, profile?, rootFolderPath?, dryRun?}` |
| POST | `/api/movies/bulk` | 批量添加电影 `{names:[]}` |
| POST | `/api/movie/<id>/search` | 重新搜索该片 |
| DELETE | `/api/movie/<id>` | 移除影片 |
| GET | `/api/movies` | 电影库 |
| POST | `/api/series` | 添加剧集（监控全部正片季）`{name 或 tvdbId, profile?}` |
| POST | `/api/series/bulk` | 批量追剧 `{names:[]}` |
| POST | `/api/series/<id>/search` | 重新搜索该剧 |
| DELETE | `/api/series/<id>` | 移除剧集 |
| GET | `/api/series` | 剧集库（含集数进度） |
| GET | `/api/queue?kind=` | 下载队列（`kind` 取 `movie` / `tv` / `all`，含总速度） |
| DELETE | `/api/queue/<id>?kind=` | 取消队列项 |
| GET | `/api/profiles?kind=` | 画质档位 |
| GET | `/api/rootfolders?kind=` | 根目录列表 |
| GET | `/api/discover?kind&cat&page&genre&country&decade&rating&runtime&sort&refresh` | 发现墙数据（TMDB，带缓存） |
| GET | `/api/douban?kind&cat&page` | 发现墙数据（豆瓣：热门 / 高分 / 最新 / 华语 + 剧集分类） |
| POST | `/api/discover/add` | 从发现墙加入下载（支持 `tmdbId` 或 `name` 按片名经 TMDB 解析） |
| GET | `/api/douban/resolve?kind&title` | 豆瓣卡片标题 → TMDB id（详情 / 添加前置解析） |
| GET | `/api/detail?kind&tmdbId&refresh` | 影片 / 剧集详情（含 `cast` 演职表、`similar` 相似推荐） |
| GET | `/api/calendar?start&end` | 上映与播出日历（`YYYY-MM-DD`） |
| GET | `/api/system` | 系统状态（磁盘 / 版本 / 服务） |
| GET | `/api/history?kind&limit&offset` | 抓取历史 |
| GET | `/api/indexers` | 索引器健康 |
| GET / POST | `/api/indexers/seed` | 查播种状态 / 手动触发播种 |
| POST | `/api/indexers/<id>/enable` | 重新启用失效索引器 |
| GET | `/api/webhook` | 通知配置与最近发送记录 |
| POST | `/api/webhook/test` | 发送测试通知 |
| GET / POST | `/api/config` | 读 / 写配置（写 `.env`） |
| GET | `/api/config_test` | 真实连通性测试 |
| GET | `/api/nettest` | 出网自检 |

装了 `AUTOPILOT_TOKEN` 时，以上所有接口都需要带 `?token=该值`。

---

## 故障排查

| 症状 | 根因 | 处理 |
|---|---|---|
| 添加影片 / 剧集报 HTTP 500，日志有 `attempt to write a readonly database` | 容器 `PUID/PGID` 与卷属主不一致 | 重新执行 `./deploy.sh`（会自动对齐并 chown） |
| *arr 里添加入库报「路径在容器内不存在」 | 宿主机 `$DATA_DIR` 下缺 `movies` / `tv` / `downloads` | `deploy.sh` 已自动创建；手动部署自己 `mkdir -p` |
| *arr 的「测试」按钮连 qBittorrent / Prowlarr 返回 502 | 内网互访被 squid 拦走 | 已在 compose 给 *arr 配 `NO_PROXY`；若改过服务名需同步 |
| 发现墙空白 / 搜索没结果 | 没填 TMDB Key，或出网不通 | 配置页填 Key；「测试外网连通性」应返回 204 |
| 索引器大面积红 / 1337x 一直失败 | 站点有 Cloudflare 挑战 | 确认 `flaresolverr` 容器 running（会自动打 cf 标签走它） |
| 页面返回 401 | 设了 `AUTOPILOT_TOKEN` | 访问时带上 `?token=你的口令` |
| 改了代理但没生效 | 只改了 `.env` 没重启 | 在页面上保存（会自动重启出口容器）；手动改 `.env` 后需 `docker compose restart proxy-forwarder` |
| 下载完 *arr 不导入 / 导入很慢 | 下载目录与媒体库不在同一挂载点，硬链接失效只能复制 | 让 `QB_SAVE_PATH` 与 `MOVIE_ROOT` / `TV_ROOT` 同处 `$DATA_DIR` 下 |
| 页面能开但什么都点不动 | 页面 HTML 里内联 JS 被破坏（改了 `app.py` 的 `PAGE` 常量） | 内联脚本必须放在 Python 的 `raw(r""" … """)` 里，否则 `\r?\n` 会被当控制字符 |

---

## 目录结构

```
docker-compose.yml        # 整套栈定义（7 个服务）
.env.example              # 配置模板，deploy/install 会复制为 .env
install.sh                # 一键安装引导（装 docker/git、克隆、填 .env、起栈）
deploy.sh                 # 幂等部署（对齐 PUID/PGID、建数据目录、compose up -d）
squid/
  squid.conf.template     # squid 模板，cache_peer / never_direct 由入口脚本注入
  entrypoint.sh           # 读 .env 渲染 squid.conf 并前台启动 squid
autopilot/
  app.py                  # 控制台全部代码（纯标准库：HTTP 服务 + 页面 + *arr/qB 封装）
  Dockerfile
data/                     # 默认数据目录（下载 + 媒体库），由 deploy.sh 创建
```

---

## 设计取舍

- **不做媒体服务器**：本栈只负责「找 + 下 + 归位」。想看电影电视剧，把它接给 Jellyfin / Emby / Plex 即可。
- **不碰宿主机网络**：`install.sh` 不会去改路由、NAT、防火墙，也不会去动已有的代理软件。它只要求「要么能直连外网，要么给一个 HTTP 代理」。
- **只有一个出口**：出网全部收敛到 `proxy-forwarder`，换来的是「改一处、全栈生效」。代价是它挂了全栈就出不了网——这是刻意的取舍。
- **控制台不用框架**：单文件标准库实现，没有构建步骤、没有依赖树，容器镜像小、启动快，代价是页面代码要手写（改内联脚本时注意别破坏 `PAGE` 的 `raw` 字符串）。

---

*本文档对应 `main` 分支当前代码。若发现文档与行为不一致，以代码为准并欢迎提 issue。*
