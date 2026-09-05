#!/usr/bin/env python3
"""
autopilot — 发片名自动下服务（无外部依赖，仅用 Python 标准库）

功能 / Features:
  - 网页（GET /）：搜索候选匹配、画质选择、批量添加、下载队列（速度/ETA/取消）、
    媒体库海报墙、系统状态（磁盘/索引器/版本）
  - 电影 / 剧集双模式：页面顶部切换，搜索·队列·库三块按模式走不同数据源。
  - API（电影）：
      POST /api/search        {term, kind?}           -> TMDB/Sonarr 候选匹配
      POST /api/movie         {name|tmdbId|imdbId, profile?, rootFolderPath?, dryRun?} -> 查片→添加→搜索
      POST /api/movies/bulk   {names:[...]}           -> 批量添加
      GET  /api/queue                                   -> 下载队列
      DELETE /api/queue/<id>                             -> 取消队列项
      GET  /api/movies                                  -> 媒体库
      DELETE /api/movie/<id>                            -> 移除影片
      GET  /api/profiles                               -> 画质档位
      GET  /api/system                                 -> 系统状态
      GET  /api/history?kind=movie|tv|all              -> 抓取历史（抓取/入库/失败）
      GET  /api/indexers                               -> 索引器只读健康（Prowlarr）
      GET  /api/discover?kind=movie|tv&cat=popular     -> TMDB 发现墙（热门/热映/即将上映/高分）
      POST /api/discover/add {kind, tmdbId, profile?, seasonMode?, rootFolderPath?} -> 一键添加到下载队列
  - API（剧集，Sonarr）：
      POST /api/series        {name|tvdbId, profile?} -> 搜剧→添加（监控全部正片季）→搜索
      POST /api/series/bulk   {names:[...]}           -> 批量追剧
      POST /api/series/<id>/search                    -> 重新搜索该剧
      GET  /api/series                                -> 剧集库（含集数进度）
      DELETE /api/series/<id>                         -> 移除剧集
      GET  /api/queue?kind=tv                         -> 剧集下载队列
      DELETE /api/queue/<id>?kind=tv                  -> 取消剧集队列项
      GET  /api/profiles?kind=tv                      -> Sonarr 画质档位
  - 运行在 media_default 网络内，靠服务名 `radarr` 访问；API Key
    从 Radarr 的 config.xml 自动读取，无需硬编码。可选接 Prowlarr 显示索引器健康。
"""
import os
import re
import json
import socket
import time
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
from urllib.request import Request, urlopen
import urllib.error
from urllib.parse import urlencode, parse_qs, urlparse, quote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ssl
SSL_CTX = ssl._create_unverified_context()  # qBittorrent 现已启用自签 HTTPS
import threading
import concurrent.futures as _cf

RADARR_URL = os.environ.get("RADARR_URL", "http://radarr:7878")
RADARR_CONFIG = os.environ.get("RADARR_CONFIG", "/radarr-config/config.xml")
SONARR_URL = os.environ.get("SONARR_URL", "http://sonarr:8989")
SONARR_CONFIG = os.environ.get("SONARR_CONFIG", "/sonarr-config/config.xml")
PROWLARR_URL = os.environ.get("PROWLARR_URL", "http://prowlarr:9696")
PROWLARR_CONFIG = os.environ.get("PROWLARR_CONFIG", "/prowlarr-config/config.xml")
LISTEN_PORT = int(os.environ.get("AUTOPILOT_PORT", "8787"))
TOKEN = os.environ.get("AUTOPILOT_TOKEN", "").strip()
ROOT_FOLDER = os.environ.get("MOVIE_ROOT", "/movies")
TV_ROOT = os.environ.get("TV_ROOT", "/tv")
# 系统状态里要探测的其余服务（都在同一 compose 网络内，用服务名访问）
QBIT_URL = os.environ.get("QBITTORRENT_URL", "https://qbittorrent:8085")
QBIT_USER = os.environ.get("QBITTORRENT_USER", "admin")
QBIT_PASS = os.environ.get("QBITTORRENT_PASS", "MediaFn2026")
FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://flaresolverr:8191")
PROXY_URL = os.environ.get("PROXY_URL", "http://proxy-forwarder:3128")
WEBHOOK_URL = os.environ.get("AUTOPILOT_WEBHOOK_URL", "").strip()
# 发现墙（🎯 发现 Tab）数据源：TMDB。免费的 v3 API Key 在 https://www.themoviedb.org/ 申请，
# 在「配置」页的 TMDB API Key 一栏填写即可（落盘到 .env 并热更新，无需重建容器）。出网默认走与 FlareSolverr 相同的代理
# （本 NAS 容器无直连外网，统一经 OpenClash）；若需直连可设 TMDB_PROXY=（空）。
TMDB_KEY = os.environ.get("TMDB_API_KEY", "").strip()
TMDB_LANG = os.environ.get("TMDB_LANG", "zh-CN").strip() or "zh-CN"
TMDB_PROXY = os.environ.get("TMDB_PROXY", PROXY_URL)
TMDB_BASE = os.environ.get("TMDB_BASE", "https://api.themoviedb.org/3")
TMDB_IMG = "https://image.tmdb.org/t/p/w342"


# 启动时从 .env(MEDIA_ENV) 覆盖：使页面保存的 TMDB_API_KEY / 代理在容器重启后仍生效。
# 容器 env 不含这些键（compose 仅注入固定代理），故以 .env 落盘值为准，保证前端设置可持久化。
def _apply_env_file_overrides():
    p = os.environ.get("MEDIA_ENV", "/app/.env")
    try:
        kv = {}
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip()
    except Exception:
        return
    global TMDB_KEY, PROXY_URL, TMDB_PROXY
    if kv.get("TMDB_API_KEY"):
        TMDB_KEY = kv["TMDB_API_KEY"]
    if kv.get("PROXY_URL"):
        PROXY_URL = kv["PROXY_URL"]
    if kv.get("TMDB_PROXY"):
        TMDB_PROXY = kv["TMDB_PROXY"]


_apply_env_file_overrides()
# 发现墙分类映射：kind -> {cat: tmdb_path}
_DISCOVER_MAP = {
    "movie": {
        "popular": "/movie/popular",
        "now_playing": "/movie/now_playing",
        "upcoming": "/movie/upcoming",
        "top_rated": "/movie/top_rated",
    },
    "tv": {
        "popular": "/tv/popular",
        "on_the_air": "/tv/on_the_air",
        "top_rated": "/tv/top_rated",
    },
}
_START_TS = time.time()

_cache = {"radarr_key": None, "sonarr_key": None, "prowlarr_key": None,
          "profiles": None, "series_profiles": None}
# RLock：get_profiles 持锁时会间接调用 r_req->get_radarr_key（也要抢同一把锁），
# 用可重入锁避免线程把自己锁死。
_lock = threading.RLock()


# ---------- Radarr / Prowlarr 客户端 ----------
def _read_key(xml_path, tag="ApiKey"):
    try:
        tree = ET.parse(xml_path)
        return tree.getroot().findtext(tag)
    except Exception:
        return None


def get_radarr_key():
    with _lock:
        if _cache["radarr_key"] is not None:
            return _cache["radarr_key"]
        k = _read_key(RADARR_CONFIG)
        _cache["radarr_key"] = k
        return k


def get_prowlarr_key():
    if not PROWLARR_URL:
        return None
    with _lock:
        if "prowlarr_key" in _cache and _cache["prowlarr_key"] is not None:
            return _cache["prowlarr_key"]
        k = _read_key(PROWLARR_CONFIG)
        _cache["prowlarr_key"] = k
        return k


def r_req(method, path, data=None, timeout=120):
    key = get_radarr_key()
    if not key:
        raise RuntimeError("无法读取 Radarr API Key（config.xml 不存在或无权限）")
    return _req(RADARR_URL, key, method, path, data, timeout)


def p_req(method, path, data=None, timeout=60):
    key = get_prowlarr_key()
    if not key:
        raise RuntimeError("Prowlarr 未配置")
    return _req(PROWLARR_URL, key, method, path, data, timeout)


def _req(base, key, method, path, data, timeout):
    url = base + path
    headers = {"X-Api-Key": key, "Accept": "application/json",
               "Content-Type": "application/json"}
    body = json.dumps(data).encode() if data is not None else None
    req = Request(url, data=body, headers=headers, method=method)
    with urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else None


def get_profiles():
    # 不在持锁期间发起网络请求，避免阻塞其它接口
    if _cache["profiles"] is not None:
        return _cache["profiles"]
    try:
        profs = r_req("GET", "/api/v3/qualityprofile") or []
    except Exception:
        profs = []
    _cache["profiles"] = profs
    return profs


def pick_profile(name=None):
    profs = get_profiles()
    if not profs:
        return 1
    if name:
        for p in profs:
            if name.lower() in (p.get("name") or "").lower():
                return p["id"]
    for p in profs:
        n = (p.get("name") or "").lower()
        if "1080" in n:
            return p["id"]
    for p in profs:
        n = (p.get("name") or "").lower()
        if "hd" in n or "web" in n:
            return p["id"]
    return profs[0]["id"]


def get_sonarr_key():
    with _lock:
        if _cache["sonarr_key"] is not None:
            return _cache["sonarr_key"]
        k = _read_key(SONARR_CONFIG)
        _cache["sonarr_key"] = k
        return k


def s_req(method, path, data=None, timeout=120):
    key = get_sonarr_key()
    if not key:
        raise RuntimeError("无法读取 Sonarr API Key（config.xml 不存在或无权限）")
    return _req(SONARR_URL, key, method, path, data, timeout)


def get_series_profiles():
    if _cache["series_profiles"] is not None:
        return _cache["series_profiles"]
    try:
        profs = s_req("GET", "/api/v3/qualityprofile") or []
    except Exception:
        profs = []
    _cache["series_profiles"] = profs
    return profs


def pick_series_profile(name=None):
    profs = get_series_profiles()
    if not profs:
        return 1
    if name:
        for p in profs:
            if name.lower() in (p.get("name") or "").lower():
                return p["id"]
    for p in profs:
        if "1080" in (p.get("name") or "").lower():
            return p["id"]
    for p in profs:
        n = (p.get("name") or "").lower()
        if "hd" in n or "web" in n:
            return p["id"]
    return profs[0]["id"]


def sonarr_root():
    """优先复用 Sonarr 里已配置的根目录，避免硬编码与实际挂载不一致。"""
    try:
        rfs = s_req("GET", "/api/v3/rootfolder") or []
        if rfs:
            for r in rfs:
                if (r.get("path") or "").rstrip("/") == TV_ROOT.rstrip("/"):
                    return TV_ROOT
            return rfs[0].get("path") or TV_ROOT
    except Exception:
        pass
    return TV_ROOT


def radarr_root():
    """优先复用 Radarr 里已配置的根目录；缺失则回退 ROOT_FOLDER。
    修复：之前 add_movie 直接写死 ROOT_FOLDER(默认 /movies)，若 Radarr 实际根目录
    是 /data/movies 就会 400。这里改为动态读取已配置根目录。"""
    try:
        rfs = r_req("GET", "/api/v3/rootfolder") or []
        if rfs:
            for r in rfs:
                if (r.get("path") or "").rstrip("/") == ROOT_FOLDER.rstrip("/"):
                    return ROOT_FOLDER
            return rfs[0].get("path") or ROOT_FOLDER
    except Exception:
        pass
    return ROOT_FOLDER


def list_rootfolders(kind="movie"):
    """返回 Radarr/Sonarr 已配置的根目录，供前端选择下载落盘位置。"""
    try:
        if kind == "tv":
            rfs = s_req("GET", "/api/v3/rootfolder") or []
        else:
            rfs = r_req("GET", "/api/v3/rootfolder") or []
        return [{"path": r.get("path"), "free": r.get("freeSpace"),
                "total": r.get("totalSpace")} for r in rfs]
    except Exception:
        return []


def _poster_of(item):
    for img in item.get("images", []) or []:
        if img.get("coverType") == "poster":
            return img.get("remoteUrl")
    return None


# ---------- 核心逻辑 ----------
def lookup_by_term(term):
    return r_req("GET", "/api/v3/movie/lookup?" + urlencode({"term": term})) or []


def lookup_by_tmdb(tmdb_id):
    r = r_req("GET", "/api/v3/movie/lookup/tmdb?" + urlencode({"tmdbId": tmdb_id}))
    if isinstance(r, list):
        return r
    if isinstance(r, dict):
        return [r]
    return []


def lookup_by_imdb(imdb_id):
    r = r_req("GET", "/api/v3/movie/lookup/imdb?" + urlencode({"imdbId": imdb_id}))
    if isinstance(r, list):
        return r
    if isinstance(r, dict):
        return [r]
    return []


def search_candidates(term):
    try:
        res = lookup_by_term(term)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    lib = _movie_state_by_tmdb()
    out = []
    for r in res[:12]:
        rating = (r.get("ratings") or {}).get("value")
        out.append({
            "tmdbId": r.get("tmdbId"),
            "title": r.get("title"),
            "year": r.get("year"),
            "overview": (r.get("overview") or "")[:220],
            "rating": round(rating, 1) if isinstance(rating, (int, float)) else None,
            "poster": _poster_of(r),
            "inLibrary": lib.get(r.get("tmdbId")),
        })
    return {"ok": True, "candidates": out}


# ---------- 发现墙（TMDB 热门/热映/即将上映/高分） ----------
def tmdb_get(path, params=None):
    """带 api_key 的 TMDB v3 GET；返回 (data, err)。err 为空表示成功。"""
    if not TMDB_KEY:
        return None, "未配置 TMDB_API_KEY（请在「配置」页填写 TMDB API Key）"
    params = dict(params or {})
    params["api_key"] = TMDB_KEY
    if "language" not in params:
        params["language"] = TMDB_LANG
    url = TMDB_BASE + path + "?" + urlencode(params)
    handlers = []
    if TMDB_PROXY:
        handlers.append(_ureq.ProxyHandler({"https": TMDB_PROXY, "http": TMDB_PROXY}))
    opener = _ureq.build_opener(*handlers)
    req = _ureq.Request(url, headers={"Accept": "application/json"})
    last_err = ""
    # TMDB 经 Clash 出网是「节点抖动」：单个连接可能 timeout / SSL EOF，
    # 但换一条新连接（重试）常常就通。故对瞬断做最多 3 次重试。
    for attempt in range(1, 4):
        try:
            with opener.open(req, timeout=25) as r:
                raw = r.read().decode("utf-8")
                return json.loads(raw), None
        except urllib.error.HTTPError as ex:
            # 4xx/5xx 是确定错误，不重试
            return None, "TMDB HTTP %s: %s" % (ex.code, ex.reason)
        except Exception as e:
            last_err = str(e)[:160]
            if attempt < 3:
                time.sleep(0.8)
    return None, last_err


def _build_tmdb_query(kind, cat, page, genre=None, country=None,
                     decade=None, rating=None, runtime=None, sort=None):
    """构造单个类型（movie/tv）的发现请求 path + params。
    sort: pop(热度) / rating(评分) / date(上映日期)，覆盖 cat 默认排序。"""
    cats = _DISCOVER_MAP.get(kind, {})
    if cat not in cats:
        cat = "popular"
    params = {"page": page}
    filters = genre or country or decade or rating or runtime
    # 排序映射：自定义 sort 优先；否则按 cat 默认排序
    _sort_map = {
        "movie": {"popular": "popularity.desc", "now_playing": "popularity.desc",
                  "upcoming": "primary_release_date.asc", "top_rated": "vote_average.desc"},
        "tv": {"popular": "popularity.desc", "on_the_air": "first_air_date.desc",
               "top_rated": "vote_average.desc"},
    }
    _sort_by = {"pop": "popularity.desc",
                "rating": "vote_average.desc",
                "date": ("primary_release_date.desc" if kind == "movie" else "first_air_date.desc")}
    if sort in _sort_by:
        effective_sort = _sort_by[sort]
    else:
        effective_sort = _sort_map.get(kind, {}).get(cat, "popularity.desc")
    # 无筛选且无自定义排序 -> 用 TMDB 分类专属端点（更准）；否则走 /discover 套排序
    if not filters and not (sort and sort != "pop"):
        path = cats.get(cat) or ("/movie/popular" if kind == "movie" else "/tv/popular")
        return path, params
    disc = "/discover/movie" if kind == "movie" else "/discover/tv"
    params["sort_by"] = effective_sort
    if effective_sort.endswith("vote_average.desc"):
        params["vote_count.gte"] = 200  # 高分榜避免冷门低票影片刷屏
    if genre:
        params["with_genres"] = genre
    if country:
        params["with_origin_country"] = country
    _date_field = "primary_release_date" if kind == "movie" else "first_air_date"
    if decade and "," in str(decade):
        _gy, _ly = str(decade).split(",", 1)
        params[_date_field + ".gte"] = _gy + "-01-01"
        params[_date_field + ".lte"] = _ly + "-12-31"
    if rating:
        try:
            params["vote_average.gte"] = float(rating)
        except (ValueError, TypeError):
            pass
    if runtime and "," in str(runtime):
        _lo, _hi = str(runtime).split(",", 1)
        if _lo:
            params["with_runtime.gte"] = int(_lo)
        if _hi and int(_hi) < 9999:
            params["with_runtime.lte"] = int(_hi)
    return disc, params


def _norm_item(it, kind):
    """把 TMDB 单条结果归一化为统一卡片结构（带自身 kind，便于合并后区分）。"""
    is_tv = (kind == "tv")
    title = it.get("title") if not is_tv else it.get("name")
    date = it.get("release_date") if not is_tv else it.get("first_air_date")
    year = date[:4] if date else ""
    poster_path = it.get("poster_path")
    poster = (TMDB_IMG + poster_path) if poster_path else None
    rating = it.get("vote_average")
    return {
        "tmdbId": it.get("id"),
        "kind": kind,
        "title": title,
        "year": year,
        "overview": (it.get("overview") or "")[:200],
        "rating": round(rating, 1) if isinstance(rating, (int, float)) else None,
        "poster": poster,
    }


# 合并模式（电影+剧集）下，各分类对应的 (类型, 该类型分类) 列表
# 键名必须与前端 CATS 的 cat 值一致：popular/top_rated/now_playing/upcoming/on_the_air
# 否则 _ALL_PAIRS.get(cat, popular) 会静默 fallback 到 popular，导致内容塌缩
_ALL_PAIRS = {
    "popular": [("movie", "popular"), ("tv", "popular")],
    "top_rated": [("movie", "top_rated"), ("tv", "top_rated")],
    "now_playing": [("movie", "now_playing"), ("tv", "on_the_air")],
    "upcoming": [("movie", "upcoming")],  # 剧集无"即将上映"等效分类
    "on_the_air": [("tv", "on_the_air")],  # 电影无"在播"等效分类
}


