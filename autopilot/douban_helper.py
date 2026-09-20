"""豆瓣数据源：热榜 / Top250 / 搜索 / 评分。零 MoviePilot 依赖。

参考 MoviePilot DoubanApi 的 URL 构造与解析思路（j/search_subjects、j/chart_top_list），
自写实现，不 import 任何 MoviePilot 代码。

设计取舍：
- 卡片结构对齐 autopilot 现有 _norm_item，但 tmdbId 留空（None）。
  因为「添加下载 / 详情」环节才需要 TMDB id，届时由 autopilot 后端按片名经 TMDB 搜索解析，
  既避免列表接口为每个条目阻塞发 20 次 TMDB 请求（慢），又复用现有 Radarr/Sonarr 下载链。
- 豆瓣本身走 HTTP（经代理），公开榜单免登录态；限流/403 时返回可懂错误，由前端降级。
"""
import json
import time
import urllib.request as _ureq
import urllib.error
from urllib.parse import quote as _quote
from concurrent.futures import ThreadPoolExecutor

# ---- 由 autopilot 在启动时注入（configure）----
_PROXY_URL = None
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 豆瓣电影分类 -> tag
_MOVIE_TAGS = {
    "popular": "热门",
    "top_rated": "豆瓣高分",
    "latest": "最新",
    "cn": "华语",
}
_TV_TAGS = {
    "tv_pop": "热门",
    "tv_top": "豆瓣高分",
}

# 发现页筛选 -> 豆瓣 explore（j/new_search_subjects）参数映射
# TMDB 类型 id -> 豆瓣类型标签（豆瓣用中文标签，个别名称需归一，如 纪录 -> 纪录片）
_TMDB_GENRE_ZH = {
    "28": "动作", "12": "冒险", "16": "动画", "35": "喜剧", "80": "犯罪",
    "99": "纪录片", "18": "剧情", "10751": "家庭", "14": "奇幻", "36": "历史",
    "27": "恐怖", "10402": "音乐", "9648": "悬疑", "10749": "爱情", "878": "科幻",
    "53": "惊悚", "10752": "战争", "37": "西部",
    # TV 类型（豆瓣标签较粗，做近似映射，未知则忽略）
    "10759": "动作", "10765": "科幻", "10768": "战争", "10762": "儿童",
}
# 国别代码 -> 豆瓣国别标签
_COUNTRY_ZH = {
    "US": "美国", "CN": "中国大陆", "HK": "中国香港", "TW": "中国台湾",
    "JP": "日本", "KR": "韩国", "GB": "英国", "FR": "法国", "DE": "德国",
    "IT": "意大利", "ES": "西班牙", "IN": "印度", "TH": "泰国", "RU": "俄罗斯",
    "CA": "加拿大", "AU": "澳大利亚", "BR": "巴西", "MX": "墨西哥", "KP": "朝鲜",
    "VN": "越南", "PH": "菲律宾",
}
# 前端排序值 -> 豆瓣 sort 码（U 综合 / S 评分 / T 时间）
_DOUBAN_SORT = {"pop": "U", "rating": "S", "date": "T"}


def configure(proxy_url=None):
    """注入代理（豆瓣为国内服务，可传 None 走直连，或传 squid/Clash 地址）。"""
    global _PROXY_URL
    _PROXY_URL = proxy_url


def _douban_get(url, referer):
    hdrs = {
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": referer,
    }
    handlers = []
    if _PROXY_URL:
        handlers.append(_ureq.ProxyHandler({"https": _PROXY_URL, "http": _PROXY_URL}))
    opener = _ureq.build_opener(*handlers)
    req = _ureq.Request(url, headers=hdrs)
    last = ""
    for attempt in range(1, 4):
        try:
            with opener.open(req, timeout=20) as r:
                raw = r.read().decode("utf-8")
                return json.loads(raw), None
        except urllib.error.HTTPError as ex:
            if ex.code in (403, 429):
                return None, "豆瓣拒绝访问（HTTP %s）：可能被限流或需登录态，稍后重试" % ex.code
            return None, "豆瓣 HTTP %s: %s" % (ex.code, ex.reason)
        except Exception as e:
            last = str(e)
            if attempt < 3:
                time.sleep(0.6)
    return None, "豆瓣请求失败（已重试 3 次）：%s" % last


