"""字幕数据源：SubtitleCat（中文站，免 key，已验证直链闭环）。零 MoviePilot 依赖。

参考 MoviePilot subtitle 模块的功能定位（按片名/季集匹配字幕），自写实现，不 import 任何 MP 代码。

实测结论（2026-09-20 经 NAS 代理出口诊断）：
- OpenSubtitles 旧 XML API（rest.opensubtitles.org）已废弃 → 502/000，弃用。
- OpenSubtitles v1 JSON API（api.opensubtitles.com）强制 Api-Key，无 key 不可用（留作后续可选增强）。
- Zimuku / Assrt 经代理不可达或需 token。
- **SubtitleCat（subtitlecat.com）经代理可达（200），且 .srt 静态直链可下载** → 作为主源。

闭环：列表页 `index.php?searchin=1&searchword=Q` 抓字幕项 `subs/{id}/{name}.html`
  → 详情页解析 JS `download_sub('filename.srt','/subs/{id}/')`
  → 构造直链 `https://subtitlecat.com/subs/{id}/filename.srt` 直接下载。

设计取舍（MVP）：
- 后端匹配候选 -> 浏览器直接下载 .srt 闭环；不写 /data 媒体库、不改 compose 挂载。
- 默认走代理出网（configure 注入 PROXY_URL）；站点结构易变，解析失败即降级返回空。
"""
import json
import time
import re as _re
import urllib.request as _ureq
import urllib.error
import urllib.parse
from urllib.parse import quote as _quote

# ---- 由 autopilot 在启动时注入（configure）----
_PROXY_URL = None
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 下载域名白名单（防 SSRF：只允许已知字幕站）
_ALLOW_HOSTS = {"subtitlecat.com", "www.subtitlecat.com",
                "api.opensubtitles.com", "rest.opensubtitles.org"}

_SITE = "https://subtitlecat.com"


def configure(proxy_url=None):
    """注入代理（字幕站走代理出网，规避区域限制）。"""
    global _PROXY_URL
    _PROXY_URL = proxy_url


def _host_ok(url):
    try:
        h = urllib.parse.urlparse(url).hostname or ""
        return h.lower() in _ALLOW_HOSTS
    except Exception:
        return False


def _http_get(url, ua=None, timeout=10, binary=False, retries=2):
    hdrs = {"User-Agent": ua or _UA, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"}
    handlers = []
    if _PROXY_URL:
        handlers.append(_ureq.ProxyHandler({"https": _PROXY_URL, "http": _PROXY_URL}))
    opener = _ureq.build_opener(*handlers)
    req = _ureq.Request(url, headers=hdrs)
    last = ""
    for attempt in range(1, retries + 1):
        try:
            with opener.open(req, timeout=timeout) as r:
                data = r.read() if binary else r.read()
                return data, None
        except urllib.error.HTTPError as ex:
            if ex.code in (403, 429):
                return None, "字幕源拒绝访问（HTTP %s），可能被限流，稍后重试" % ex.code
            return None, "字幕源 HTTP %s: %s" % (ex.code, ex.reason)
        except Exception as e:
            last = str(e)
            if attempt < retries:
                time.sleep(0.6)
    return None, "字幕源请求失败（重试 %d 次）：%s" % (retries, last)


def guess_lang(name):
    n = (name or "").lower()
    if any(k in n for k in ("chi", "chs", "cht", "chinese", "中文", "简体", "繁体", "国字")):
        return "zh"
    if any(k in n for k in ("eng", "english", "orig", ".en.")):
        return "en"
    if any(k in n for k in ("kor", "korean")):
        return "ko"
    if any(k in n for k in ("jpn", "japanese")):
        return "ja"
    return ""


def search_subtitlecat(query, limit=8, budget=15):
    """列表页 -> 每项详情页解析 download_sub -> 扁平候选（含直链）。返回 (items, err)。
    budget: 整体耗时预算（秒），到时即停止继续拉详情页（返回已拿到的部分结果，防慢源卡死请求）。"""
    deadline = time.monotonic() + budget
    url = "%s/index.php?searchin=1&searchword=%s" % (_SITE, _quote(query))
    raw, err = _http_get(url)
    if err:
        return [], err
    text = raw.decode("utf-8", "ignore")
    # 字幕项：subs/{id}/{name}.html
    items_meta = []
    for m in _re.finditer(r'href="(subs/(\d+)/([^"]+\.html))"', text):
        items_meta.append((m.group(2), m.group(3)))
    items_meta = items_meta[:limit]
    items = []
    for sub_id, slug in items_meta:
        if time.monotonic() >= deadline:
            break
        detail_url = "%s/subs/%s/%s" % (_SITE, sub_id, slug)
        draw, derr = _http_get(detail_url)
        if derr or not draw:
            continue
        dtext = draw.decode("utf-8", "ignore")
        # _server_folder('af', 'filename.srt', '/subs/{id}/')  （函数名非 download_sub）
        for dm in _re.finditer(r"_server_folder\(\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*'([^']+)'\s*\)", dtext):
            lang_code = dm.group(1)
            fname = dm.group(2)
            ddir = dm.group(3)
            direct = "%s%s%s" % (_SITE, ddir, fname)
            items.append({
                "source": "subtitlecat",
                "title": fname,
                "lang": guess_lang(fname) or lang_code,
                "fmt": "srt",
                "url": direct,
            })
        time.sleep(0.2)
    # 去重：同一详情页多语言版本可能共用 filename -> 同 url 重复
    seen, uniq = set(), []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        uniq.append(it)
    return uniq, None


def search_subtitles(name, tmdbId=None, season=None, episode=None, lang="zh"):
    """统一入口。返回 {ok, items|error, warns}。"""
    q = name or ""
    if season and episode:
        q = "%s S%02dE%02d" % (name, int(season), int(episode))
    elif season:
        q = "%s Season %d" % (name, int(season))
    items, err = search_subtitlecat(q)
    if err:
        return {"ok": False, "error": "字幕源不可用：%s" % err, "warns": [err], "items": []}
    if not items:
        return {"ok": False, "error": "未找到字幕（SubtitleCat 无匹配结果）",
                "warns": ["SubtitleCat 无结果，可换片名/季集重试"], "items": []}
    return {"ok": True, "items": items, "warns": []}


def download_subtitle(url, source):
    """下载字幕字节流。返回 (bytes, filename, ctype, err)。"""
    if not _host_ok(url):
        return None, None, None, "拒绝下载：域名不在字幕源白名单（防 SSRF）"
    # 安全编码路径中的特殊字符（空格、中文等），避免 urllib 对裸 URL 报错
    try:
        _p = urllib.parse.urlparse(url)
        url = urllib.parse.urlunparse((_p.scheme, _p.netloc,
                                       urllib.parse.quote(_p.path), _p.params, _p.query, _p.fragment))
    except Exception:
        pass
    raw, err = _http_get(url, timeout=30, binary=True)
    if err:
        return None, None, None, err
    if not raw:
        return None, None, None, "字幕下载为空"
    fname = "subtitle.srt"
    try:
        disp = ""
        # 尝试从 url 文件名取
        base = url.rsplit("/", 1)[-1]
        if base and "." in base:
            fname = base
    except Exception:
        pass
    ctype = "application/x-subrip"
    if fname.lower().endswith(".ass"):
        ctype = "text/plain"
    return raw, fname, ctype, None