def tmdb_discover(kind="movie", cat="popular", page=1, genre=None, country=None,
                decade=None, rating=None, runtime=None, sort=None):
    """拉取 TMDB 某分类的影视列表（支持分页），归一化为统一的卡片结构。
    kind="all" 时合并电影与剧集：分别拉取后交错合并，每张卡片保留自身 kind。"""
    if not TMDB_KEY:
        return {"ok": False, "configured": False,
                "error": "未配置 TMDB_API_KEY；请在「配置」页的 TMDB API Key 一栏填写"
                         "（免费，在 themoviedb.org 申请）。"}
    try:
        page = max(1, int(page or 1))
    except (ValueError, TypeError):
        page = 1
    page = min(page, 500)  # TMDB 列表接口实际最多 500 页，超出返回 HTTP 400
    if kind != "all":
        kind = kind if kind in ("movie", "tv") else "movie"
        path, params = _build_tmdb_query(kind, cat, page, genre, country, decade, rating, runtime, sort)
        data, err = tmdb_get(path, params)
        if err:
            return {"ok": False, "configured": True, "error": err}
        items = data.get("results", []) if isinstance(data, dict) else []
        out = [_norm_item(it, kind) for it in items]
        return {"ok": True, "configured": True, "kind": kind, "cat": cat,
                "genre": genre, "country": country, "decade": decade,
                "rating": rating, "runtime": runtime,
                "page": data.get("page", page),
                "totalPages": min(data.get("total_pages", 1), 500),
                "totalResults": data.get("total_results", len(out)),
                "items": out}
    # kind == "all"：合并电影 + 剧集
    pairs = _ALL_PAIRS.get(cat, _ALL_PAIRS["popular"])
    per_type = []
    total_pages_list, total_results_list = [], []
    for k, c in pairs:
        path, params = _build_tmdb_query(k, c, page, genre, country, decade, rating, runtime, sort)
        data, err = tmdb_get(path, params)
        if err:
            continue  # 单类型失败不致命，跳过该类型
        items = data.get("results", []) if isinstance(data, dict) else []
        per_type.append([_norm_item(it, k) for it in items])
        total_pages_list.append(min(data.get("total_pages", 1), 500))
        total_results_list.append(data.get("total_results", 0))
    if not per_type:
        return {"ok": False, "configured": True,
                "error": "全部子查询失败（电影/剧集 TMDB 请求均失败），可能是出网抖动，请稍后重试或点「刷新」。"}
    # 交错合并，避免电影/剧集各自成块
    merged = []
    maxlen = max((len(x) for x in per_type), default=0)
    for i in range(maxlen):
        for lst in per_type:
            if i < len(lst):
                merged.append(lst[i])
    return {"ok": True, "configured": True, "kind": "all", "cat": cat,
            "genre": genre, "country": country, "decade": decade,
            "rating": rating, "runtime": runtime,
            "page": page,
            "totalPages": min(total_pages_list) if total_pages_list else 1,
            "totalResults": sum(total_results_list),
            "items": merged}


_ISO_CN = {"US": "美国", "CN": "中国大陆", "HK": "中国香港", "TW": "中国台湾", "JP": "日本",
            "KR": "韩国", "GB": "英国", "FR": "法国", "DE": "德国", "IT": "意大利", "ES": "西班牙",
            "IN": "印度", "TH": "泰国", "RU": "俄罗斯", "CA": "加拿大", "AU": "澳大利亚",
            "BR": "巴西", "MX": "墨西哥", "KP": "朝鲜", "VN": "越南", "PH": "菲律宾"}


def tmdb_detail(kind="movie", tmdb_id=None):
    """拉取单部影视的完整信息（详情页用）：简介、类型、国别、时长、评分等。"""
    if not tmdb_id:
        return {"ok": False, "error": "缺少 tmdbId"}
    if not TMDB_KEY:
        return {"ok": False, "configured": False,
                "error": "未配置 TMDB_API_KEY；请在「配置」页的 TMDB API Key 一栏填写"
                         "（免费，在 themoviedb.org 申请）。"}
    kind = kind if kind in ("movie", "tv") else "movie"
    path = ("/movie/%s" if kind == "movie" else "/tv/%s") % tmdb_id
    data, err = tmdb_get(path, {"append_to_response": "external_ids"})
    if err:
        return {"ok": False, "configured": True, "error": err}
    is_tv = (kind == "tv")
    title = data.get("title") if not is_tv else data.get("name")
    orig = data.get("original_title") if not is_tv else data.get("original_name")
    date = data.get("release_date") if not is_tv else data.get("first_air_date")
    year = date[:4] if date else ""
    poster_path = data.get("poster_path")
    poster = (TMDB_IMG + poster_path) if poster_path else None
    backdrop_path = data.get("backdrop_path")
    backdrop = (TMDB_IMG.replace("/w342", "/w780") + backdrop_path) if backdrop_path else None
    genres = [g.get("name") for g in data.get("genres", []) if g.get("name")]
    if is_tv:
        countries = [_ISO_CN.get(c, c) for c in data.get("origin_country", [])]
    else:
        countries = [_ISO_CN.get(c.get("iso_3166_1"), c.get("name")) for c in data.get("production_countries", []) if c.get("iso_3166_1") or c.get("name")]
    runtime = data.get("runtime")
    if is_tv:
        rt = data.get("episode_run_time") or []
        runtime = rt[0] if rt else None
    ext = data.get("external_ids") or {}
    tvdb = ext.get("tvdb_id") if is_tv else None
    rating = data.get("vote_average")
    return {"ok": True, "configured": True, "kind": kind, "tmdbId": tmdb_id,
            "title": title, "originalTitle": orig, "year": year,
            "poster": poster, "backdrop": backdrop, "genres": genres, "countries": countries,
            "runtime": runtime, "seasons": data.get("number_of_seasons") if is_tv else None,
            "episodes": data.get("number_of_episodes") if is_tv else None,
            "overview": data.get("overview") or "", "rating": round(rating, 1) if isinstance(rating, (int, float)) else None,
            "tagline": data.get("tagline") or "", "status": data.get("status") or "",
            "tvdbId": tvdb}


from concurrent.futures import ThreadPoolExecutor
_TMDB_PREFETCH = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tmdb-prefetch")

def _prefetch_details(items, limit=20):
    """发现列表返回后，后台把卡片详情预热进 24h sqlite 缓存，
    使随后点击卡片秒开（避开每次冷拉 TMDB 的 8~25s 抖动）。
    跳过已缓存项；共享线程池把并发控制在 4；失败静默忽略。"""
    if not items:
        return
    todos = []
    for it in items[:limit]:
        tid = it.get("tmdbId")
        if not tid:
            continue
        kind = it.get("kind") or "movie"
        ckey = ("detail", kind, str(tid))
        if _tmdb_cache_get(ckey) is not None:
            continue
        todos.append((kind, str(tid)))
    if not todos:
        return

    def _one(arg):
        kind, tid = arg
        try:
            d = tmdb_detail(kind, tid)
            if d.get("ok"):
                _tmdb_cache_put(("detail", kind, tid), d)
        except Exception:
            pass

    try:
        list(_TMDB_PREFETCH.map(_one, todos))
    except Exception:
        pass


def discover_add(kind="movie", tmdb_id=None, profile=None, root_folder=None, season_mode="all"):
    """发现墙一键添加：电影直接走 add_movie(tmdb_id)；剧集先取 tvdbId 再走 add_series。"""
    if not tmdb_id:
        return {"ok": False, "error": "缺少 tmdbId"}
    if kind == "tv":
        ext, err = tmdb_get("/tv/%s/external_ids" % tmdb_id)
        if err:
            return {"ok": False, "error": "获取剧集 TVDB ID 失败: " + err}
        tvdb = (ext or {}).get("tvdb_id")
        if not tvdb:
            return {"ok": False, "error": "TMDB 未提供该剧的 TVDB ID，无法交由 Sonarr 添加"}
        return add_series(tvdb_id=tvdb, profile=profile, season_mode=season_mode, root_folder=root_folder)
    return add_movie(tmdb_id=tmdb_id, profile=profile, root_folder=root_folder)


def add_movie(name=None, tmdb_id=None, imdb_id=None, profile=None,
              root_folder=None, dry_run=False):
    try:
        if tmdb_id:
            results = lookup_by_tmdb(tmdb_id)
        elif imdb_id:
            results = lookup_by_imdb(imdb_id)
        else:
            results = lookup_by_term(name)
    except Exception as e:
        return {"ok": False, "error": "Radarr 搜片名失败: %s" % e}
    if not results:
        return {"ok": False, "error": "没匹配到「%s」" % (name or tmdb_id or imdb_id)}

    chosen = results[0]
    if not tmdb_id and name:
        nl = (name or "").strip().lower()
        for r in results:
            if (r.get("title") or "").lower() == nl:
                chosen = r
                break

    tmdb = chosen.get("tmdbId")
    title = chosen.get("title")
    year = chosen.get("year")
    prof_id = pick_profile(profile)

    try:
        existing = r_req("GET", "/api/v3/movie?" + urlencode({"tmdbId": tmdb}))
    except Exception:
        existing = None

    if dry_run:
        return {"ok": True, "dryRun": True, "tmdbId": tmdb, "title": title,
                "year": year, "profileId": prof_id,
                "alreadyInRadarr": bool(existing), "action": "仅预览，未添加/未下载"}

    if existing:
        mid = existing[0]["id"]
        try:
            r_req("POST", "/api/v3/command", {"name": "MoviesSearch", "movieIds": [mid]})
        except Exception:
            pass
        return {"ok": True, "movieId": mid, "tmdbId": tmdb, "title": title,
                "year": year, "action": "已存在，重新触发搜索", "profileId": prof_id}

    payload = {
        "tmdbId": tmdb, "title": title, "qualityProfileId": prof_id,
        "rootFolderPath": root_folder or radarr_root(), "monitored": True,
        "minimumAvailability": "released", "addOptions": {"searchForMovie": True},
    }
    try:
        created = r_req("POST", "/api/v3/movie", payload)
    except Exception as e:
        return {"ok": False, "error": "Radarr 添加影片失败: %s" % e}

    mid = created.get("id") if isinstance(created, dict) else None
    try:
        if mid:
            r_req("POST", "/api/v3/command", {"name": "MoviesSearch", "movieIds": [mid]})
    except Exception:
        pass
    return {"ok": True, "movieId": mid, "tmdbId": tmdb, "title": title,
            "year": year, "action": "已添加并触发搜索", "profileId": prof_id}


def _queue_records(rq, extra=""):
    """Radarr / Sonarr 的队列结构一致，只是筛选参数不同，故抽出公共解析。"""
    path = "/api/v3/queue?pageSize=30&sortDirection=descending&sortKey=timeleft" + extra
    try:
        q = rq("GET", path)
    except Exception:
        return []
    out = []
    for it in (q or {}).get("records", []):
        dci = it.get("downloadClientInfo") or {}
        out.append({
            "id": it.get("id"), "title": it.get("title"), "status": it.get("status"),
            "size": it.get("size"), "sizeleft": it.get("sizeleft"),
            "progress": round((it.get("progress") or 0) * 100, 1),
            "downloadClient": it.get("downloadClient"),
            "timeleft": it.get("timeleft"),
            "speed": dci.get("speed") if isinstance(dci, dict) else None,
            "indexer": it.get("indexer"), "protocol": it.get("protocol"),
            "eta": it.get("estimatedCompletionTime"),
        })
    return out


def queue_status(movie_id=None):
    return _queue_records(r_req, ("&movieId=%d" % int(movie_id)) if movie_id else "")


def series_queue_status(series_id=None):
    return _queue_records(s_req, ("&seriesId=%d" % int(series_id)) if series_id else "")


def all_queue_status():
    """电影 + 剧集合并队列，每项带 kind 便于前端区分与取消。"""
    out = []
    for it in queue_status():
        it["kind"] = "movie"
        out.append(it)
    for it in series_queue_status():
        it["kind"] = "tv"
        out.append(it)
    return out


def remove_from_queue(qid, remove_from_client=True):
    params = urlencode({"removeFromClient": str(remove_from_client).lower(),
                        "blacklist": "false"})
    r_req("DELETE", "/api/v3/queue/%d?%s" % (int(qid), params))
    return {"ok": True}


def list_movies():
    try:
        ms = r_req("GET", "/api/v3/movie?pageSize=1000") or []
    except Exception:
        return []
    # 标记当前正在下载中的影片（避免把"下载中"误标成"待源"）
    downloading_ids = set()
    try:
        q = r_req("GET", "/api/v3/queue?pageSize=200") or {}
        for it in q.get("records", []):
            mid = it.get("movieId")
            if mid:
                downloading_ids.add(mid)
    except Exception:
        pass
    out = []
    for m in ms:
        mf = m.get("movieFile") or {}
        mid = m.get("id")
        has = m.get("hasFile", False)
        out.append({
            "id": mid, "title": m.get("title"), "year": m.get("year"),
            "downloaded": has, "monitored": m.get("monitored"),
            "downloading": (mid in downloading_ids) and (not has),
            "poster": _poster_of(m), "sizeOnDisk": m.get("sizeOnDisk"),
            "quality": (mf.get("quality") or {}).get("quality", {}).get("name", ""),
        })
    return out


def remove_movie(mid, delete_files=True):
    params = urlencode({"deleteFiles": str(delete_files).lower()})
    r_req("DELETE", "/api/v3/movie/%d?%s" % (int(mid), params))
    return {"ok": True}


def _movie_state_by_tmdb():
    """tmdbId -> 状态(downloaded/downloading/waiting)，用于搜索结果标注"已在库"。"""
    m = {}
    try:
        ms = r_req("GET", "/api/v3/movie?pageSize=1000") or []
    except Exception:
        return m
    dl = set()
    try:
        q = r_req("GET", "/api/v3/queue?pageSize=200") or {}
        for it in q.get("records", []):
            if it.get("movieId"):
                dl.add(it["movieId"])
    except Exception:
        pass
    for x in ms:
        tid = x.get("tmdbId")
        if tid is None:
            continue
        m[tid] = ("downloaded" if x.get("hasFile")
                  else ("downloading" if x.get("id") in dl else "waiting"))
    return m


def _series_state_by_tvdb():
    """tvdbId -> 状态，用于搜索结果标注"已在库"。"""
    m = {}
    try:
        ss = s_req("GET", "/api/v3/series?pageSize=1000") or []
    except Exception:
        return m
    dl = set()
    try:
        q = s_req("GET", "/api/v3/queue?pageSize=200") or {}
        for it in q.get("records", []):
            if it.get("seriesId"):
                dl.add(it["seriesId"])
    except Exception:
        pass
    for s in ss:
        tid = s.get("tvdbId")
        if tid is None:
            continue
        st = s.get("statistics") or {}
        epf = st.get("episodeFileCount", 0)
        eps = st.get("totalEpisodeCount", 0)
        m[tid] = ("downloaded" if (eps > 0 and epf >= eps)
                  else ("downloading" if s.get("id") in dl else "waiting"))
    return m


# ---------- Sonarr：追剧 ----------
def s_lookup(term_val):
    return s_req("GET", "/api/v3/series/lookup?" + urlencode({"term": term_val})) or []


def search_series_candidates(term):
    try:
        res = s_lookup(term)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    lib = _series_state_by_tvdb()
    out = []
    for r in res[:12]:
        rating = (r.get("ratings") or {}).get("value")
        seasons = r.get("seasons") or []
        out.append({
            "inLibrary": lib.get(r.get("tvdbId")),
            "tvdbId": r.get("tvdbId"),
            "title": r.get("title"),
            "year": r.get("year"),
            "overview": (r.get("overview") or "")[:220],
            "rating": round(rating, 1) if isinstance(rating, (int, float)) else None,
            "poster": _poster_of(r),
            "network": r.get("network"),
            "status": r.get("status"),
            "seasons": sum(1 for s in seasons if s.get("seasonNumber", 0) > 0),
        })
    return {"ok": True, "candidates": out}


def search_all(term):
    """合并搜索：Radarr 查电影 + Sonarr 查剧集，各自归一化后合并，每条带 kind 字段。
    前端据此「搜出什么是什么」——电影与剧集混合展示，按 kind 路由添加。"""
    out = []
    # 电影（Radarr / TMDB lookup）
    try:
        mres = lookup_by_term(term)
    except Exception:
        mres = []
    if not isinstance(mres, list):
        mres = []
    mlib = _movie_state_by_tmdb()
    for r in mres[:12]:
        rating = (r.get("ratings") or {}).get("value")
        out.append({
            "kind": "movie",
            "tmdbId": r.get("tmdbId"),
            "title": r.get("title"),
            "year": r.get("year"),
            "overview": (r.get("overview") or "")[:220],
            "rating": round(rating, 1) if isinstance(rating, (int, float)) else None,
            "poster": _poster_of(r),
            "inLibrary": mlib.get(r.get("tmdbId")),
        })
    # 剧集（Sonarr lookup）
    try:
        sres = s_lookup(term)
    except Exception:
        sres = []
    if not isinstance(sres, list):
        sres = []
    slib = _series_state_by_tvdb()
    for r in sres[:12]:
        rating = (r.get("ratings") or {}).get("value")
        seasons = r.get("seasons") or []
        out.append({
            "kind": "tv",
            "tvdbId": r.get("tvdbId"),
            "title": r.get("title"),
            "year": r.get("year"),
            "overview": (r.get("overview") or "")[:220],
            "rating": round(rating, 1) if isinstance(rating, (int, float)) else None,
            "poster": _poster_of(r),
            "inLibrary": slib.get(r.get("tvdbId")),
            "network": r.get("network"),
            "status": r.get("status"),
            "seasons": sum(1 for s in seasons if s.get("seasonNumber", 0) > 0),
        })
    return {"ok": True, "candidates": out}


def add_series(name=None, tvdb_id=None, profile=None, season_mode="all",
               root_folder=None):
    term_val = ("tvdb:%s" % tvdb_id) if tvdb_id else (name or "")
    try:
        results = s_lookup(term_val)
    except Exception as e:
        return {"ok": False, "error": "Sonarr 搜剧失败: %s" % e}
    if not results:
        return {"ok": False, "error": "没匹配到「%s」" % (name or tvdb_id)}

    chosen = results[0]
    if not tvdb_id and name:
        nl = (name or "").strip().lower()
        for r in results:
            if (r.get("title") or "").lower() == nl:
                chosen = r
                break

    tvdb = chosen.get("tvdbId")
    title = chosen.get("title")
    year = chosen.get("year")
    prof_id = pick_series_profile(profile)
    root = root_folder or sonarr_root()

    # 已存在则只重新触发搜索，避免重复添加
    existing = None
    try:
        existing = s_req("GET", "/api/v3/series?" + urlencode({"tvdbId": tvdb}))
    except Exception:
        existing = None
    if existing:
        sid = existing[0]["id"]
        try:
            s_req("POST", "/api/v3/command", {"name": "SeriesSearch", "seriesIds": [sid]})
        except Exception:
            pass
        return {"ok": True, "seriesId": sid, "tvdbId": tvdb, "title": title,
                "year": year, "action": "已存在，重新触发搜索", "profileId": prof_id}

    # 只监控正片季（跳过第 0 季特别篇）；seasonMode 决定监控范围
    # all=全部季 / latest=只最新季 / first=只第一季
    all_seasons = [s for s in (chosen.get("seasons") or [])
                   if (s.get("seasonNumber") or 0) > 0]
    if season_mode in ("latest", "first") and all_seasons:
        nums = [s.get("seasonNumber") for s in all_seasons]
        pick = max(nums) if season_mode == "latest" else min(nums)
        all_seasons = [s for s in all_seasons if s.get("seasonNumber") == pick]
    seasons = [{"seasonNumber": s.get("seasonNumber"), "monitored": True}
               for s in all_seasons]

    payload = {
        "tvdbId": tvdb, "title": title, "qualityProfileId": prof_id,
        "rootFolderPath": root, "monitored": True, "seasonFolder": True,
        "seasons": seasons,
        "addOptions": {"searchForMissingEpisodes": True,
                       "ignoreEpisodesWithFiles": False,
                       "ignoreEpisodesWithoutFiles": False},
    }
    try:
        created = s_req("POST", "/api/v3/series", payload)
    except Exception as e:
        return {"ok": False, "error": "Sonarr 添加剧集失败: %s" % e}

    sid = created.get("id") if isinstance(created, dict) else None
    try:
        if sid:
            s_req("POST", "/api/v3/command", {"name": "SeriesSearch", "seriesIds": [sid]})
    except Exception:
        pass
    return {"ok": True, "seriesId": sid, "tvdbId": tvdb, "title": title,
            "year": year, "action": "已添加并触发搜索", "profileId": prof_id,
            "seasonsMonitored": len(seasons), "seasonMode": season_mode}


def list_series():
    try:
        ss = s_req("GET", "/api/v3/series?pageSize=1000") or []
    except Exception:
        return []
    downloading_ids = set()
    try:
        q = s_req("GET", "/api/v3/queue?pageSize=200") or {}
        for it in q.get("records", []):
            sid = it.get("seriesId")
            if sid:
                downloading_ids.add(sid)
    except Exception:
        pass
    out = []
    for s in ss:
        st = s.get("statistics") or {}
        epf = st.get("episodeFileCount", s.get("episodeFileCount") or 0)
        eps = st.get("totalEpisodeCount", s.get("totalEpisodeCount") or 0)
        has = eps > 0 and epf >= eps
        out.append({
            "id": s.get("id"), "title": s.get("title"), "year": s.get("year"),
            "monitored": s.get("monitored"), "downloaded": has,
            "downloading": (s.get("id") in downloading_ids) and (not has),
            "episodeFileCount": epf, "totalEpisodeCount": eps,
            "poster": _poster_of(s), "network": s.get("network"),
            "status": s.get("status"), "sizeOnDisk": s.get("sizeOnDisk"),
        })
    return out