def _fetch_list(kind, cat, page):
    """返回 (subjects, err)。subjects 为豆瓣原始条目列表（含 title/cover/rate/id）。"""
    try:
        page = max(1, int(page or 1))
    except (ValueError, TypeError):
        page = 1
    start = (page - 1) * 20

    if kind == "tv":
        tag = _TV_TAGS.get(cat, "热门")
        dkind = "tv"
    else:
        tag = _MOVIE_TAGS.get(cat, "热门")
        dkind = "movie"

    # Top250 用排行榜接口（返回 JSON 数组，非 {subjects}）
    if cat == "top_rated" and dkind == "movie":
        url = ("https://movie.douban.com/j/chart_top_list?type=movie"
               "&interval_id=100:90&action=&start=%d&limit=20" % start)
        data, err = _douban_get(url, "https://movie.douban.com/top250")
        if err:
            return None, err
        subjects = data if isinstance(data, list) else []
        return subjects, None

    url = ("https://movie.douban.com/j/search_subjects?type=%s&tag=%s"
           "&sort=recommend&page_limit=20&page_start=%d" % (dkind, _quote(tag), start))
    data, err = _douban_get(url, "https://movie.douban.com/")
    if err:
        return None, err
    subjects = (data.get("subjects") if isinstance(data, dict) else None) or []
    return subjects, None


def _fetch_search(kind, genre, country, rating, sort, page):
    """带筛选的探索：j/new_search_subjects（tags=类型/国别 + range=评分 + sort=排序）。
    返回 (subjects, err)。subjects 字段与榜单接口不同：data[].title/rate/cover/id。"""
    try:
        page = max(1, int(page or 1))
    except (ValueError, TypeError):
        page = 1
    start = (page - 1) * 20
    tags = ["电视剧" if kind == "tv" else "电影"]
    for gid in (genre or "").split(","):
        gid = gid.strip()
        if gid and _TMDB_GENRE_ZH.get(gid):
            tags.append(_TMDB_GENRE_ZH[gid])
    if country and _COUNTRY_ZH.get(country):
        tags.append(_COUNTRY_ZH[country])
    tagstr = ",".join(tags)
    rng = ("%s,10" % rating) if rating else "0,10"
    s = _DOUBAN_SORT.get(sort or "pop", "U")
    url = ("https://movie.douban.com/j/new_search_subjects?sort=%s&range=%s&tags=%s&start=%d&limit=20"
           % (s, rng, _quote(tagstr), start))
    data, err = _douban_get(url, "https://movie.douban.com/explore")
    if err:
        return None, err
    subs = (data.get("data") if isinstance(data, dict) else None) or []
    return subs, None


def get_douban(kind="movie", cat="popular", page=1,
               genre=None, country=None, rating=None, sort=None):
    # 豆瓣只有「电影 / 剧集」两类，没有混合榜：非 tv 一律按电影抓。
    # item.kind 必须回填实际抓取类型，不能沿用请求里的 "all"（否则下游按片名
    # 解析 tmdbId 时会走错 /search/movie|tv 分支，且添加时 kind 非法）。
    eff_kind = "tv" if kind == "tv" else "movie"
    has_filter = bool(genre or country or rating or (sort and sort != "pop"))
    if has_filter:
        subjects, err = _fetch_search(kind, genre, country, rating, sort, page)
    else:
        subjects, err = _fetch_list(kind, cat, page)
    if err:
        return {"ok": False, "configured": True, "source": "douban", "error": err}
    items = []
    for s in subjects[:20]:
        title = (s.get("title") or "").strip()
        rate = s.get("rate") or ""
        try:
            rating = round(float(rate), 1) if rate not in ("", None) else None
        except (ValueError, TypeError):
            rating = None
        cover = s.get("cover") or (s.get("pic") or {}).get("large") or ""
        items.append({
            "tmdbId": None,
            "kind": eff_kind,
            "title": title,
            "year": None,
            "overview": "",
            "rating": rating,
            "poster": cover or None,
            "source": "douban",
            "doubanId": s.get("id"),
        })
    return {"ok": True, "configured": True, "source": "douban", "kind": eff_kind, "cat": cat,
            "page": page, "totalPages": 9999, "totalResults": None, "items": items}