def remove_series(sid, delete_files=True):
    params = urlencode({"deleteFiles": str(delete_files).lower()})
    s_req("DELETE", "/api/v3/series/%d?%s" % (int(sid), params))
    return {"ok": True}


def remove_from_series_queue(qid, remove_from_client=True):
    params = urlencode({"removeFromClient": str(remove_from_client).lower(),
                        "blacklist": "false"})
    s_req("DELETE", "/api/v3/queue/%d?%s" % (int(qid), params))
    return {"ok": True}


# ---------- 抓取历史（Radarr / Sonarr history） ----------
def _history_records(rq, kind_label, limit=20):
    """取某服务的抓取历史记录；返回统一字段，便于前后端合并渲染。"""
    try:
        h = rq("GET", "/api/v3/history?pageSize=%d&sortDirection=descending&sortKey=date"
               % limit) or {}
    except Exception:
        return []
    out = []
    for it in (h or {}).get("records", []):
        data = it.get("data") or {}
        q = (it.get("quality") or {}).get("quality", {}) or {}
        out.append({
            "title": it.get("sourceTitle"),
            "eventType": it.get("eventType", ""),
            "date": it.get("date"),
            "indexer": data.get("indexer"),
            "quality": q.get("name"),
            "size": data.get("size"),
            "client": data.get("downloadClientName"),
            "kind": kind_label,
        })
    return out


def recent_history(kind="all", limit=20):
    """电影 / 剧集抓取历史合并，按时间倒序。kind=movie|tv|all。"""
    out = []
    if kind in ("movie", "all"):
        out += _history_records(r_req, "movie", limit)
    if kind in ("tv", "all"):
        out += _history_records(s_req, "tv", limit)
    out.sort(key=lambda x: x.get("date") or "", reverse=True)
    return out[:limit]


# ---------- 抓取完成 webhook 通知（F8） ----------
import json as _json
import urllib.request as _ureq

_WEBHOOK_SEEN = set()          # 已通知过的事件 id，避免重复发送
_WEBHOOK_LOCK = threading.Lock()
_WEBHOOK_SENT = 0             # 已发送通知数
_WEBHOOK_LAST = None          # 最近一次发送时间（UTC 串）
_WEBHOOK_TARGETS = {"downloadFolderImported", "movieFileImported", "episodeFileImported"}


def _complete_events():
    """取 Radarr+Sonarr 最近『导入完成』事件，归一化为统一结构。"""
    evs = []
    for rq, label in ((r_req, "movie"), (s_req, "tv")):
        try:
            h = rq("GET", "/api/v3/history?pageSize=30&sortDirection=descending&sortKey=date") or {}
        except Exception:
            continue
        for it in (h or {}).get("records", []):
            if it.get("eventType") not in _WEBHOOK_TARGETS:
                continue
            data = it.get("data") or {}
            q = (it.get("quality") or {}).get("quality", {}) or {}
            evs.append({
                "id": "%s:%s" % (label, it.get("id")),
                "title": it.get("sourceTitle"),
                "eventType": it.get("eventType"),
                "date": it.get("date"),
                "indexer": data.get("indexer"),
                "quality": q.get("name"),
                "size": data.get("size"),
                "client": data.get("downloadClientName"),
                "kind": label,
            })
    return evs


def _fire_webhook(payload):
    """POST JSON 到配置的 webhook；成功返回 True。"""
    global _WEBHOOK_SENT, _WEBHOOK_LAST
    if not WEBHOOK_URL:
        return False
    try:
        req = _ureq.Request(WEBHOOK_URL, data=_json.dumps(payload).encode("utf-8"),
                            headers={"Content-Type": "application/json"}, method="POST")
        with _ureq.urlopen(req, timeout=10) as resp:
            ok = resp.status < 400
    except Exception:
        ok = False
    if ok:
        with _WEBHOOK_LOCK:
            _WEBHOOK_SENT += 1
            _WEBHOOK_LAST = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return ok


def webhook_watcher(interval=30):
    """后台线程：轮询 history，对新增的『导入完成』事件打 webhook。
    启动先预热 seen 集合，不回填历史旧事件。"""
    try:
        for e in _complete_events():
            _WEBHOOK_SEEN.add(e["id"])
    except Exception:
        pass
    while True:
        time.sleep(interval)
        if not WEBHOOK_URL:
            continue
        try:
            evs = _complete_events()
        except Exception:
            continue
        fresh = [e for e in evs if e["id"] not in _WEBHOOK_SEEN]
        if not fresh:
            continue
        with _WEBHOOK_LOCK:
            for e in fresh:
                _WEBHOOK_SEEN.add(e["id"])
            if len(_WEBHOOK_SEEN) > 2000:
                _WEBHOOK_SEEN = set(list(_WEBHOOK_SEEN)[-1000:])
        for e in fresh:
            _fire_webhook({"event": "grab_complete", "title": e["title"],
                           "quality": e["quality"], "indexer": e["indexer"],
                           "size": e["size"], "client": e["client"],
                           "kind": e["kind"], "date": e["date"]})


# ---------- 日历视图（F9）：Radarr 电影上映 + Sonarr 剧集播出 ----------
def calendar_events(start, end):
    """合并 Radarr(电影上映) + Sonarr(剧集播出) 日历，按日期排序。
    start/end 形如 YYYY-MM-DD；Radarr 用 releaseDate，Sonarr 用 airDate。"""
    out = []
    try:
        ms = r_req("GET", "/api/v3/calendar?start=%s&end=%s&unmonitored=true" % (start, end)) or []
        for m in ms:
            rd = m.get("releaseDate") or m.get("digitalRelease") or m.get("physicalRelease")
            if not rd:
                continue
            out.append({"date": (rd or "")[:10], "title": m.get("title"),
                        "kind": "movie", "sub": str(m.get("year") or ""),
                        "tmdbId": m.get("tmdbId"), "id": m.get("id")})
    except Exception:
        pass
    try:
        eps = s_req("GET", "/api/v3/calendar?start=%s&end=%s" % (start, end)) or []
        for e in eps:
            ad = e.get("airDate")
            if not ad:
                continue
            ser = e.get("series") or {}
            out.append({"date": (ad or "")[:10],
                        "title": ser.get("title") or e.get("title"),
                        "kind": "tv",
                        "sub": "S%02dE%02d %s" % (e.get("seasonNumber", 0),
                                                 e.get("episodeNumber", 0),
                                                 e.get("title") or ""),
                        "seriesId": ser.get("id"), "id": e.get("id")})
    except Exception:
        pass
    out.sort(key=lambda x: (x.get("date") or "", x.get("title") or ""))
    return out


# ---------- 索引器只读健康（Prowlarr） ----------
def _parse_z(timestr):
    """把 '2026-08-31T15:22:43Z' 这种 UTC 串解析成可比较的 datetime。"""
    if not timestr:
        return None
    try:
        return datetime.strptime(timestr, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def indexer_health():
    """只读汇总 Prowlarr 索引器配置 + 运行状态，不写任何东西。

    /api/v1/indexer 给配置（enable/protocol/privacy/name），
    /api/v1/indexerstatus 只给「出过问题」的索引器（disabledTill / 失败时间）。
    健康索引器不会出现在 status 里，需两者合并后派生状态。
    """
    out = {"total": 0, "enabled": 0, "indexers": []}
    if not (PROWLARR_URL and get_prowlarr_key()):
        return out
    try:
        idx = p_req("GET", "/api/v1/indexer") or []
    except Exception:
        return out
    status_map = {}
    try:
        st = p_req("GET", "/api/v1/indexerstatus") or []
        for s in st:
            status_map[s.get("indexerId")] = s
    except Exception:
        pass
    now = datetime.now(timezone.utc)
    rows = []
    for i in idx:
        iid = i.get("id")
        enable = bool(i.get("enable"))
        st = status_map.get(iid) or {}
        disabled_till = st.get("disabledTill")
        recent_fail = st.get("mostRecentFailure")
        # 派生状态：配置关 / 当前被自动停用 / 曾失败但可用 / 正常
        if not enable:
            status = "disabled"
        elif _parse_z(disabled_till) and _parse_z(disabled_till) > now:
            status = "autoDisabled"
        elif recent_fail:
            status = "hadFailure"
        else:
            status = "healthy"
        rows.append({
            "name": i.get("name"),
            "enable": enable,
            "protocol": i.get("protocol"),
            "privacy": i.get("privacy"),
            "status": status,
            "lastFailure": recent_fail,
            "disabledTill": disabled_till,
        })
    out["total"] = len(rows)
    out["enabled"] = sum(1 for r in rows if r["enable"])
    # 不健康的排前面，便于一眼看见问题
    order = {"autoDisabled": 0, "hadFailure": 1, "disabled": 2, "healthy": 3}
    rows.sort(key=lambda r: (order.get(r["status"], 9), not r["enable"], r["name"] or ""))
    out["indexers"] = rows
    return out


def enable_indexer(name):
    """恢复一个被自动停用的索引器（Prowlarr enable:true）。只翻转 enable 标志，
    不改其抓取配置；返回 (ok, msg)。Prowlarr 若仍连不上会在下次失败时再自动停用，
    所以这是个安全的"重试恢复"动作，不破坏用户配置。"""
    if not (PROWLARR_URL and get_prowlarr_key()):
        return False, "Prowlarr 未配置"
    try:
        idx = p_req("GET", "/api/v1/indexer") or []
    except Exception as e:
        return False, "读取索引器列表失败: %s" % e
    target = None
    for i in idx:
        if (i.get("name") or "").lower() == (name or "").lower():
            target = i
            break
    if not target:
        return False, "找不到索引器「%s」" % name
    iid = target.get("id")
    try:
        full = p_req("GET", "/api/v1/indexer/%s" % iid) or {}
    except Exception as e:
        return False, "读取索引器配置失败: %s" % e
    full["enable"] = True
    try:
        p_req("PUT", "/api/v1/indexer/%s" % iid, full)
    except urllib.error.HTTPError as ex:
        # Prowlarr 保存时会做连通性校验；站点不可达（如被 Cloudflare 拦截）会返回 400，
        # body 里是可读的失败原因。原样转译给用户，而不是只报一个 400。
        try:
            body = json.loads(ex.read().decode("utf-8"))
            if isinstance(body, list):
                msgs = [x.get("errorMessage") for x in body if x.get("errorMessage")]
                if msgs:
                    return False, "启用被 Prowlarr 拒绝：" + "；".join(msgs)
            elif isinstance(body, dict) and body.get("message"):
                return False, "启用被 Prowlarr 拒绝：" + body["message"]
        except Exception:
            pass
        return False, "启用失败（HTTP %s）：%s" % (ex.code, ex.reason)
    except Exception as e:
        return False, "启用失败: %s" % e
    return True, "已重新启用「%s」" % name


# ---------- 各服务健康探测 ----------
def _uptime():
    s = int(time.time() - _START_TS)
    if s < 60:
        return "%d 秒" % s
    if s < 3600:
        return "%d 分钟" % (s // 60)
    if s < 86400:
        return "%d 小时 %d 分" % (s // 3600, (s % 3600) // 60)
    return "%d 天 %d 小时" % (s // 86400, (s % 86400) // 3600)


def _ver_of(rq, path):
    """返回 (ok, 版本串或错误信息)。注意必须用 /api/v3/system/status，
    裸 /system/status 返回的是前端 HTML 壳页，json 解析会失败。"""
    try:
        st = rq("GET", path, timeout=15) or {}
        v = st.get("version")
        return True, ("v" + str(v)) if v else "就绪"
    except Exception as e:
        return False, str(e)[:90]


def _qbit_status():
    """qBittorrent：v5 会话 Cookie 名为 QBT_SID_<port>，POST 需 CSRF。"""
    try:
        data = urlencode({"username": QBIT_USER, "password": QBIT_PASS}).encode()
        req = Request(QBIT_URL + "/api/v2/auth/login", data=data,
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urlopen(req, timeout=10, context=SSL_CTX) as r:
            ck = r.headers.get("Set-Cookie", "")
    except Exception as e:
        return False, "登录失败: %s" % str(e)[:60]
    m = re.search(r"(QBT_SID_\d+=[^;]+)", ck) or re.search(r"(SID=[^;]+)", ck)
    if not m:
        return False, "登录失败（未拿到会话）"
    cm = re.search(r"(csrftoken=[^;]+)", ck)
    hdrs = {"Cookie": m.group(1) + ("; " + cm.group(1) if cm else "")}
    if cm:
        hdrs["X-Csrftoken"] = cm.group(1).split("=", 1)[1]

    ver = ""
    try:
        with urlopen(Request(QBIT_URL + "/api/v2/app/version", headers=hdrs),
                     timeout=10, context=SSL_CTX) as r:
            ver = r.read().decode().strip()
    except Exception:
        pass
    dl = up = 0
    try:
        with urlopen(Request(QBIT_URL + "/api/v2/transfer/info", headers=hdrs),
                     timeout=10, context=SSL_CTX) as r:
            ti = json.loads(r.read().decode())
        dl, up = ti.get("dl_info_speed", 0) or 0, ti.get("up_info_speed", 0) or 0
    except Exception:
        pass
    n = 0
    try:
        with urlopen(Request(QBIT_URL + "/api/v2/torrents/info?filter=downloading",
                             headers=hdrs), timeout=10, context=SSL_CTX) as r:
            n = len(json.loads(r.read().decode()) or [])
    except Exception:
        pass

    def spd(b):
        return "%.1f MB/s" % (b / 1048576.0) if b else "0 MB/s"

    return True, "%s · ↓%s ↑%s · %d 个进行中" % (
        ("v" + ver.lstrip("v")) if ver else "就绪", spd(dl), spd(up), n)


def qbit_add_magnet(magnet, category=""):
    """把 magnet 直接提交给 qBittorrent 下载（复用 v5 会话 Cookie + CSRF 登录）。"""
    magnet = (magnet or "").strip()
    if not magnet.lower().startswith("magnet:"):
        return False, "无效的 magnet 链接"
    try:
        data = urlencode({"username": QBIT_USER, "password": QBIT_PASS}).encode()
        req = Request(QBIT_URL + "/api/v2/auth/login", data=data,
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urlopen(req, timeout=10, context=SSL_CTX) as r:
            ck = r.headers.get("Set-Cookie", "")
    except Exception as e:
        return False, "qBittorrent 登录失败: %s" % str(e)[:80]
    m = re.search(r"(QBT_SID_\d+=[^;]+)", ck) or re.search(r"(SID=[^;]+)", ck)
    if not m:
        return False, "未拿到 qBittorrent 会话"
    cm = re.search(r"(csrftoken=[^;]+)", ck)
    hdrs = {"Cookie": m.group(1) + ("; " + cm.group(1) if cm else "")}
    if cm:
        hdrs["X-Csrftoken"] = cm.group(1).split("=", 1)[1]
    add_hdrs = dict(hdrs)
    add_hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    # 先带分类提交（qB 会自动建分类）；失败则不带分类重试
    last = ""
    for cat in (category, ""):
        try:
            form = {"urls": magnet}
            if cat:
                form["category"] = cat
            body = urlencode(form).encode()
            req = Request(QBIT_URL + "/api/v2/torrents/add", data=body, headers=add_hdrs)
            with urlopen(req, timeout=15, context=SSL_CTX) as r:
                r.read()
            return True, "已提交 qBittorrent 下载" + (("（分类 %s）" % cat) if cat else "（默认目录）")
        except urllib.error.HTTPError as ex:
            # qBittorrent v5 对「已在列表中的种子」返回 409 Conflict；
            # 这对用户而言等于「已经在下载」，不应报成失败。
            if ex.code == 409:
                return True, "已在下载队列中（重复，未重复添加）"
            last = "HTTP %d" % ex.code
        except Exception as e:
            last = str(e)[:80]
    return False, "添加失败: %s" % last


def _flaresolverr_status():
    if not FLARESOLVERR_URL:
        return False, "未配置"
    try:
        with urlopen(Request(FLARESOLVERR_URL + "/", headers={"Accept": "*/*"}),
                     timeout=10) as r:
            body = r.read().decode("utf-8", "replace")
        return (True, "就绪（Cloudflare 挑战求解）") if "flaresolverr" in body.lower() \
            else (True, "响应异常")
    except Exception as e:
        return False, str(e)[:90]


def _proxy_status():
    if not PROXY_URL:
        return False, "未配置"
    try:
        u = urlparse(PROXY_URL)
        host, port = u.hostname, (u.port or 3128)
        s = socket.create_connection((host, port), timeout=5)
        s.close()
        return True, "%s:%d 可达" % (host, port)
    except Exception as e:
        return False, str(e)[:90]


# ---- 配置面板后端（自包含，不再依赖 bitmagnet-bot）----
# 单一「代理链接」即外网出口：autopilot 经它直连 TMDB 等；同时解析成 squid 上游
# (UPSTREAM_PROXY_*) 写入 .env，使 proxy-forwarder 也出网，从而 flaresolverr/qb/*arr 一并可用。
# TMDB_API_KEY 同样落盘并热更新全局变量，无需重建 autopilot 即可生效。
MEDIA_ENV = os.environ.get("MEDIA_ENV", "/app/.env")
_SQUID_ENV_CANDIDATES = [os.environ.get("SQUID_ENV", ""), "/opt/media/.env", MEDIA_ENV]


def _read_env_file(path):
    d = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
    except Exception:
        pass
    return d


def _write_env_file(path, updates):
    """就地更新 .env 指定键（保留注释与其余键）；不存在则创建。"""
    try:
        raw = open(path).read().splitlines()
    except Exception:
        raw = []
    keys = set(updates.keys())
    out, handled = [], set()
    for ln in raw:
        s = ln.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in keys:
                out.append("%s=%s" % (k, updates[k]))
                handled.add(k)
                continue
        out.append(ln)
    for k in updates:
        if k not in handled:
            out.append("%s=%s" % (k, updates[k]))
    try:
        with open(path, "w") as f:
            f.write(("\n".join(out) + "\n") if out else "")
        return True
    except Exception:
        return False


def _parse_proxy_url(u):
    """http[s]://[user:pass@]host:port -> {host, port, auth}；解析失败返回 None。"""
    if not u:
        return None
    try:
        from urllib.parse import urlparse
        p = urlparse(u)
        if not p.hostname:
            return None
        return {
            "host": p.hostname,
            "port": int(p.port or (443 if p.scheme == "https" else 80)),
            "auth": ("%s:%s" % (p.username, p.password)) if p.username else "",
        }
    except Exception:
        return None


def _restart_container(name):
    """尽力而为：经 docker.sock 重启出口代理，使 squid 上游生效；失败静默。"""
    try:
        import subprocess
        subprocess.run(["docker", "restart", name], timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def config_get():
    return {"ok": True, "values": {"PROXY_URL": PROXY_URL, "TMDB_KEY": TMDB_KEY}}


def config_save(body_str):
    try:
        data = json.loads(body_str or "{}")
    except Exception:
        return 400, {"ok": False, "error": "请求体解析失败"}
    proxy = (data.get("proxy_url") or "").strip()
    tmdb_key = (data.get("tmdb_key") or "").strip()
    updates = {}
    if proxy:
        updates["EGRESS_PROXY"] = proxy
        updates["PROXY_URL"] = proxy
        updates["TMDB_PROXY"] = proxy
        parsed = _parse_proxy_url(proxy)
        if parsed:
            updates["UPSTREAM_PROXY_HOST"] = parsed["host"]
            updates["UPSTREAM_PROXY_PORT"] = str(parsed["port"])
            updates["UPSTREAM_PROXY_AUTH"] = parsed["auth"]
    if tmdb_key:
        updates["TMDB_API_KEY"] = tmdb_key
    wrote = _write_env_file(MEDIA_ENV, updates)
    # squid 与 autopilot 共享宿主 .env：MEDIA_ENV 多半就是它；否则额外写候选路径
    for cand in _SQUID_ENV_CANDIDATES:
        if cand and cand != MEDIA_ENV and os.path.exists(cand):
            _write_env_file(cand, {k: v for k, v in updates.items()
                                   if k.startswith("UPSTREAM_PROXY_")})
            break
    global PROXY_URL, TMDB_PROXY, TMDB_KEY
    if proxy:
        PROXY_URL = proxy
        TMDB_PROXY = proxy
    if tmdb_key:
        TMDB_KEY = tmdb_key
    if proxy:
        _restart_container("proxy-forwarder")
    return 200, {"ok": True, "wrote": wrote,
                 "values": {"PROXY_URL": PROXY_URL, "TMDB_KEY": TMDB_KEY}}


# 系统状态缓存：探测一次约 10~30s（两次 pageSize=1000 大列表 + 三次外部探测），
# 加 15s TTL 后，30s 自动刷新与每次切到状态页都不会再打爆各 *arr API。
_SYS_CACHE = {"ts": 0.0, "data": None}
_SYS_CACHE_TTL = 15.0
_sys_cache_lock = threading.Lock()


# 发现墙/详情缓存：TMDB 经 Clash 出网慢且节点抖动，而榜单/详情变化不频繁。
# 落库到 sqlite 持久化（容器重建/重部署不丢），按完整查询 key 缓存，TTL=24h（一天一拉）。
# 纯 lazy-load：无后台预热线程；刷新按钮带 refresh=1 会先删 key 再强制重拉。
import sqlite3 as _sqlite3

_TMDB_CACHE_DIR = os.environ.get("AUTOPILOT_CACHE_DIR", "/app/cache") + ""
_TMDB_DB = os.path.join(_TMDB_CACHE_DIR, "discover.db")
_TMDB_CACHE_TTL = 86400.0   # 24 小时（一天一拉）
_tmdb_db_lock = threading.Lock()


def _tmdb_ckey(key):
    """缓存 key 可能是 tuple，直接当 SQL 参数会被 SQLite 拒绝（无法适配 tuple），
    故统一序列化为稳定字符串再入库。"""
    return str(key)


def _tmdb_cache_conn():
    os.makedirs(_TMDB_CACHE_DIR, exist_ok=True)
    conn = _sqlite3.connect(_TMDB_DB, timeout=30)
    conn.execute("CREATE TABLE IF NOT EXISTS tmdb_cache ("
                 "key TEXT PRIMARY KEY, data TEXT, fetched_at REAL)")
    return conn


def _tmdb_cache_get(key):
    k = _tmdb_ckey(key)
    try:
        with _tmdb_db_lock:
            conn = _tmdb_cache_conn()
            try:
                row = conn.execute(
                    "SELECT data, fetched_at FROM tmdb_cache WHERE key=?", (k,)).fetchone()
            finally:
                conn.close()
        if row is None:
            return None
        data, ts = row
        if (time.time() - float(ts)) >= _TMDB_CACHE_TTL:
            return None  # 已过期，视为未命中
        return json.loads(data)
    except Exception:
        return None


def _tmdb_cache_put(key, data):
    k = _tmdb_ckey(key)
    try:
        blob = json.dumps(data, ensure_ascii=False)
        with _tmdb_db_lock:
            conn = _tmdb_cache_conn()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO tmdb_cache (key, data, fetched_at) VALUES (?,?,?)",
                    (k, blob, time.time()))
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass  # 缓存写入失败不应影响主链路（榜单/详情照常返回 TMDB 实时数据）


def _tmdb_cache_invalidate(key):
    k = _tmdb_ckey(key)
    try:
        with _tmdb_db_lock:
            conn = _tmdb_cache_conn()
            try:
                conn.execute("DELETE FROM tmdb_cache WHERE key=?", (k,))
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass


def system_status():
    now = time.time()
    with _sys_cache_lock:
        if _SYS_CACHE["data"] is not None and (now - _SYS_CACHE["ts"]) < _SYS_CACHE_TTL:
            return _SYS_CACHE["data"]

    out = {"radarrVersion": None, "appName": "Radarr", "totalMovies": 0,
           "downloadedMovies": 0, "disks": [], "indexers": None, "services": [],
           "authEnabled": bool(TOKEN)}
    rad_status = {}
    try:
        rad_status = r_req("GET", "/api/v3/system/status", timeout=15) or {}
    except Exception:
        rad_status = {}
    rad_ver = rad_status.get("version")
    rad_ok = bool(rad_ver)
    out["radarrVersion"] = rad_ver
    out["appName"] = rad_status.get("appName", "Radarr")
    ms = []
    try:
        ms = r_req("GET", "/api/v3/movie?pageSize=1000") or []
        out["totalMovies"] = len(ms)
        out["downloadedMovies"] = sum(1 for m in ms if m.get("hasFile"))
    except Exception:
        pass
    try:
        disks = r_req("GET", "/api/v3/diskspace") or []
        _INTERNAL = {"/", "/config", "/tv"}  # 容器根文件系统/配置卷，非媒体存储
        seen = set()
        out["disks"] = []
        for d in disks:
            p = d.get("path")
            if p in _INTERNAL:
                continue
            # 同一块物理盘可能挂到 /downloads、/movies 等多个子目录，按容量去重只留一条
            key = (d.get("freeSpace"), d.get("totalSpace"))
            if key in seen:
                continue
            seen.add(key)
            out["disks"].append({"path": p, "free": d.get("freeSpace"),
                                 "total": d.get("totalSpace")})
        # 极端情况：过滤后为空则回退显示全部，避免面板空白
        if not out["disks"]:
            out["disks"] = [{"path": d.get("path"), "free": d.get("freeSpace"),
                             "total": d.get("totalSpace")} for d in disks]
    except Exception:
        pass
    if PROWLARR_URL and get_prowlarr_key():
        try:
            idx = p_req("GET", "/api/v1/indexer") or []
            out["indexers"] = {"total": len(idx),
                               "enabled": sum(1 for i in idx if i.get("enable"))}
        except Exception:
            out["indexers"] = {"total": 0, "enabled": 0}
    # Sonarr 统计（不可用保持 None，前端自动隐藏）
    out["sonarrVersion"] = None
    out["totalSeries"] = None
    out["downloadedSeries"] = None
    son_ok, son_ver = False, None
    try:
        sst = s_req("GET", "/api/v3/system/status", timeout=15) or {}
        son_ver = sst.get("version")
        son_ok = bool(son_ver)
        out["sonarrVersion"] = son_ver
    except Exception:
        pass
    try:
        ss = s_req("GET", "/api/v3/series?pageSize=1000") or []
        out["totalSeries"] = len(ss)
        out["downloadedSeries"] = sum(
            1 for x in ss
            if ((x.get("statistics") or {}).get("episodeFileCount", 0) > 0
                and (x.get("statistics") or {}).get("episodeFileCount", 0)
                >= (x.get("statistics") or {}).get("totalEpisodeCount", 1))
        )
    except Exception:
        pass

    # Prowlarr
    if PROWLARR_URL and get_prowlarr_key():
        pro_ok, pro_ver = _ver_of(p_req, "/api/v1/system/status")
    else:
        pro_ok, pro_ver = False, "未配置"

    idx = out.get("indexers") or {}
    # 三个外部服务彼此独立、且最慢（qB 每次重登录），并发拉取缩短总耗时
    with _cf.ThreadPoolExecutor(max_workers=3) as ex:
        f_q = ex.submit(_qbit_status)
        f_f = ex.submit(_flaresolverr_status)
        f_p = ex.submit(_proxy_status)
        qb = f_q.result()
        fla = f_f.result()
        prx = f_p.result()
    svcs = [
        {"key": "autopilot", "name": "autopilot", "ok": True,
         "detail": "已运行 " + _uptime(),
         "desc": "统一入口：搜索片名并自动选源下载，管理电影/剧集与下载队列"},
        {"key": "radarr", "name": "Radarr", "ok": rad_ok,
         "detail": ("v%s · 影片 %d · 已下载 %d"
                    % (rad_ver, out["totalMovies"], out["downloadedMovies"]))
                   if rad_ok else (rad_ver or "不可达"),
         "desc": "电影管理：监控想看的电影，自动匹配并下载高质量版本 · 账号 admin / MediaFn2026"},
        {"key": "sonarr", "name": "Sonarr", "ok": son_ok,
         "detail": ("v%s · 剧集 %s · 已下载 %s"
                    % (son_ver, out["totalSeries"], out["downloadedSeries"]))
                   if son_ok else "不可达",
         "desc": "剧集管理：追更电视剧，按季/集自动抓取与整理 · 账号 admin / MediaFn2026"},
        {"key": "prowlarr", "name": "Prowlarr", "ok": pro_ok,
         "detail": ("%s · 索引器 %s/%s 启用" % (pro_ver, idx.get("enabled", 0),
                                            idx.get("total", 0)))
                   if (pro_ok and idx) else (pro_ver or "不可达"),
         "desc": "索引器聚合：汇总各 BT/Usenet 站点资源，供 Radarr/Sonarr 统一检索 · 账号 admin / MediaFn2026"},
        {"key": "qbittorrent", "name": "qBittorrent", "ok": qb[0],
         "detail": qb[1],
         "desc": "下载客户端：实际执行 BT/PT 下载，做种并写入媒体库目录 · 账号 admin / MediaFn2026"},
        {"key": "flaresolverr", "name": "FlareSolverr", "ok": fla[0],
         "detail": fla[1],
         "desc": "反爬求解：破解 Cloudflare 等站点验证，让索引器能正常抓取"},
        {"key": "proxy", "name": "转发代理 (Squid)", "ok": prx[0],
         "detail": prx[1],
         "desc": "网络出口：为下载/抓取提供代理转发，绕过访问限制"},
    ]
    out["services"] = svcs
    out["webhook"] = {"configured": bool(WEBHOOK_URL), "url": WEBHOOK_URL,
                      "sent": _WEBHOOK_SENT, "last": _WEBHOOK_LAST}
    with _sys_cache_lock:
        _SYS_CACHE["data"] = out
        _SYS_CACHE["ts"] = time.time()
    return out




# ---------- HTTP ----------
PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>影视下载台</title>
<style>
  :root{color-scheme:dark}
  *{box-sizing:border-box}
  body{font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
       background:#0f1115;color:#e6e6e6;margin:0}
  header{background:#151923;border-bottom:1px solid #232a37;padding:14px 20px;
         display:flex;align-items:center;gap:12px;position:sticky;top:0;z-index:10}
  header h1{font-size:18px;margin:0}
  header .pill{margin-left:auto;font-size:12px;color:#8b93a1;display:flex;gap:14px;flex-wrap:wrap}
  header .pill b{color:#5fd38a;font-weight:600}
  .wrap{max-width:1000px;margin:0 auto;padding:18px}
  .tabs{display:flex;gap:6px;margin-bottom:16px;flex-wrap:wrap}
  .tab{padding:8px 14px;border-radius:8px;background:#171a21;border:1px solid #232a37;
       color:#aeb6c2;cursor:pointer;font-size:14px}
  .tab.active{background:#2f6fed;color:#fff;border-color:#2f6fed}
  .panel{display:none}
  .panel.active{display:block}
  input,select,textarea,button{font-size:14px;padding:9px 11px;border-radius:8px;
       border:1px solid #2a2f3a;background:#171a21;color:#e6e6e6;font-family:inherit}
  button{cursor:pointer;font-weight:600}
  .btn{background:#2f6fed;color:#fff;border:none}
  .btn:hover{background:#3f7ffa}
  .btn.ghost{background:#222834;border:1px solid #313a49;color:#cdd4df}
  .btn.ghost:hover{background:#2b3441}
  .btn.danger{background:#3a2330;border-color:#5a2b3c;color:#ff9aa9}
  .btn.danger:hover{background:#4a2a3a}
  .row{display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap}
  .row>*{flex:0 0 auto}
  #term{flex:1;min-width:200px}
  .chk{display:flex;align-items:center;gap:5px;color:#aeb6c2;font-size:13px;white-space:nowrap;user-select:none;padding:0 4px}
  .chk input{width:auto;padding:0;accent-color:#2f6fed}
  #bulk{flex:1;min-width:240px;min-height:90px;resize:vertical}
  #status{margin:10px 0;font-size:14px;line-height:1.6;min-height:20px}
  .ok{color:#5fd38a}.err{color:#ff6b6b}.muted{color:#8b93a1;font-size:12px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
  .card{background:#171a21;border:1px solid #232833;border-radius:10px;overflow:hidden;
        display:flex;flex-direction:column;position:relative}
  .card .poster{aspect-ratio:2/3;background:linear-gradient(135deg,#1d2430,#2a3340);
        display:flex;align-items:center;justify-content:center;color:#3c4759;font-size:28px;
        position:relative}
  .card .poster img{width:100%;height:100%;object-fit:cover}
  .card .meta{padding:8px 10px;font-size:13px}
  .card .meta .t{font-weight:600;line-height:1.3}
  .card .meta .s{color:#8b93a1;font-size:11px;margin-top:2px}
  .card .acts{padding:0 10px 10px;display:flex;gap:6px}
  .card .acts button{flex:1;padding:7px 4px;font-size:12px}
  .badge{position:absolute;top:6px;right:6px;color:#fff;font-size:10px;
         padding:2px 6px;border-radius:6px}
  .badge.ok{background:#1b8a4b}
  .badge.wait{background:#c98a2b}
  .badge.dl{background:#2f6fed}
  .badge.off{background:#4a5260}
  .tag{display:inline-block;padding:2px 8px;border-radius:6px;font-size:11px;
       background:#222834;border:1px solid #313a49;color:#aeb6c2;white-space:nowrap;margin:0 4px 4px 0}
  .tag.ok{background:#1b8a4b;color:#fff;border-color:#1b8a4b}
  .tag.off{background:#3a4250;color:#c5cdd8;border-color:#3a4250}
  .tag.warn{background:#c98a2b;color:#fff;border-color:#c98a2b}
  .filters{display:flex;gap:6px;flex-wrap:wrap}
  .fbtn{padding:6px 11px;border-radius:8px;background:#171a21;border:1px solid #232a37;color:#aeb6c2;cursor:pointer;font-size:13px}
  .fbtn.active{background:#2f6fed;color:#fff;border-color:#2f6fed}
  .mode{display:flex;gap:3px;background:#171a21;border:1px solid #232a37;border-radius:9px;padding:3px}
  .mbtn{padding:5px 13px;border-radius:6px;background:transparent;border:none;color:#aeb6c2;
        cursor:pointer;font-size:13px;font-weight:600;font-family:inherit}
  .mbtn.active{background:#2f6fed;color:#fff}
  .sdot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px;vertical-align:middle}
  .sdot.ok{background:#5fd38a}
  .sdot.err{background:#ff6b6b}
  .slink{color:#6ea8fe;text-decoration:none;margin-left:8px}
  .slink:hover{text-decoration:underline}
  .svc-desc{color:#8b93a1;font-size:12px;margin-top:6px;line-height:1.5}
  .bres{background:#171a21;border:1px solid #232833;border-radius:8px;padding:8px 11px;
        margin-top:6px;font-size:13px;display:flex;gap:8px;align-items:flex-start}
  .bres .nm{flex:1;min-width:0;word-break:break-word}
  .bres .rs{font-size:12px;color:#8b93a1;white-space:nowrap}
  .bres .rs.ok{color:#5fd38a}
  .bres .rs.err{color:#ff6b6b}
  .qrow{display:flex;gap:6px;align-items:center;font-size:12px;color:#8b93a1;margin:6px 0 12px}
  .queue-item{background:#171a21;border:1px solid #232833;border-radius:10px;padding:12px 14px;margin-bottom:10px}
  .queue-item .top{display:flex;justify-content:space-between;gap:10px;align-items:center}
  .queue-item .top b{font-size:14px}
  .queue-item .sub{color:#8b93a1;font-size:12px;margin-top:4px;display:flex;gap:14px;flex-wrap:wrap}
  .bar{height:7px;background:#2a2f3a;border-radius:4px;overflow:hidden;margin-top:8px}
  .bar>i{display:block;height:100%;background:linear-gradient(90deg,#2f6fed,#5fd38a);transition:width .5s}
  .cal-head{display:flex;align-items:center;gap:12px;margin-bottom:10px}
  .cal-title{font-size:16px;font-weight:600}
  .cal-nav{margin-left:auto;display:flex;gap:6px}
  .cal-week{display:grid;grid-template-columns:repeat(7,1fr);gap:6px;margin-bottom:4px}
  .cal-week span{text-align:center;font-size:12px;color:#8b93a1}
  .cal-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:6px}
  .cal-cell{background:#171a21;border:1px solid #232833;border-radius:8px;min-height:74px;padding:5px}
  .cal-cell.cal-empty{background:transparent;border:none}
  .cal-cell.cal-today{border-color:#2f6fed}
  .cal-d{font-size:12px;color:#8b93a1;margin-bottom:3px}
  .cal-chip{font-size:11px;padding:2px 5px;border-radius:5px;margin-bottom:3px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .cal-chip.mv{background:#1b2a44;color:#9cc2ff}
  .cal-chip.tv{background:#2a2140;color:#cbb0ff}
  .cal-more{font-size:11px;color:#8b93a1}
  .toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#222834;
         border:1px solid #313a49;padding:10px 18px;border-radius:10px;font-size:14px;
         box-shadow:0 8px 30px rgba(0,0,0,.4);opacity:0;transition:opacity .3s;pointer-events:none}
  .toast.show{opacity:1}
  .modal{position:fixed;inset:0;background:rgba(0,0,0,.62);display:flex;align-items:center;
         justify-content:center;z-index:60;padding:20px}
  .modal-box{background:#171a21;border:1px solid #2a2f3a;border-radius:14px;max-width:700px;width:100%;
             max-height:88vh;overflow:auto;padding:20px;position:relative}
  .modal-x{position:absolute;top:8px;right:12px;background:none;border:none;color:#8b93a1;
           font-size:24px;line-height:1;cursor:pointer;z-index:1}
  .modal-x:hover{color:#e6e6e6}
  .detail-backdrop{width:100%;height:150px;object-fit:cover;border-radius:10px;margin-bottom:14px}
  .detail-tag{display:inline-block;background:#222834;border:1px solid #313a49;color:#aeb6c2;
              font-size:12px;padding:2px 8px;border-radius:6px;margin:0 6px 6px 0}
  .detail-head{padding:0 2px}
  .detail-title{font-size:22px;font-weight:700;color:#f4f6fb;margin-bottom:6px;line-height:1.25}
  .detail-tagline{color:#8b93a3;font-style:italic;margin:10px 2px 0}
  .detail-tags{margin:10px 0 0}
  .detail-overview{margin-top:14px;padding-top:14px;border-top:1px solid #222a37;
    line-height:1.7;color:#c6cdd9;font-size:14px;white-space:pre-wrap}
  .detail-acts{margin-top:18px;display:flex;gap:10px;justify-content:flex-end}
  .detail-acts .btn{width:auto;padding:9px 22px}
  .chip-row{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
  .chip{padding:6px 12px;border-radius:8px;background:#171a21;border:1px solid #232a37;color:#aeb6c2;cursor:pointer;font-size:13px;user-select:none;line-height:1.4;white-space:nowrap}
  .chip:hover{background:#1d212a;border-color:#2f3a4d}
  .chip.on{background:#2f6fed;color:#fff;border-color:#2f6fed}
  .chip.on.multi{background:#16313f;border-color:#2f6fed;color:#9bdcff}
  .chip-group{display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap}
  .cg-label{font-size:12px;color:#8b93a1;min-width:30px;flex:0 0 auto}
  .disc-filters{margin:10px 0 2px}
  .active-row{margin:2px 0 10px;padding-top:8px;border-top:1px solid #222a37;display:flex;gap:6px;flex-wrap:wrap;align-items:center}
  .xchip{background:#1b2230;border-color:#2f3a4d;color:#c5cdd8}
  .xchip b{color:#fff;font-weight:600}
  .xchip .x{margin-left:6px;color:#ff8a8a;font-weight:700}
  .xchip.clear{background:transparent;border-color:#3a4250;color:#8b93a1}
  .xchip.clear:hover{background:#222834}
  .disc-dd-row{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0;align-items:center}
  .filter-dd{position:relative}
  .filter-dd>summary{list-style:none;cursor:pointer;padding:5px 10px;border-radius:6px;background:#171a21;border:1px solid #232a37;color:#aeb6c2;font-size:12px;user-select:none;line-height:1.4;white-space:nowrap}
  .filter-dd>summary::-webkit-details-marker{display:none}
  .filter-dd[open]>summary{background:#1d2230;border-color:#2f6fed;color:#fff}
  .dd-body{position:absolute;top:calc(100% + 4px);left:0;z-index:50;background:#0f1318;border:1px solid #2a3140;border-radius:6px;padding:8px;display:grid;grid-template-columns:repeat(3,minmax(80px,1fr));gap:4px 12px;max-height:280px;overflow-y:auto;box-shadow:0 6px 20px rgba(0,0,0,.5);min-width:260px}
  .dd-body label{display:flex;align-items:center;gap:4px;font-size:12px;color:#aeb6c2;cursor:pointer;white-space:nowrap;padding:2px 4px;border-radius:4px}
  .dd-body label:hover{background:#1a2030}
  .dd-body label.on{color:#9bdcff;background:#16313f}
  .dd-body label input{accent-color:#2f6fed;cursor:pointer}
  .filter-sel{padding:5px 8px;border-radius:6px;background:#171a21;border:1px solid #232a37;color:#aeb6c2;font-size:12px;cursor:pointer;line-height:1.4}
  .filter-sel:focus{outline:1px solid #2f6fed;border-color:#2f6fed}
</style>
</head>
<body>
<header>
  <h1>🎬 影视下载台</h1>
  <div class="pill" id="syspill">
    <span id="pillver">—</span>
    <span>影片 <b id="pillmv">—</b></span>
    <span>已下 <b id="pilldl">—</b></span>
    <span>索引器 <b id="pillidx">—</b></span>
    <span id="pilldisk">—</span>
  </div>
</header>
<div class="wrap">
  <div class="tabs">
    <div class="tab active" data-p="search">🔍 搜索下载</div>
    <div class="tab" data-p="discover">🎯 发现</div>
    <div class="tab" data-p="queue">⬇️ 下载队列</div>
    <div class="tab" data-p="library">🎞️ 媒体库</div>
    <div class="tab" data-p="history">📜 抓取历史</div>
    <div class="tab" data-p="indexers">🛰️ 索引器</div>
    <div class="tab" data-p="calendar">📅 日历</div>
    <div class="tab" data-p="status">📊 系统状态</div>
    <div class="tab" data-p="config">🛠 配置</div>
  </div>

  <!-- 搜索下载（电影/剧集合并搜索：搜出什么是什么） -->
  <div class="panel active" id="p-search">
    <div class="row">
      <input id="term" placeholder="输入片名或剧名，或粘贴 TMDB/IMDB 链接直接添加">
      <select id="profile"><option value="">默认画质</option></select>
      <select id="rootfolder"><option value="">默认目录</option></select>
      <select id="seasonMode" style="display:none" title="监控哪些季">
        <option value="all">全部季</option>
        <option value="latest">只追最新季</option>
        <option value="first">只追第一季</option>
      </select>
      <label class="chk"><input type="checkbox" id="dryRun"> 仅预览</label>
      <button class="btn" onclick="doSearch()">搜索</button>
    </div>
    <div id="status"></div>
    <div class="grid" id="cands"></div>
  </div>

  <!-- 发现墙（TMDB 热门影视，可按 电影/剧集 + 多种条件筛选） -->
  <div class="panel" id="p-discover">
    <div id="discCats" class="chip-row" style="margin-bottom:10px"></div>
    <div id="discTypes" class="chip-row" style="margin-bottom:12px"></div>
    <div class="disc-dd-row">
      <details class="filter-dd" id="discGenreDD">
        <summary id="discGenreSummary">类型 ▼</summary>
        <div class="dd-body" id="discGenreBody"></div>
      </details>
      <select id="discYearSel" class="filter-sel"></select>
      <select id="discCountrySel" class="filter-sel"></select>
      <select id="discRatingSel" class="filter-sel"></select>
      <select id="discRuntimeSel" class="filter-sel" style="display:none"></select>
      <select id="discSortSel" class="filter-sel"></select>
    </div>
    <div id="discActive" class="active-row"></div>
    <div class="row" style="align-items:center">
      <span id="discStatus" class="muted"></span>
      <span style="flex:1"></span>
      <button class="btn ghost" onclick="discPage=1;loadDiscover(false,true)">刷新</button>
    </div>
    <div class="grid" id="discGrid"></div>
    <div class="more" id="discMore"></div>
  </div>

  <!-- 详情弹层 -->
  <div id="discDetail" class="modal" style="display:none" onclick="if(event.target===this)closeDetail()">
    <div class="modal-box">
      <button class="modal-x" onclick="closeDetail()">×</button>
      <div id="detailBody"></div>
    </div>
  </div>

  <!-- 下载队列 -->
  <div class="panel" id="p-queue">
    <div class="row"><button class="btn ghost" onclick="loadQueue()">刷新</button>
      <div class="filters" id="qFilters">
        <button class="fbtn active" data-q="all">全部</button>
        <button class="fbtn" data-q="movie">🎬 电影</button>
        <button class="fbtn" data-q="tv">📺 剧集</button>
      </div>
      <span class="muted" id="qhint">每 5 秒自动刷新</span>
      <span style="flex:1"></span>
      <svg id="spdSpark" width="150" height="26" viewBox="0 0 150 26" preserveAspectRatio="none" style="vertical-align:middle;opacity:.95"></svg>
      <span class="muted" id="spdNow" style="min-width:80px;text-align:right"></span></div>
    <div id="qwatch" class="muted" style="margin:4px 0 10px"></div>
    <div id="queue"></div>
  </div>

  <!-- 媒体库 -->
  <div class="panel" id="p-library">
    <div class="filters" id="libMode" style="margin-bottom:12px">
      <button class="mbtn active" data-m="movie">🎬 电影</button>
      <button class="mbtn" data-m="tv">📺 剧集</button>
    </div>
    <div class="row">
      <button class="btn ghost" onclick="loadLibrary()">刷新</button>
      <button class="btn ghost" id="bulkRescan" onclick="bulkRescan()">批量重搜</button>
      <div class="filters" id="libFilters">
        <button class="fbtn active" data-f="all">全部</button>
        <button class="fbtn" data-f="downloaded">已下载</button>
        <button class="fbtn" data-f="downloading">下载中</button>
        <button class="fbtn" data-f="waiting">待源</button>
        <button class="fbtn" data-f="off">未监控</button>
      </div>
    </div>
    <div class="grid" id="lib"></div>
    <div id="libMore"></div>
  </div>

  <!-- 系统状态 -->
  <div class="panel" id="p-status">
    <div id="sysbody"></div>
  </div>

  <!-- 抓取历史 -->
  <div class="panel" id="p-history">
    <div class="row"><button class="btn ghost" onclick="loadHistory()">刷新</button>
      <span class="muted">最近抓取 / 入库 / 失败记录（电影与剧集合并，按时间倒序）</span></div>
    <div id="history"></div>
  </div>

  <!-- 索引器只读健康 -->
  <div class="panel" id="p-indexers">
    <div class="row"><button class="btn ghost" onclick="loadIndexers()">刷新</button>
      <span class="muted">只读：资源库（索引器）健康状态，无需登录 Prowlarr</span></div>
    <div id="indexers"></div>
  </div>

  <!-- 日历 -->
  <div class="panel" id="p-calendar">
    <div class="cal-head">
      <span class="cal-title" id="calTitle"></span>
      <span class="cal-nav">
        <button class="btn ghost" onclick="prevMonth()">‹</button>
        <button class="btn ghost" onclick="loadCalendar()">今天</button>
        <button class="btn ghost" onclick="nextMonth()">›</button>
      </span>
    </div>
    <div class="cal-week"><span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span><span>日</span></div>
    <div class="cal-grid" id="calGrid"></div>
    <div class="muted" style="margin-top:8px">点击条目即用当前模式搜索并添加；🎬 电影上映 / 📺 剧集播出（数据来自 Radarr/Sonarr 日历）。</div>
  </div>

  <!-- 配置（单一代理出口 + TMDB） -->
  <div class="panel" id="p-config">
    <div class="muted" style="margin-bottom:10px">外网出口与 TMDB 配置。保存后即时生效（自动写入 <code>.env</code> 并重启出口代理）。</div>
    <label class="cfg-row" style="display:block;margin:8px 0"><span>代理链接 Proxy URL</span>
      <input id="ap_PROXY_URL" style="width:100%;margin-top:4px;padding:8px" placeholder="http://user:pass@host:port（留空=仅内网）"></label>
    <label class="cfg-row" style="display:block;margin:8px 0"><span>TMDB API Key</span>
      <input id="ap_TMDB_KEY" type="password" style="width:100%;margin-top:4px;padding:8px" placeholder="在 themoviedb.org 申请的 v3 API Key"></label>
    <div class="row" style="margin-top:12px">
      <button class="btn" id="apCfgSave" onclick="apSaveConfig()">保存</button>
      <button class="btn ghost" onclick="apLoadConfig()">重新加载</button>
      <span class="muted" id="apCfgStatus"></span>
    </div>
  </div>

  <!-- 索引器 -->
</div>
<div class="toast" id="toast"></div>

<script>
const TOKEN=new URLSearchParams(location.search).get("token")||"";
function authHdr(){return TOKEN?{Authorization:"Bearer "+TOKEN}:{}}
function jget(u){return fetch(u,{headers:authHdr()}).then(r=>r.json())}
function jpost(u,b){return fetch(u,{method:"POST",headers:Object.assign({"Content-Type":"application/json"},authHdr()),body:JSON.stringify(b)}).then(r=>r.json())}
function toast(t,cls){const e=document.getElementById("toast");e.textContent=t;e.className="toast show"+(cls?" "+cls:"");setTimeout(()=>e.className="toast",2200)}
function setStatus(t,cls){const s=document.getElementById("status");s.className=cls||"";s.textContent=t||""}
function posterFail(img){try{var d=document.createElement("div");d.className="poster-fallback";d.textContent="🎞";img.parentNode.replaceChild(d,img);}catch(e){}}
function posterHTML(p,title){if(p)return '<div class="poster"><img src="'+p+'" loading="lazy" onerror="posterFail(this)"></div>';return '<div class="poster poster-fallback">🎞</div>';}
// 统一卡片构造器：搜索下载 / 发现 / 媒体库 / 剧集库 全部复用，保证视觉一致
// o: {poster, title, kind("movie"|"tv"|null), sub(已转义HTML串), extra(额外行HTML), badges([HTML]), acts(按钮HTML)}
function kindBadge(kind){return kind==="tv"?"📺 ":((kind==="movie")?"🎬 ":"");}
function buildCardInner(o){
  let h=posterHTML(o.poster,o.title);
  if(o.badges&&o.badges.length)h+=o.badges.join("");
  h+='<div class="meta"><div class="t">'+kindBadge(o.kind)+esc(o.title||"")+'</div>';
  if(o.sub)h+='<div class="s">'+o.sub+'</div>';
  if(o.extra)h+=o.extra;
  h+='</div><div class="acts">'+(o.acts||"")+'</div>';
  return h;
}
function buildCard(o){return '<div class="card">'+buildCardInner(o)+'</div>';}
function fmtSize(b){if(!b)return"";const g=b/1073741824;if(g>=1)return g.toFixed(2)+" GB";return (b/1048576).toFixed(0)+" MB";}
function fmtSpeed(b){if(!b)return"";return (b/1048576).toFixed(1)+" MB/s";}

// 模式：movie（Radarr 电影） / tv（Sonarr 剧集）
let MODE="movie";
const PH={movie:"输入片名或剧名，或粘贴 TMDB/IMDB 链接直接添加",tv:"输入片名或剧名，或粘贴 TMDB/IMDB 链接直接添加"};
function setMode(m){
  if(m===MODE)return;
  MODE=m;
  document.querySelectorAll(".mbtn").forEach(x=>x.classList.toggle("active",x.dataset.m===m));
  document.getElementById("term").placeholder=PH[m];
  const libTab=document.querySelector('.tab[data-p="library"]');
  if(libTab)libTab.textContent=(m==="tv"?"📺 剧集库":"🎞️ 媒体库");
  document.getElementById("seasonMode").style.display=(m==="tv"?"":"none");
  document.getElementById("cands").innerHTML="";
  setStatus("");
  loadProfiles();loadRootFolders();loadQueue();
  if(document.querySelector(".tab.active").dataset.p==="library"){
    if(m==="tv")loadSeriesLibrary();else loadLibrary();
  }
}

// tabs
function switchTo(p){
  document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));
  document.querySelectorAll(".panel").forEach(x=>x.classList.remove("active"));
  document.querySelectorAll(".tab").forEach(x=>{if(x.dataset.p===p)x.classList.add("active");});
  const panel=document.getElementById("p-"+p);
  if(panel)panel.classList.add("active");
  if(p==="queue")loadQueue();
  if(p==="library"){ if(MODE==="tv")loadSeriesLibrary(); else loadLibrary(); }
  if(p==="status"){loadSystem();}
  if(p==="history")loadHistory();
  if(p==="indexers")loadIndexers();
  if(p==="calendar")loadCalendar();
  if(p==="discover"){ renderDiscChips(); loadDiscover(); }
  if(p==="config")apLoadConfig();
}
document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>{
  switchTo(t.dataset.p);
});

function escAttr(s){ return (s||"").replace(/&/g,"&amp;").replace(/"/g,"&quot;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }

// 发现墙：TMDB 海报墙（电影/剧集合并展示；顶部「全部/电影/剧集」下拉筛选）
// 类型筛选（合并模式：电影/剧集类型 ID 合并；标注·剧 的为剧集专属分类）
// 发现页筛选器（chip 化）：类型多选、年代/国别/评分/时长/排序单选。数据来自 TMDB。
const GENRE_MOVIE=[["28","动作"],["12","冒险"],["16","动画"],["35","喜剧"],["80","犯罪"],["99","纪录"],["18","剧情"],["10751","家庭"],["14","奇幻"],["36","历史"],["27","恐怖"],["10402","音乐"],["9648","悬疑"],["10749","爱情"],["878","科幻"],["53","惊悚"],["10752","战争"],["37","西部"]];
const GENRE_TV=[["10759","动作冒险"],["10762","儿童"],["10763","新闻"],["10764","真人秀"],["10765","科幻奇幻"],["10766","肥皂剧"],["10767","脱口秀"],["10768","战争政治"]];
const GENRE_TOP=[["28","动作"],["35","喜剧"],["18","剧情"],["878","科幻"],["9648","悬疑"],["27","恐怖"],["10749","爱情"],["16","动画"]];
const COUNTRIES=[["US","美国"],["CN","中国大陆"],["HK","中国香港"],["TW","中国台湾"],["JP","日本"],["KR","韩国"],["GB","英国"],["FR","法国"],["DE","德国"],["IT","意大利"],["ES","西班牙"],["IN","印度"],["TH","泰国"],["RU","俄罗斯"],["CA","加拿大"],["AU","澳大利亚"],["BR","巴西"],["MX","墨西哥"],["KP","朝鲜"],["VN","越南"],["PH","菲律宾"]];
const COUNTRIES_TOP=[["US","美国"],["JP","日本"],["KR","韩国"],["CN","中国大陆"],["HK","中国香港"],["GB","英国"],["FR","法国"],["IN","印度"]];
const YEARS=[["","全部"],["2025","2025"],["2024","2024"],["2023","2023"],["2020,2029","2020s"],["2010,2019","2010s"],["2000,2009","2000s"],["1990,1999","90年代"],["1900,1989","更早"]];
const RATINGS=[["","全部"],["6","6分以上"],["7","7分以上"],["8","8分以上"],["9","9分以上"]];
const RUNTIMES=[["","全部"],["0,90","90分钟内"],["90,120","90-120分钟"],["120,9999","120分钟以上"]];
const SORTS=[["pop","按热度"],["rating","按评分"],["date","按上映日期"]];
const CATS=[["popular","🔥 热门"],["top_rated","⭐ 高分"],["now_playing","🎬 热映"],["upcoming","📅 即将上映"],["on_the_air","📺 在播"]];

let discKind="all", discCat="popular", discPage=1, discTotal=1;
let discGenres=[], discCountry="", discYear="", discRating="", discRuntime="", discSort="pop";

function _genreList(){ return discKind==="tv"?GENRE_TV:GENRE_MOVIE; }
function _catVisible(c){ if(c==="on_the_air"&&discKind==="movie")return false; if(c==="upcoming"&&discKind==="tv")return false; return true; }
function _label(list,v){ const f=list.find(x=>x[0]===v); return f?f[1]:v; }

function renderDiscChips(){
  const cats=document.getElementById("discCats");
  cats.innerHTML=CATS.filter(c=>_catVisible(c[0])).map(c=>'<span class="chip'+(c[0]===discCat?" on":"")+'" data-cat="'+c[0]+'">'+c[1]+'</span>').join("");
  cats.querySelectorAll("[data-cat]").forEach(el=>el.onclick=()=>{discCat=el.getAttribute("data-cat");discPage=1;renderDiscChips();loadDiscover();});
  const types=document.getElementById("discTypes");
  types.innerHTML=[["all","全部"],["movie","🎬 电影"],["tv","📺 剧集"]].map(t=>'<span class="chip'+(t[0]===discKind?" on":"")+'" data-kind="'+t[0]+'">'+t[1]+'</span>').join("");
  types.querySelectorAll("[data-kind]").forEach(el=>el.onclick=()=>{discKind=el.getAttribute("data-kind");discGenres=[];discPage=1;renderDiscChips();renderDiscDropdowns();renderActiveChips();loadDiscover();});
  renderDiscDropdowns(); renderActiveChips();
}
function renderDiscDropdowns(){
  // 类型（多选，details + checkboxes）
  const gb=document.getElementById("discGenreBody");
  if(gb){
    const list=_genreList();
    gb.innerHTML=list.map(g=>{
      const on=discGenres.includes(g[0]);
      return '<label'+(on?' class="on"':'')+'><input type="checkbox" value="'+g[0]+'"'+(on?' checked':'')+'>'+g[1]+'</label>';
    }).join("");
    gb.querySelectorAll('input[type="checkbox"]').forEach(cb=>{
      cb.onchange=()=>{
        const v=cb.value;
        discGenres=cb.checked?discGenres.concat(v):discGenres.filter(x=>x!==v);
        cb.parentElement.classList.toggle("on",cb.checked);
        updateGenreSummary();
        discPage=1; renderActiveChips(); loadDiscover();
      };
    });
    updateGenreSummary();
  }
  // 5 个单选下拉：年代/国别/评分/时长/排序
  _fillSel("discYearSel", YEARS, discYear, "", true);
  _fillSel("discCountrySel", COUNTRIES, discCountry, "国别 ", true);
  _fillSel("discRatingSel", RATINGS, discRating, "评分 ", true);
  _fillSel("discRuntimeSel", RUNTIMES, discRuntime, "时长 ", true);
  _fillSel("discSortSel", SORTS, discSort, "排序 ", false);
  const rt=document.getElementById("discRuntimeSel");
  if(rt) rt.style.display=(discKind==="tv")?"none":"";
}
function _fillSel(id, list, currentVal, prefix, allowAll){
  const el=document.getElementById(id);
  if(!el)return;
  let opts=[];
  if(allowAll && list.length>0 && list[0][0]!==""){
    opts.push('<option value="">'+escAttr(prefix+"全部")+'</option>');
  }
  opts=opts.concat(list.map(x=>{
    const isEmpty=(x[0]===""||x[0]==null);
    const txt=isEmpty?x[1]:(prefix?prefix+x[1]:x[1]);
    return '<option value="'+escAttr(x[0])+'">'+escAttr(txt)+'</option>';
  }));
  el.innerHTML=opts.join("");
  el.value=currentVal||"";
}
function updateGenreSummary(){
  const sum=document.getElementById("discGenreSummary");
  if(!sum)return;
  const n=discGenres.length;
  sum.textContent=n?("类型 ("+n+") ▼"):("类型 ▼");
}
function bindDiscDropdowns(){
  const ys=document.getElementById("discYearSel"); if(ys) ys.onchange=e=>{discYear=e.target.value;discPage=1;renderActiveChips();loadDiscover();};
  const cs=document.getElementById("discCountrySel"); if(cs) cs.onchange=e=>{discCountry=e.target.value;discPage=1;renderActiveChips();loadDiscover();};
  const rs=document.getElementById("discRatingSel"); if(rs) rs.onchange=e=>{discRating=e.target.value;discPage=1;renderActiveChips();loadDiscover();};
  const ts=document.getElementById("discRuntimeSel"); if(ts) ts.onchange=e=>{discRuntime=e.target.value;discPage=1;renderActiveChips();loadDiscover();};
  const ss=document.getElementById("discSortSel"); if(ss) ss.onchange=e=>{discSort=e.target.value;discPage=1;renderActiveChips();loadDiscover();};
  // 点击外部关闭类型 dropdown
  document.addEventListener("click",e=>{
    const dd=document.getElementById("discGenreDD");
    if(!dd||!dd.hasAttribute("open"))return;
    if(dd.contains(e.target))return;
    dd.removeAttribute("open");
  });
}
bindDiscDropdowns();
function renderActiveChips(){
  const box=document.getElementById("discActive");
  const parts=[];
  if(discGenres.length) parts.push(["类型",discGenres.map(g=>_label(_genreList(),g)).join("/"),"g",discGenres.join(",")]);
  if(discCountry) parts.push(["国别",_label(COUNTRIES,discCountry),"c",discCountry]);
  if(discYear) parts.push(["年代",_label(YEARS,discYear),"y",discYear]);
  if(discRating) parts.push(["评分",_label(RATINGS,discRating),"r",discRating]);
  if(discRuntime) parts.push(["时长",_label(RUNTIMES,discRuntime),"t",discRuntime]);
  if(discSort&&discSort!=="pop") parts.push(["排序",_label(SORTS,discSort),"s",discSort]);
  if(!parts.length){box.innerHTML="";return;}
  box.innerHTML=parts.map(p=>'<span class="chip xchip" data-k="'+p[2]+'" data-v="'+p[3]+'"><b>'+p[0]+'：'+p[1]+'</b> <span class="x">✕</span></span>').join("")+'<span class="chip xchip clear" id="clearAll">✕ 清空全部</span>';
  box.querySelectorAll(".xchip[data-k]").forEach(el=>el.onclick=()=>{const k=el.getAttribute("data-k");if(k==="g")discGenres=[];else if(k==="c")discCountry="";else if(k==="y")discYear="";else if(k==="r")discRating="";else if(k==="t")discRuntime="";else if(k==="s")discSort="pop";discPage=1;renderDiscChips();renderActiveChips();loadDiscover();});
  const clr=document.getElementById("clearAll"); if(clr)clr.onclick=()=>{discGenres=[];discCountry="";discYear="";discRating="";discRuntime="";discSort="pop";discPage=1;renderDiscChips();renderActiveChips();loadDiscover();};
}

function loadDiscover(append, refresh){
  const g=document.getElementById("discGrid");
  const st=document.getElementById("discStatus");
  const more=document.getElementById("discMore");
  const genre=discGenres.join(",");
  const country=discCountry;
  const decade=discYear;
  const rating=discRating;
  const runtime=discRuntime;
  const fparts=[];
  if(genre){const gs=discGenres.map(x=>_label(_genreList(),x)).join("/");if(gs)fparts.push("类型="+gs);}
  if(country){const cn=_label(COUNTRIES,country);if(cn)fparts.push("国别="+cn);}
  if(decade){const dn=_label(YEARS,decade);if(dn)fparts.push(dn);}
  if(rating){const rn=_label(RATINGS,rating);if(rn)fparts.push(rn);}
  if(runtime){const tn=_label(RUNTIMES,runtime);if(tn)fparts.push(tn);}
  if(discSort&&discSort!=="pop"){const sn=_label(SORTS,discSort);if(sn)fparts.push("排序="+sn);}
  if(!append){discPage=1;if(g)g.innerHTML="";}
  st.textContent=append?"正在加载更多…":"正在从 TMDB 拉取…";
  st.className="muted";
  const qs="/api/discover?kind="+discKind+"&cat="+discCat+"&page="+discPage
    +(genre?"&genre="+encodeURIComponent(genre):"")
    +(country?"&country="+encodeURIComponent(country):"")
    +(decade?"&decade="+encodeURIComponent(decade):"")
    +(rating?"&rating="+encodeURIComponent(rating):"")
    +(runtime?"&runtime="+encodeURIComponent(runtime):"")
    +(discSort?"&sort="+encodeURIComponent(discSort):"")
    +(refresh?"&refresh=1":"");
  jget(qs).then(d=>{
    if(d.configured===false){
      st.className="err";
      st.innerHTML='⚠️ 未配置 TMDB_API_KEY。请在「配置」页的 <code>TMDB API Key</code> 一栏填写'
        +'（免费，在 themoviedb.org 申请），保存后即时生效。';
      if(g)g.innerHTML="";if(more)more.innerHTML="";return;
    }
    if(!d.ok){st.className="err";st.textContent="❌ "+(d.error||"拉取失败");if(g)g.innerHTML="";if(more)more.innerHTML="";return;}
    const items=d.items||[];
    discPage=d.page||discPage;
    discTotal=d.totalPages||1;
    if(!items.length){if(!append)st.textContent="暂无内容";if(g)g.innerHTML="";if(more)more.innerHTML="";return;}
    const loaded=(append?(g?g.querySelectorAll(".card").length:0):0)+items.length;
    st.textContent="已加载 "+loaded+" 个"+(d.totalResults?(" · 共 "+d.totalResults+" 个"):"")+(fparts.length?(" · "+fparts.join(" · ")):"")+" · 点「添加下载」即加入队列";
    if(!append&&g)g.innerHTML="";
    items.forEach(it=>{
      const r=Math.round(it.rating||0);
      const sub=(it.year||"")+(r?' · ★ '+r:"");
      const ch=buildCard({poster:it.poster,title:it.title,kind:it.kind,sub:sub,
        acts:'<button class="btn" data-add="'+escAttr(it.kind+":"+it.tmdbId)+'">添加下载</button>'});
      const wrap=document.createElement("div");wrap.innerHTML=ch;
      const card=wrap.firstElementChild;
      card.style.cursor="pointer";
      card.setAttribute("data-detail",it.kind+":"+it.tmdbId);
      g.appendChild(card);
    });
    g.querySelectorAll('[data-detail]').forEach(c=>{
      c.onclick=(e)=>{ if(e.target.closest('button'))return; const p=c.getAttribute('data-detail').split(':'); openDetail(p[0],p[1]); };
    });
    g.querySelectorAll('button[data-add]').forEach(b=>{
      b.onclick=(e)=>{ e.stopPropagation(); const p=b.getAttribute('data-add').split(':');
        b.disabled=true;b.textContent="添加中…";
        jpost("/api/discover/add",{kind:p[0],tmdbId:Number(p[1]),seasonMode:document.getElementById("seasonMode").value}).then(res=>handleAddResult(res,b)).catch(e=>{b.disabled=false;b.textContent="添加下载";toast("❌ "+e,"err");});
      };
    });
    if(more){
      more.innerHTML="";
      if(discPage<discTotal){
        const mb=document.createElement("button");
        mb.className="btn";mb.textContent="加载更多";
        mb.style.width="100%";mb.style.margin="14px 0";
        mb.onclick=()=>{discPage++;loadDiscover(true);};
        more.appendChild(mb);
      }else if(discTotal>1){
        const tip=document.createElement("div");
        tip.className="muted";tip.style.textAlign="center";tip.style.margin="12px 0";
        tip.textContent="—— 已经到底啦 ——";
        more.appendChild(tip);
      }
    }
  }).catch(e=>{st.className="err";st.textContent="❌ 请求失败: "+e;if(more)more.innerHTML="";});
}

function openDetail(kind,tmdbId){
  const m=document.getElementById("discDetail"), body=document.getElementById("detailBody");
  m.style.display="flex";document.body.style.overflow="hidden";
  body.innerHTML='<div class="muted" style="padding:40px;text-align:center">正在加载详情…</div>';
  jget("/api/detail?kind="+kind+"&tmdbId="+encodeURIComponent(tmdbId)).then(d=>{
    if(d.configured===false){
      body.innerHTML='<div class="err" style="padding:24px">⚠️ 未配置 TMDB_API_KEY，无法拉取详情。</div>';return;
    }
    if(!d.ok){body.innerHTML='<div class="err" style="padding:24px">❌ '+(d.error||"拉取失败")+'</div>';return;}
    const genres=(d.genres||[]).map(g=>'<span class="detail-tag">'+esc(g)+'</span>').join("");
    const countries=(d.countries||[]).map(c=>'<span class="detail-tag">'+esc(c)+'</span>').join("");
    const r=Math.round(d.rating||0);
    const extra=(d.kind==="tv"&&(d.seasons||d.episodes))
      ? '<div class="s">'+[d.seasons?("全 "+d.seasons+" 季"):null,d.episodes?("共 "+d.episodes+" 集"):null].filter(Boolean).join(" · ")+'</div>' : "";
    const ov=(d.overview?('<div class="detail-overview">'+esc(d.overview)+'</div>'):'<div class="muted">暂无简介。</div>');
    const tag=(d.tagline?'<div class="detail-tagline">'+esc(d.tagline)+'</div>':"");
    body.innerHTML=
      (d.backdrop?'<img class="detail-backdrop" src="'+esc(d.backdrop)+'" alt=""/>':'')
      +'<div class="detail-head">'
      +'<div class="detail-title">'+esc(d.title||"")+'</div>'
      +(d.originalTitle&&d.originalTitle!==d.title?'<div class="s">原名：'+esc(d.originalTitle)+'</div>':"")
      +'<div class="s">'+(d.year||"")+(r?' · ★ '+r:"")+(d.status?(' · '+esc(d.status)):"")+'</div>'
      +(genres?'<div class="detail-tags">'+genres+'</div>':"")
      +(countries?'<div class="detail-tags">'+countries+'</div>':"")
      +(d.runtime?('<div class="s">片长 '+d.runtime+' 分钟</div>'):"")
      +extra
      +tag
      +'</div>'
      +ov
      +'<div class="detail-acts"><button class="btn" id="detailAdd">添加下载</button></div>';
    const ab=document.getElementById("detailAdd");
    ab.onclick=()=>{
      ab.disabled=true;ab.textContent="添加中…";
      jpost("/api/discover/add",{kind:kind,tmdbId:tmdbId,
        seasonMode:document.getElementById("seasonMode").value}).then(res=>{
        handleAddResult(res,ab);
      }).catch(e=>{ab.disabled=false;ab.textContent="添加下载";toast("❌ "+e,"err");});
    };
  }).catch(e=>{body.innerHTML='<div class="err" style="padding:24px">❌ 请求失败: '+esc(""+e)+'</div>';});
}
function closeDetail(){
  const m=document.getElementById("discDetail");
  if(m)m.style.display="none";
  document.body.style.overflow="";
}

function loadProfiles(){
  const sel=document.getElementById("profile");
  const url=(MODE==="tv")?"/api/profiles?kind=tv":"/api/profiles";
  jget(url).then(d=>{
    sel.innerHTML='<option value="">默认画质</option>';
    (d.profiles||[]).forEach(p=>{
      const o=document.createElement("option");o.value=p.name;o.textContent=p.name;
      sel.appendChild(o);});
  }).catch(()=>{});
}

function loadRootFolders(){
  const sel=document.getElementById("rootfolder");
  const url=(MODE==="tv")?"/api/rootfolders?kind=tv":"/api/rootfolders";
  jget(url).then(d=>{
    sel.innerHTML='<option value="">默认目录</option>';
    (d.rootfolders||[]).forEach(r=>{
      const o=document.createElement("option");o.value=r.path;
      o.textContent=r.path+(r.free?(" ("+fmtSize(r.free)+" 可用)"):"");
      sel.appendChild(o);});
  }).catch(()=>{sel.innerHTML='<option value="">默认目录</option>';});
}

// 支持直接粘贴 TMDB/IMDB 链接或纯数字 ID，跳过搜索候选直接添加
function parseDirectId(term){
  if(!term)return null;
  term=term.trim();
  let m=term.match(/themoviedb\.org\/(?:movie|tv)\/(\d+)/i);
  if(m)return {type:"tmdb",id:m[1]};
  if(/^\d{4,10}$/.test(term))return {type:"tmdb",id:term};
  m=term.match(/imdb\.com\/.*?(tt\d+)/i)||term.match(/^tt\d+$/i);
  if(m)return {type:"imdb",id:m[1]};
  return null;
}

function handleAddResult(res, btn){
  if(res.ok){
    if(res.dryRun){
      toast("🔎 预览："+res.title+(res.alreadyInRadarr?"（已在库）":"")+" → "+res.action,"ok");
      if(btn){btn.textContent="已预览";btn.disabled=false;}
    }else{
      toast("✅ "+res.title+" 已添加，正在搜索资源…","ok");
      if(btn)btn.textContent="✓ 已添加";
      switchTo("queue"); loadQueue(); if(res.title)watchAdded(res.title);
    }
  }else{
    toast("❌ "+(res.error||"添加失败"),"err");
    if(btn){btn.disabled=false;btn.textContent="重试";}
  }
}

function doSearch(){
  const raw=document.getElementById("term").value.trim();
  if(!raw){setStatus("请先输入片名或剧名","err");return;}
  const prof=document.getElementById("profile").value;
  const rf=document.getElementById("rootfolder").value;
  const dry=document.getElementById("dryRun").checked;
  const base={profile:prof,rootFolderPath:rf,dryRun:dry};
  // 直接 ID / 链接：跳过候选搜索直接添加（TMDB/IMDB 仅支持电影；剧集请走搜索）
  const did=parseDirectId(raw);
  if(did){
    setStatus("直接添加 "+did.type.toUpperCase()+" "+did.id+" …","");
    const body=did.type==="imdb"?Object.assign({imdbId:did.id,name:null},base)
                                :Object.assign({tmdbId:did.id,name:null},base);
    jpost("/api/movie",body).then(handleAddResult);
    return;
  }
  setStatus("正在 TMDB 查找电影与剧集匹配…","");
  document.getElementById("cands").innerHTML="";
  jpost("/api/search",{term:raw}).then(d=>{
    if(!d.ok){setStatus("❌ "+ (d.error||"搜索失败"),"err");return;}
    const cs=d.candidates||[];
    if(!cs.length){setStatus("没有匹配结果","err");return;}
    setStatus("找到 "+cs.length+" 个候选（电影/剧集混合），点「添加」即下载","ok");
    const grid=document.getElementById("cands");
    cs.forEach(c=>{
      const isTv=(c.kind==="tv");
      const r=Math.round(c.rating||0);
      let sub=(c.year||"")+(r?' · ★ '+r:"");
      if(isTv) sub=(c.year||"")+(c.seasons?' · '+c.seasons+' 季':"")+(c.network?' · '+esc(c.network):"")+(c.status?' · '+esc(c.status):"");
      let libBadge="";
      if(c.inLibrary==="downloaded")libBadge='<span class="badge ok">已下载</span>';
      else if(c.inLibrary==="downloading")libBadge='<span class="badge dl">⬇️ 下载中</span>';
      else if(c.inLibrary==="waiting")libBadge='<span class="badge wait">⏳ 待源</span>';
      const extra=(c.overview&&!isTv)?'<div class="s" style="margin-top:5px;line-height:1.4">'+esc(c.overview)+'</div>':"";
      const addLabel=isTv?"追这部剧":"添加并下载";
      const ch=buildCard({poster:c.poster,title:c.title,kind:c.kind,sub:sub,badges:libBadge?[libBadge]:[],extra:extra,
        acts:'<button class="btn" data-add="'+(isTv?"tv":"movie")+":"+(isTv?c.tvdbId:c.tmdbId)+'">'+addLabel+'</button>'});
      const wrap=document.createElement("div");wrap.innerHTML=ch;grid.appendChild(wrap.firstElementChild);
    });
    grid.querySelectorAll('button[data-add]').forEach(b=>{
      b.onclick=()=>{
        b.disabled=true;b.textContent="添加中…";
        const p=b.getAttribute('data-add').split(':');
        const url=p[0]==="tv"?"/api/series":"/api/movie";
        const body=p[0]==="tv"?Object.assign({tvdbId:Number(p[1]),seasonMode:document.getElementById("seasonMode").value},base):Object.assign({tmdbId:Number(p[1])},base);
        jpost(url,body).then(res=>handleAddResult(res,b));
      };
    });
  }).catch(e=>setStatus("❌ 请求失败: "+e,"err"));
}

function esc(s){const d=document.createElement("div");d.textContent=s||"";return d.innerHTML;}

let _qFilter="all";   // 队列筛选：all / movie / tv（独立于顶部电影·剧集模式）
let _spd=[]; const SPD_MAX=40;
function renderSpark(){
  const svg=document.getElementById("spdSpark");
  if(!svg)return;
  const w=150,h=26,pad=2;
  const n=_spd.length;
  if(n<2){svg.innerHTML="";return;}
  const max=Math.max.apply(null,_spd)||1;
  const step=(w-pad*2)/Math.max(1,(SPD_MAX-1));
  const x=i=>pad+i*step;
  const y=v=>h-pad-(v/max)*(h-pad*2);
  let line="",area="M"+x(0).toFixed(1)+","+(h-pad);
  _spd.forEach((v,i)=>{
    const px=x(i).toFixed(1),py=y(v).toFixed(1);
    line+=(i?" ":"")+px+","+py;
    area+=" L"+px+","+py;
  });
  area+=" L"+x(n-1).toFixed(1)+","+(h-pad)+" Z";
  svg.innerHTML='<path d="'+area+'" fill="#5fd38a" opacity="0.12"/>'+
                '<polyline points="'+line+'" fill="none" stroke="#5fd38a" stroke-width="1.6" stroke-linejoin="round"/>';
}
function loadQueue(){
  const url="/api/queue?kind="+_qFilter;
  jget(url).then(d=>{
    const box=document.getElementById("queue");const items=d.queue||[];
    const totalSpd=d.totalSpeed||0;
    _spd.push(totalSpd);
    if(_spd.length>SPD_MAX)_spd.shift();
    const spdNow=document.getElementById("spdNow");
    if(spdNow)spdNow.textContent= totalSpd? fmtSpeed(totalSpd):"空闲";
    renderSpark();
    if(!items.length){box.innerHTML='<div class="muted">当前没有进行中的下载。<br>刚添加的内容可能还在搜索中（见上方提示）；若长时间为空，可能暂无可下载资源——多为未上映/冷门内容，可稍后重试或换译名。</div>';return;}
    box.innerHTML=items.map(it=>{
      const pct=it.progress||0;
      const speed=it.speed?fmtSpeed(it.speed):"";
      const sz=fmtSize(it.size);
      const left=fmtSize(it.sizeleft);
      // 全部视图下打标签区分电影 / 剧集
      const kTag=(_qFilter==="all"&&it.kind)
        ? '<span class="tag '+(it.kind==="tv"?"warn":"")+'">'+(it.kind==="tv"?"📺 剧集":"🎬 电影")+'</span>' : "";
      return '<div class="queue-item"><div class="top"><b>'+esc(it.title||"")+'</b>'+
        '<button class="btn danger" data-cid="'+it.id+'" data-ckind="'+(it.kind||"")+'">取消</button></div>'+
        '<div class="sub">'+(it.status?'<span>'+esc(it.status)+'</span>':"")+kTag+
        (it.downloadClient?'<span>'+esc(it.downloadClient)+'</span>':"")+
        (it.indexer?'<span>'+esc(it.indexer)+'</span>':"")+(sz?'<span>'+sz+'</span>':"")+
        (left?'<span>剩余 '+left+'</span>':"")+(speed?'<span>'+speed+'</span>':"")+
        (it.timeleft?'<span>ETA '+esc(it.timeleft)+'</span>':"")+'</div>'+
        '<div class="bar"><i style="width:'+pct+'%"></i></div>'+
        '<div class="muted" style="margin-top:4px">'+pct+'%</div></div>';
    }).join("");
    // 用 data 属性绑定，避免内联 onclick 的引号嵌套（历史上把整段 script 搞崩过）
    box.querySelectorAll("button[data-cid]").forEach(b=>{
      b.onclick=()=>cancelQ(b.getAttribute("data-cid"),b.getAttribute("data-ckind"));
    });
  }).catch(e=>{document.getElementById("queue").innerHTML='<div class="err">队列加载失败: '+e+'</div>';});
}

function cancelQ(id,kind){
  // kind 缺省时回退到顶部模式，保证老调用也能正常工作
  const k=kind||(MODE==="tv"?"tv":"movie");
  fetch("/api/queue/"+id+"?kind="+k,{method:"DELETE",headers:authHdr()}).then(()=>{toast("已取消");loadQueue();});
}

let _watchTimer=null;
function watchAdded(title){
  const el=document.getElementById("qwatch");
  if(!el)return;
  let n=0;
  if(_watchTimer)clearInterval(_watchTimer);
  const tick=()=>{
    n++;
    // 查全部队列，避免当前筛选正好过滤掉刚添加的那一项
    jget("/api/queue?kind=all").then(d=>{
      const items=d.queue||[];
      const t=(title||"").toLowerCase();
      const hit=items.find(it=>(it.title||"").toLowerCase().includes(t));
      if(hit){el.textContent="✅ 已在下载队列：《"+title+"》";clearInterval(_watchTimer);_watchTimer=null;}
      else if(n>=15){el.textContent="⚠️ 《"+title+"》已搜索完成，暂无可下载资源（多见于未上映新片/冷门片）。可稍后重试或换译名。";clearInterval(_watchTimer);_watchTimer=null;}
      else{el.textContent="🔍 正在为《"+title+"》搜索资源… ("+n+"/15)";}
    }).catch(()=>{});
  };
  tick();
  _watchTimer=setInterval(tick,4000);
}

let _libItems=[];
let _libFilter="all";
const LIB_PAGE=60; let _libPage=LIB_PAGE; let _tvPage=LIB_PAGE;
function libState(m){
  if(m.downloaded)return "downloaded";
  if(m.downloading)return "downloading";
  if(m.monitored)return "waiting";
  return "off";
}
function renderLib(){
  const box=document.getElementById("lib");
  const items=_libItems.filter(m=>_libFilter==="all"||libState(m)===_libFilter);
  if(!_libItems.length){box.innerHTML='<div class="muted">媒体库为空。</div>';return;}
  if(!items.length){box.innerHTML='<div class="muted">该筛选下没有影片。</div>';return;}
  const shown=items.slice(0,_libPage);
  box.innerHTML=shown.map(m=>{
    const st=libState(m);
    let badge="";
    if(st==="downloaded") badge='<span class="badge ok">已下载</span>';
    else if(st==="downloading") badge='<span class="badge dl">⬇️ 下载中</span>';
    else if(st==="waiting") badge='<span class="badge wait">⏳ 待源</span>';
    else badge='<span class="badge off">未监控</span>';
    const resBtn = st!=="downloaded" ? '<button class="btn ghost" data-res="'+m.id+'">重新搜索</button>' : "";
    return buildCard({
      poster:m.poster, title:m.title, kind:"movie",
      sub:(m.year||"")+(m.quality?' · '+m.quality:""),
      badges:[badge],
      acts:'<button class="btn danger" data-del="'+m.id+'">移除</button>'+resBtn
    });
  }).join("");
  box.querySelectorAll("button[data-del]").forEach(b=>{
    b.onclick=function(){
      const id=b.getAttribute("data-del");
      const t=b.closest(".card").querySelector(".t").textContent;
      if(confirm("移除《"+t+"》？")){
        fetch("/api/movie/"+id,{method:"DELETE",headers:authHdr()}).then(()=>{toast("已移除");loadLibrary();});
      }
    };
  });
  box.querySelectorAll("button[data-res]").forEach(b=>{
    b.onclick=function(){
      const id=b.getAttribute("data-res");
      b.disabled=true;b.textContent="搜索中…";
      fetch("/api/movie/"+id+"/search",{method:"POST",headers:authHdr()}).then(()=>{
        toast("已触发重新搜索，稍后看队列/媒体库更新","ok");
        b.textContent="✓ 已触发";
        setTimeout(()=>{loadLibrary();},1500);
      }).catch(()=>{b.disabled=false;b.textContent="重新搜索";});
    };
  });
  const moreBox=document.getElementById("libMore");
  if(items.length>_libPage){
    moreBox.innerHTML='<button class="btn ghost" id="libMoreBtn">显示更多（剩余 '+(items.length-_libPage)+'）</button>';
    document.getElementById("libMoreBtn").onclick=()=>{_libPage+=LIB_PAGE;renderLib();};
  }else moreBox.innerHTML="";
}
function loadLibrary(){
  jget("/api/movies").then(d=>{
    _libItems=d.movies||[];
    _libPage=LIB_PAGE;
    renderLib();
  }).catch(e=>{document.getElementById("lib").innerHTML='<div class="err">加载失败: '+e+'</div>';});
}

// 批量重新搜索：对所有「待源（已监控但未下载）」条目触发一次搜索
function bulkRescan(){
  const btn=document.getElementById("bulkRescan");
  const isTv=MODE==="tv";
  const items=isTv?_tvItems:_libItems;
  const stFn=isTv?tvState:libState;
  const waiting=items.filter(m=>stFn(m)==="waiting");
  if(!waiting.length){toast("没有待源条目需要重搜","ok");return;}
  if(!confirm("对 "+waiting.length+" 个待源条目批量重新搜索？"))return;
  if(btn){btn.disabled=true;btn.textContent="重搜中…";}
  let done=0,fail=0;const total=waiting.length;
  const finish=()=>{ if(done+fail===total){
    if(btn){btn.disabled=false;btn.textContent="批量重搜";}
    toast("批量重搜：成功 "+done+" / 失败 "+fail,"ok");
    setTimeout(()=>{ if(MODE==="tv")loadSeriesLibrary();else loadLibrary(); },1500);
  }};
  waiting.forEach(m=>{
    const url=isTv?("/api/series/"+m.id+"/search"):("/api/movie/"+m.id+"/search");
    fetch(url,{method:"POST",headers:authHdr()}).then(()=>{done++;}).catch(()=>{fail++;}).finally(finish);
  });
}

// ---- 剧集库（Sonarr） ----
let _tvItems=[];
let _tvFilter="all";
function tvState(m){
  if(m.downloaded)return "downloaded";
  if(m.downloading)return "downloading";
  if(m.monitored)return "waiting";
  return "off";
}
function renderSeries(){
  const box=document.getElementById("lib");
  const items=_tvItems.filter(m=>_tvFilter==="all"||tvState(m)===_tvFilter);
  if(!_tvItems.length){box.innerHTML='<div class="muted">剧集库为空。切到「🔍 搜索下载」，用顶部「📺 剧集」模式添加即可。</div>';return;}
  if(!items.length){box.innerHTML='<div class="muted">该筛选下没有剧集。</div>';return;}
  const shown=items.slice(0,_tvPage);
  box.innerHTML=shown.map(m=>{
    const st=tvState(m);
    let badge="";
    if(st==="downloaded")badge='<span class="badge ok">已下载</span>';
    else if(st==="downloading")badge='<span class="badge dl">⬇️ 下载中</span>';
    else if(st==="waiting")badge='<span class="badge wait">⏳ 待源</span>';
    else badge='<span class="badge off">未监控</span>';
    const resBtn=st!=="downloaded"?'<button class="btn ghost" data-res="'+m.id+'">重新搜索</button>':"";
    const ep=m.totalEpisodeCount?((m.episodeFileCount||0)+"/"+m.totalEpisodeCount+" 集"):"";
    return buildCard({
      poster:m.poster, title:m.title, kind:"tv",
      sub:(m.year||"")+(ep?' · '+ep:"")+(m.network?' · '+esc(m.network):""),
      badges:[badge],
      acts:'<button class="btn danger" data-del="'+m.id+'">移除</button>'+resBtn
    });
  }).join("");
  box.querySelectorAll("button[data-del]").forEach(b=>{
    b.onclick=function(){
      const id=b.getAttribute("data-del");
      const t=b.closest(".card").querySelector(".t").textContent;
      if(confirm("移除《"+t+"》？该剧已下载的文件也会一并删除。")){
        fetch("/api/series/"+id,{method:"DELETE",headers:authHdr()}).then(()=>{toast("已移除");loadSeriesLibrary();});
      }
    };
  });
  box.querySelectorAll("button[data-res]").forEach(b=>{
    b.onclick=function(){
      const id=b.getAttribute("data-res");
      b.disabled=true;b.textContent="搜索中…";
      fetch("/api/series/"+id+"/search",{method:"POST",headers:authHdr()}).then(()=>{
        toast("已触发重新搜索，稍后看队列/剧集库更新","ok");
        b.textContent="✓ 已触发";
        setTimeout(()=>{loadSeriesLibrary();},1500);
      }).catch(()=>{b.disabled=false;b.textContent="重新搜索";});
    };
  });
  const moreBox=document.getElementById("libMore");
  if(items.length>_tvPage){
    moreBox.innerHTML='<button class="btn ghost" id="libMoreBtn">显示更多（剩余 '+(items.length-_tvPage)+'）</button>';
    document.getElementById("libMoreBtn").onclick=()=>{_tvPage+=LIB_PAGE;renderSeries();};
  }else moreBox.innerHTML="";
}
function loadSeriesLibrary(){
  jget("/api/series").then(d=>{
    _tvItems=d.series||[];
    _tvPage=LIB_PAGE;
    renderSeries();
  }).catch(e=>{document.getElementById("lib").innerHTML='<div class="err">加载失败（Sonarr 未就绪？）: '+e+'</div>';});
}

// 各服务的对外端口（用于生成直达链接），与 docker-compose 保持一致
const SVC_PORTS={radarr:7878,sonarr:8989,prowlarr:9696,qbittorrent:8085,flaresolverr:8191};
// 一键登录：用统一账号 admin / MediaFn2026 向各 *arr 的 /login 表单发起同源导航提交，
// 新标签页直接带着会话 cookie 进入已登录状态（无需手动输入）。
function svcLogin(key,port){
  const f=document.createElement('form');
  f.method='POST';
  f.action='http://'+location.hostname+':'+port+'/login';
  f.target='_blank';
  f.style.display='none';
  f.innerHTML='<input name="username" value="admin">'+
              '<input name="password" value="MediaFn2026">'+
              '<input name="rememberMe" value="true">';
  document.body.appendChild(f); f.submit(); f.remove();
  toast('已在新标签页用 admin / MediaFn2026 登录 '+key,'ok');
}
function loadSystem(){
  jget("/api/system").then(d=>{
    let h='';
    if(d.authEnabled===false){
      h+='<div class="queue-item" style="border-color:#5a2b3c;background:#2a1720">'+
         '<b>⚠️ 未启用访问鉴权</b> '+
         '<span class="muted">AUTOPILOT_TOKEN 为空，影视下载台当前对任何能访问 8787 端口的人开放。'+
         '公网/多人环境请在 compose 里设置 AUTOPILOT_TOKEN。</span></div>';
    }
    const svcs=d.services||[];
    if(svcs.length){
      h+='<div class="muted" style="margin:2px 0 8px">服务总览 · '+
         svcs.filter(s=>s.ok).length+'/'+svcs.length+' 正常</div>';
      h+=svcs.map(s=>{
        const dot=s.ok?'<span class="sdot ok"></span>':'<span class="sdot err"></span>';
        const port=SVC_PORTS[s.key];
        const scheme='http';
        const link=port?'<a class="slink" href="'+scheme+'://'+location.hostname+':'+port+
                   '" target="_blank" rel="noopener noreferrer">打开 ↗</a>':'';
        const loginBtn=(s.key==='radarr'||s.key==='sonarr'||s.key==='prowlarr')?
          '<button class="btn ghost" style="margin-left:6px;padding:2px 8px;font-size:12px" onclick="svcLogin('+JSON.stringify(s.key)+','+port+')">一键登录</button>':'';
        return '<div class="queue-item"><div class="top"><b>'+dot+esc(s.name||"")+'</b>'+
          '<span class="muted">'+esc(s.detail||"")+link+loginBtn+'</span></div>'+
          (s.desc?'<div class="svc-desc">'+esc(s.desc)+'</div>':'')+'</div>';
      }).join("");
    }
    // 抓取完成 webhook 通知状态
    if(d.webhook){
      const wh=d.webhook;
      const whHost=(wh.url||"").replace(/^https?:\/\//,'').split('/')[0]||"—";
      const whLine = wh.configured
        ? ('已开启 → <b>'+esc(whHost)+'</b> · 已发送 '+wh.sent+(wh.last?' · 最近 '+wh.last:''))
        : '未配置（在 compose 设置 <code>AUTOPILOT_WEBHOOK_URL</code> 开启）';
      h+='<div class="queue-item" style="border-color:#23304a;background:#141b2b">'+
         '<div class="top"><b>🔔 抓取完成通知</b>'+
         '<button class="btn ghost" onclick="testWebhook()">发送测试</button></div>'+
         '<div class="muted">'+whLine+'</div></div>';
    }
    (d.disks||[]).forEach(x=>{
      const used=x.total-x.free, pct=x.total?Math.round(used/x.total*100):0;
      const low = x.total && (x.free/x.total) < 0.1;
      const barColor = low ? 'linear-gradient(90deg,#e0533f,#ff7a5c)' : 'linear-gradient(90deg,#5fd38a,#e0a85f)';
      h+='<div class="queue-item"><div class="top"><b>'+x.path+'</b><span class="muted">'+fmtSize(x.free)+' 可用 / '+fmtSize(x.total)+'</span></div>'+
        '<div class="bar"><i style="width:'+pct+'%;background:'+barColor+'"></i></div>'+
        '<div class="muted" style="margin-top:4px">已用 '+pct+'%'+(low?' · ⚠️ 空间不足':'')+'</div></div>';
    });
    document.getElementById("sysbody").innerHTML=h;
    // 顶栏 pill
    document.getElementById("pillver").textContent=d.appName+" v"+(d.radarrVersion||"?");
    document.getElementById("pillmv").textContent=d.totalMovies;
    document.getElementById("pilldl").textContent=d.downloadedMovies;
    document.getElementById("pillidx").textContent=d.indexers?(d.indexers.enabled+"/"+d.indexers.total):"—";
    const d0=(d.disks||[])[0];
    document.getElementById("pilldisk").textContent=d0?("磁盘 "+fmtSize(d0.free)+" 可用"):"";
  }).catch(e=>{document.getElementById("sysbody").innerHTML='<div class="err">加载失败: '+e+'</div>';});
}

function testWebhook(){
  fetch("/api/webhook/test",{method:"POST",headers:authHdr()}).then(r=>r.json()).then(d=>{
    if(d.ok) toast("✅ 测试通知已发送，去接收端确认","ok");
    else if(d.error && d.error.indexOf("未配置")>=0) toast("⚠️ "+d.error,"err");
    else toast("❌ 发送失败："+(d.error||"检查 AUTOPILOT_WEBHOOK_URL 与接收端可达性"),"err");
  }).catch(()=>toast("❌ 请求失败","err"));
}

// 日历视图（F9）：Radarr 电影上映 + Sonarr 剧集播出，按月网格展示
function ymd(y,m,d){return y+"-"+String(m+1).padStart(2,"0")+"-"+String(d).padStart(2,"0");}
let _calY=0,_calM=0,_calMap={};
function loadCalendar(){
  const now=new Date();
  if(!_calY){_calY=now.getFullYear();_calM=now.getMonth();}
  const start=ymd(_calY,_calM,1), end=ymd(_calY,_calM+1,1);
  jget("/api/calendar?start="+start+"&end="+end).then(d=>{
    _calMap={};
    (d.events||[]).forEach(e=>{ (_calMap[e.date]=_calMap[e.date]||[]).push(e); });
    renderCalendar();
  }).catch(e=>{document.getElementById("calGrid").innerHTML='<div class="err">加载失败: '+e+'</div>';});
}
function renderCalendar(){
  const box=document.getElementById("calGrid");
  document.getElementById("calTitle").textContent=_calY+"年"+(_calM+1)+"月";
  const firstDow=(new Date(_calY,_calM,1).getDay()+6)%7;
  const days=new Date(_calY,_calM+1,0).getDate();
  let cells="";
  for(let i=0;i<firstDow;i++)cells+='<div class="cal-cell cal-empty"></div>';
  const tToday=ymd(new Date().getFullYear(),new Date().getMonth(),new Date().getDate());
  for(let d=1;d<=days;d++){
    const ds=ymd(_calY,_calM,d);
    const evs=_calMap[ds]||[];
    let chips="";
    evs.slice(0,3).forEach(e=>{
      const ic=e.kind==="tv"?"📺":"🎬";
      chips+='<div class="cal-chip '+(e.kind==="tv"?"tv":"mv")+'" data-title="'+esc(e.title)+'" data-kind="'+e.kind+'" title="'+esc(e.title)+(e.sub?(" · "+esc(e.sub)):"")+'">'+ic+esc((e.title||"").slice(0,8))+'</div>';
    });
    if(evs.length>3)chips+='<div class="cal-more">+'+(evs.length-3)+'</div>';
    cells+='<div class="cal-cell'+(ds===tToday?" cal-today":"")+'"><div class="cal-d">'+d+'</div>'+chips+'</div>';
  }
  box.innerHTML=cells;
  box.querySelectorAll(".cal-chip").forEach(ch=>{ ch.onclick=()=>calAdd(ch.getAttribute("data-title"),ch.getAttribute("data-kind")); });
}
function prevMonth(){ if(!_calY){_calY=new Date().getFullYear();_calM=new Date().getMonth();} _calM--; if(_calM<0){_calM=11;_calY--;} loadCalendar(); }
function nextMonth(){ if(!_calY){_calY=new Date().getFullYear();_calM=new Date().getMonth();} _calM++; if(_calM>11){_calM=0;_calY++;} loadCalendar(); }
function calAdd(title,kind){
  if(!title)return;
  if(kind==="tv"&&MODE!=="tv")setMode("tv");
  else if(kind==="movie"&&MODE!=="movie")setMode("movie");
  document.getElementById("term").value=title;
  switchTo("search");
  doSearch();
}

// 抓取历史：事件类型 -> 中文标签与配色
const HIST_LABEL={
  "grabbed":{t:"✅ 抓取完成",c:"ok"},
  "episodeDownload":{t:"✅ 抓取完成",c:"ok"},
  "downloadFolderImported":{t:"📥 已入库",c:"ok"},
  "episodeFileImported":{t:"📥 已入库",c:"ok"},
  "downloadFailed":{t:"❌ 下载失败",c:"err"},
  "movieFileDeleted":{t:"🗑 已删除",c:"off"},
  "episodeFileDeleted":{t:"🗑 已删除",c:"off"},
  "movieFileRenamed":{t:"✏️ 重命名",c:"off"},
  "episodeFileRenamed":{t:"✏️ 重命名",c:"off"},
  "downloadIgnored":{t:"⏭ 已忽略",c:"off"}
};
function _fmtDate(s){return s?esc(String(s).replace("T"," ").replace("Z","")):"";}

let _histKind="all";
function loadHistory(){
  jget("/api/history?kind="+_histKind).then(d=>{
    const box=document.getElementById("history");
    const items=d.history||[];
    if(!items.length){box.innerHTML='<div class="muted">暂无抓取记录。</div>';return;}
    const filt=(k,l)=>'<button class="fbtn'+( _histKind===k?' active':'')+'" data-h="'+k+'">'+l+'</button>';
    let h='<div class="qrow"><div class="filters" id="hFilters">'+filt("all","全部")+filt("movie","🎬 电影")+filt("tv","📺 剧集")+'</div></div>';
    h+=items.map(it=>{
      const ev=HIST_LABEL[it.eventType]||{t:(it.eventType||"未知"),c:"off"};
      const evTag='<span class="tag '+(ev.c==="ok"?"ok":(ev.c==="err"?"off":"off"))+'">'+esc(ev.t)+'</span>';
      const kTag=(_histKind==="all"&&it.kind)?'<span class="tag '+(it.kind==="tv"?"warn":"")+'">'+(it.kind==="tv"?"📺 剧集":"🎬 电影")+'</span>':"";
      const idx=it.indexer?'<span class="tag">'+esc(it.indexer)+'</span>':"";
      const q=it.quality?'<span class="tag">'+esc(it.quality)+'</span>':"";
      const sz=it.size?fmtSize(it.size):"";
      return '<div class="queue-item"><div class="top"><b>'+esc(it.title||"")+'</b>'+evTag+'</div>'+
        '<div class="sub">'+kTag+idx+q+(sz?'<span>'+sz+'</span>':"")+(it.date?'<span>'+_fmtDate(it.date)+'</span>':"")+'</div></div>';
    }).join("");
    box.innerHTML=h;
    box.querySelectorAll("#hFilters .fbtn").forEach(b=>{b.onclick=()=>{
      _histKind=b.getAttribute("data-h");loadHistory();
    };});
  }).catch(e=>{document.getElementById("history").innerHTML='<div class="err">加载失败: '+e+'</div>';});
}

function _localTime(iso){
  if(!iso)return "";
  const d=new Date(iso);
  if(isNaN(d.getTime()))return iso;
  const p=n=>String(n).padStart(2,"0");
  return p(d.getMonth()+1)+"-"+p(d.getDate())+" "+p(d.getHours())+":"+p(d.getMinutes());
}
function enableIndexer(name,btn){
  if(btn){btn.disabled=true;btn.textContent="启用中…";}
  toast("正在重新启用「"+name+"」…");
  fetch("/api/indexers/"+encodeURIComponent(name)+"/enable",{method:"POST",headers:authHdr()}).then(r=>r.json()).then(d=>{
    toast(d.ok?"✅ "+(d.message||"已启用"):"❌ "+(d.error||"启用失败"), d.ok?"ok":"err");
    loadIndexers();
  }).catch(()=>toast("❌ 请求失败","err"));
}
function enableAllIndexers(){
  const btn=document.getElementById("enableAllIdx");
  if(btn){btn.disabled=true;btn.textContent="启用中…";}
  jget("/api/indexers").then(d=>{
    const down=(d.indexers||[]).filter(r=>r.status==="autoDisabled"||r.status==="hadFailure").map(r=>r.name);
    if(!down.length){toast("没有需要启用的失效索引器","ok");if(btn){btn.disabled=false;btn.textContent="全部重启用失效索引器";}return;}
    let done=0,fail=0;
    down.forEach(n=>{
      fetch("/api/indexers/"+encodeURIComponent(n)+"/enable",{method:"POST",headers:authHdr()}).then(r=>r.json()).then(x=>{if(x.ok)done++;else fail++;}).catch(()=>fail++).finally(()=>{
        if(done+fail===down.length){toast("重启用完成：成功 "+done+" / 失败 "+fail,"ok");loadIndexers();}
      });
    });
  }).catch(()=>{toast("❌ 读取索引器失败","err");if(btn){btn.disabled=false;btn.textContent="全部重启用失效索引器";}});
}
function loadIndexers(){
  jget("/api/indexers").then(d=>{
    const box=document.getElementById("indexers");
    const rows=d.indexers||[];
    if(!rows.length){box.innerHTML='<div class="muted">未配置索引器，或 Prowlarr 不可达。</div>';return;}
    const ST={
      healthy:{t:"正常",c:"ok"},
      disabled:{t:"未启用",c:"off"},
      autoDisabled:{t:"已停用(失败)",c:"err"},
      hadFailure:{t:"曾失败",c:"warn"}
    };
    const anyDown=rows.some(r=>r.status==="autoDisabled"||r.status==="hadFailure");
    let h='<div class="muted" style="margin:2px 0 8px">资源库 '+d.enabled+'/'+d.total+' 启用'+
      (anyDown?' · <button class="btn ghost" id="enableAllIdx">全部重启用失效索引器</button>':"")+'</div>';
    h+=rows.map(r=>{
      const st=ST[r.status]||{t:(r.status||"?"),c:"off"};
      const proto=r.protocol==="torrent"?"BT":(r.protocol==="usenet"?"Usenet":(r.protocol||""));
      const priv=r.privacy==="public"?"公开":(r.privacy==="private"?"私有":(r.privacy==="semiPrivate"?"半私有":(r.privacy||"")));
      const stTag='<span class="tag '+(st.c==="ok"?"ok":(st.c==="err"?"off":st.c==="warn"?"warn":"off"))+'">'+esc(st.t)+'</span>';
      const pf='<span class="tag">'+esc(proto)+'</span><span class="tag">'+esc(priv)+'</span>';
      const kTag=r.enable?'':'<span class="tag off">未启用</span>';
      const canEnable=(r.status==="autoDisabled"||r.status==="hadFailure");
      const eta=(r.status==="autoDisabled"&&r.disabledTill)?'<span class="muted">预计 '+_localTime(r.disabledTill)+' 自动恢复</span>':"";
      const fail=r.lastFailure?'<span class="muted">最近失败 '+_fmtDate(r.lastFailure)+'</span>':"";
      const enBtn=canEnable?'<button class="btn ghost" data-en="'+esc(r.name||"")+'">重新启用</button>':"";
      return '<div class="queue-item"><div class="top"><b>'+esc(r.name||"")+'</b>'+stTag+enBtn+'</div>'+
        '<div class="sub">'+pf+kTag+eta+fail+'</div></div>';
    }).join("");
    box.innerHTML=h;
    box.querySelectorAll("button[data-en]").forEach(b=>{b.onclick=()=>enableIndexer(b.getAttribute("data-en"),b);});
    const ea=document.getElementById("enableAllIdx");
    if(ea)ea.onclick=enableAllIndexers;
  }).catch(e=>{document.getElementById("indexers").innerHTML='<div class="err">加载失败: '+e+'</div>';});
}


// 配置面板：单一代理出口 + TMDB（自包含，无需 bitmagnet-bot）
function apLoadConfig(){
  const st=document.getElementById("apCfgStatus");
  if(st)st.textContent="加载中…";
  jget("/api/config").then(d=>{
    if(!d||d.ok===false){ if(st)st.textContent="加载失败："+(d&&d.error||"未知"); return; }
    const v=d.values||{};
    document.getElementById("ap_PROXY_URL").value=v.PROXY_URL||"";
    document.getElementById("ap_TMDB_KEY").value=v.TMDB_KEY||"";
    if(st)st.textContent="已加载";
  }).catch(e=>{ if(st)st.textContent="加载失败："+e; });
}
function apSaveConfig(){
  const st=document.getElementById("apCfgStatus");
  if(st)st.textContent="保存中…";
  const payload={
    proxy_url:document.getElementById("ap_PROXY_URL").value.trim(),
    tmdb_key:document.getElementById("ap_TMDB_KEY").value.trim()
  };
  jpost("/api/config", payload).then(d=>{
    if(d&&d.ok){ if(st)st.textContent="已保存，出口代理重启中…"; toast("配置已写入"); }
    else { if(st)st.textContent="保存失败："+(d&&d.error||"未知"); toast("保存失败："+(d&&d.error||""),"err"); }
  }).catch(e=>{ if(st)st.textContent="保存失败："+e; toast("保存失败："+e,"err"); });
}

// init
loadProfiles();loadRootFolders();loadSystem();loadQueue();
setInterval(()=>{if(document.querySelector(".tab.active").dataset.p==="queue")loadQueue();},5000);
setInterval(()=>{loadSystem();},30000);
document.getElementById("term").addEventListener("keydown",e=>{if(e.key==="Enter")doSearch();});
document.querySelectorAll(".mbtn").forEach(b=>{
  b.onclick=()=>setMode(b.dataset.m);
});
document.querySelectorAll("#qFilters .fbtn").forEach(b=>{
  b.onclick=()=>{
    document.querySelectorAll("#qFilters .fbtn").forEach(x=>x.classList.remove("active"));
    b.classList.add("active");
    _qFilter=b.getAttribute("data-q");
    loadQueue();
  };
});
document.querySelectorAll("#libFilters .fbtn").forEach(b=>{
  b.onclick=()=>{
    document.querySelectorAll("#libFilters .fbtn").forEach(x=>x.classList.remove("active"));
    b.classList.add("active");
    _libFilter=_tvFilter=b.getAttribute("data-f");
    _libPage=_tvPage=LIB_PAGE;
    if(MODE==="tv")renderSeries();else renderLib();
  };
});
</script>
</body></html>"""





class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self):
        if not TOKEN:
            return True
        h = self.headers.get("Authorization", "")
        return h == ("Bearer " + TOKEN) or h == ("token " + TOKEN)

    def _q(self):
        """返回 (path无查询串, 查询参数dict)。支持 ?kind=tv 之类模式参数。"""
        if "?" in self.path:
            base, qs = self.path.split("?", 1)
            return base.rstrip("/"), parse_qs(qs)
        return self.path.rstrip("/"), {}

    def _kind(self):
        return (self._q()[1].get("kind") or ["movie"])[0]

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception as e:
            return {"__error__": str(e)}

    def do_GET(self):
        if not self._auth_ok():
            self._send(401, {"error": "unauthorized"}); return
        if self.path.startswith("/favicon.ico"):
            self.send_response(204); self.end_headers(); return
        if self.path == "/" or self.path.startswith("/?"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            b = PAGE.encode("utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b); return
        if self.path.startswith("/api/"):
            try:
                base, _qs = self._q()
                kind = (_qs.get("kind") or ["movie"])[0]
                if base.startswith("/api/queue"):
                    if kind == "tv":
                        q = series_queue_status()
                    elif kind == "all":
                        q = all_queue_status()
                    else:
                        q = queue_status()
                    total = sum((it.get("speed") or 0) for it in q)
                    self._send(200, {"queue": q, "totalSpeed": total})
                elif base.startswith("/api/profiles"):
                    src = get_series_profiles() if kind == "tv" else get_profiles()
                    self._send(200, {"profiles": [{"id": p["id"], "name": p.get("name")} for p in src]})
                elif base.startswith("/api/movies"):
                    self._send(200, {"movies": list_movies()})
                elif base.startswith("/api/series"):
                    self._send(200, {"series": list_series()})
                elif base.startswith("/api/system"):
                    self._send(200, system_status())
                elif base.startswith("/api/history"):
                    hkind = (_qs.get("kind") or ["all"])[0]
                    self._send(200, {"history": recent_history(hkind)})
                elif base.startswith("/api/indexers"):
                    self._send(200, indexer_health())
                elif base.startswith("/api/rootfolders"):
                    rkind = (_qs.get("kind") or ["movie"])[0]
                    self._send(200, {"rootfolders": list_rootfolders(rkind)})
                elif base.startswith("/api/webhook"):
                    self._send(200, {"configured": bool(WEBHOOK_URL), "url": WEBHOOK_URL,
                                    "sent": _WEBHOOK_SENT, "last": _WEBHOOK_LAST})
                elif base == "/api/config":
                    self._send(200, config_get()); return
                elif base.startswith("/api/calendar"):
                    cs = (_qs.get("start") or [""])[0]
                    ce = (_qs.get("end") or [""])[0]
                    if not cs or not ce:
                        self._send(400, {"error": "缺少 start/end (YYYY-MM-DD)"})
                    else:
                        self._send(200, {"events": calendar_events(cs, ce)})
                elif base == "/api/discover":
                    dkind = (_qs.get("kind") or ["movie"])[0]
                    dcat = (_qs.get("cat") or ["popular"])[0]
                    dpage = (_qs.get("page") or ["1"])[0]
                    dgenre = (_qs.get("genre") or [""])[0]
                    dcountry = (_qs.get("country") or [""])[0]
                    ddecade = (_qs.get("decade") or [""])[0]
                    drating = (_qs.get("rating") or [""])[0]
                    druntime = (_qs.get("runtime") or [""])[0]
                    dsort = (_qs.get("sort") or [""])[0]
                    drefresh = (_qs.get("refresh") or [""])[0] == "1"
                    ckey = ("disc", dkind, dcat, dpage, dgenre, dcountry, ddecade, drating, druntime, dsort)
                    if drefresh:
                        _tmdb_cache_invalidate(ckey)
                    elif not drefresh:
                        hit = _tmdb_cache_get(ckey)
                        if hit is not None:
                            _it = hit.get("items") or []
                            if _it:
                                threading.Thread(target=_prefetch_details, args=(_it,), daemon=True).start()
                            self._send(200, hit); return
                    res = tmdb_discover(dkind, dcat, dpage, dgenre or None,
                                        dcountry or None, ddecade or None,
                                        drating or None, druntime or None,
                                        dsort or None)
                    if res.get("ok") and res.get("items"):
                        _tmdb_cache_put(ckey, res)
                        _it = res.get("items") or []
                        if _it:
                            threading.Thread(target=_prefetch_details, args=(_it,), daemon=True).start()
                    self._send(200, res)
                elif base == "/api/detail":
                    dkind = (_qs.get("kind") or ["movie"])[0]
                    dtid = (_qs.get("tmdbId") or [""])[0]
                    drefresh = (_qs.get("refresh") or [""])[0] == "1"
                    ckey = ("detail", dkind, dtid)
                    if drefresh:
                        _tmdb_cache_invalidate(ckey)
                    elif not drefresh:
                        hit = _tmdb_cache_get(ckey)
                        if hit is not None:
                            self._send(200, hit); return
                    res = tmdb_detail(dkind, dtid)
                    if res.get("ok"):
                        _tmdb_cache_put(ckey, res)
                    self._send(200, res)
                else:
                    self._send(404, {"error": "not found"})
            except Exception as e:
                self._send(500, {"error": str(e)})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth_ok():
            self._send(401, {"error": "unauthorized"}); return
        p = self.path.rstrip("/")
        if p == "/api/config":
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except Exception:
                length = 0
            raw = self.rfile.read(length).decode("utf-8", "replace") if length else "{}"
            code, obj = config_save(raw)
            self._send(code, obj); return
        if p == "/api/movie":
            d = self._read_json()
            if "__error__" in d:
                self._send(400, {"ok": False, "error": "请求体解析失败: " + d["__error__"]}); return
            name = (d.get("name") or "").strip()
            tmdb = d.get("tmdbId")
            imdb = d.get("imdbId")
            if not name and not tmdb and not imdb:
                self._send(400, {"ok": False, "error": "缺少 name / tmdbId / imdbId"}); return
            try:
                res = add_movie(name=name, tmdb_id=tmdb, imdb_id=d.get("imdbId"),
                                profile=d.get("profile"),
                                root_folder=d.get("rootFolderPath"),
                                dry_run=bool(d.get("dryRun")))
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)}); return
            self._send(200 if res.get("ok") else 400, res); return
        if p == "/api/webhook/test":
            if not WEBHOOK_URL:
                self._send(400, {"ok": False,
                                "error": "未配置 AUTOPILOT_WEBHOOK_URL；请在 compose 里加上该环境变量后再试。"}); return
            ok = _fire_webhook({"event": "test", "title": "影视下载台 测试通知",
                               "message": "如果你收到这条消息，说明 webhook 配置正确"})
            self._send(200, {"ok": ok}); return
        if p == "/api/search":
            d = self._read_json()
            term = (d.get("term") or "").strip()
            kind = (d.get("kind") or "all").strip()
            if not term:
                self._send(400, {"ok": False, "error": "缺少 term"}); return
            try:
                if kind == "tv":
                    res = search_series_candidates(term)
                elif kind == "movie":
                    res = search_candidates(term)
                else:
                    res = search_all(term)
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)}); return
            self._send(200 if res.get("ok") else 400, res); return
        if p == "/api/series":
            d = self._read_json()
            if "__error__" in d:
                self._send(400, {"ok": False, "error": "请求体解析失败: " + d["__error__"]}); return
            name = (d.get("name") or "").strip()
            tvdb = d.get("tvdbId")
            if not name and not tvdb:
                self._send(400, {"ok": False, "error": "缺少 name 或 tvdbId"}); return
            season_mode = str(d.get("seasonMode") or "all").strip()
            if season_mode not in ("all", "latest", "first"):
                season_mode = "all"
            try:
                res = add_series(name=name, tvdb_id=tvdb, profile=d.get("profile"),
                                 season_mode=season_mode,
                                 root_folder=d.get("rootFolderPath"))
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)}); return
            self._send(200 if res.get("ok") else 400, res); return
        if p == "/api/series/bulk":
            d = self._read_json()
            names = [str(x).strip() for x in (d.get("names") or []) if str(x).strip()]
            season_mode = str(d.get("seasonMode") or "all").strip()
            if season_mode not in ("all", "latest", "first"):
                season_mode = "all"
            results = []
            for n in names:
                try:
                    results.append(add_series(n, None, d.get("profile"), season_mode))
                except Exception as e:
                    results.append({"ok": False, "name": n, "error": str(e)})
            self._send(200, {"results": results}); return
        if p.startswith("/api/series/") and p.endswith("/search"):
            try:
                sid = int(p.rsplit("/", 2)[1])
                s_req("POST", "/api/v3/command", {"name": "SeriesSearch", "seriesIds": [sid]})
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)}); return
            self._send(200, {"ok": True}); return
        if p == "/api/movies/bulk":
            d = self._read_json()
            names = [str(x).strip() for x in (d.get("names") or []) if str(x).strip()]
            results = []
            for n in names:
                try:
                    results.append(add_movie(n, None, d.get("profile")))
                except Exception as e:
                    results.append({"ok": False, "name": n, "error": str(e)})
            self._send(200, {"results": results}); return
        if p.startswith("/api/movie/") and p.endswith("/search"):
            try:
                mid = int(p.rsplit("/", 2)[1])
                r_req("POST", "/api/v3/command", {"name": "MoviesSearch", "movieIds": [mid]})
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)}); return
            self._send(200, {"ok": True}); return
        if p.startswith("/api/indexers/") and p.endswith("/enable"):
            name = p.rsplit("/", 2)[1]
            if not name:
                self._send(400, {"ok": False, "error": "缺少索引器名称"}); return
            ok, msg = enable_indexer(name)
            self._send(200, {"ok": ok, "message": msg} if ok else {"ok": False, "error": msg}); return
        if p == "/api/discover/add":
            d = self._read_json()
            if "__error__" in d:
                self._send(400, {"ok": False, "error": "请求体解析失败: " + d["__error__"]}); return
            dkind = (d.get("kind") or "movie").strip()
            tmdb = d.get("tmdbId")
            if not tmdb:
                self._send(400, {"ok": False, "error": "缺少 tmdbId"}); return
            season_mode = str(d.get("seasonMode") or "all").strip()
            if season_mode not in ("all", "latest", "first"):
                season_mode = "all"
            try:
                res = discover_add(kind=dkind, tmdb_id=tmdb, profile=d.get("profile"),
                                   root_folder=d.get("rootFolderPath"), season_mode=season_mode)
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)}); return
            self._send(200 if res.get("ok") else 400, res); return
        self._send(404, {"error": "not found"})

    def do_DELETE(self):
        if not self._auth_ok():
            self._send(401, {"error": "unauthorized"}); return
        p, qs = self._q()
        kind = (qs.get("kind") or ["movie"])[0]
        if p.startswith("/api/queue/"):
            try:
                qid = int(p.rsplit("/", 1)[1])
                res = remove_from_series_queue(qid) if kind == "tv" else remove_from_queue(qid)
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)}); return
            self._send(200, res); return
        if p.startswith("/api/series/"):
            try:
                sid = int(p.rsplit("/", 1)[1])
                res = remove_series(sid)
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)}); return
            self._send(200, res); return
        if p.startswith("/api/movie/"):
            try:
                mid = int(p.rsplit("/", 1)[1])
                res = remove_movie(mid)
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)}); return
            self._send(200, res); return
        self._send(404, {"error": "not found"})


def main():
    if WEBHOOK_URL:
        threading.Thread(target=webhook_watcher, args=(30,), daemon=True).start()
        print("[autopilot] webhook watcher -> %s" % WEBHOOK_URL)
    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), H)
    print("[autopilot] listening on :%d  RADARR_URL=%s" % (LISTEN_PORT, RADARR_URL))
    server.serve_forever()


if __name__ == "__main__":
    main()
