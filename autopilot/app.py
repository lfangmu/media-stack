#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
影视下载台 · 媒体栈统一控制台
Tab1 出网配置：填一个 HTTP 代理地址即可出外网（写入 .env 并重启 squid）
Tab2 发现：按 类型/类型/国家/年代/评分/时长 筛选 TMDB，搜索，一键添加到 Radarr/Sonarr
Tab3 状态：各服务可达性
"""
import os, json, subprocess, urllib.parse, urllib.request, socket, http.cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("AUTOPILOT_PORT", "8787"))
TOKEN = os.environ.get("AUTOPILOT_TOKEN", "")
EGRESS = os.environ.get("EGRESS_PROXY", "http://proxy-forwarder:3128")
ENV_PATH = os.environ.get("MEDIA_ENV", "/app/.env")
QBIT_URL = os.environ.get("QBITTORRENT_URL", "http://media-qbittorrent:8085").rstrip("/")
QBIT_USER = os.environ.get("QBITTORRENT_USER", "admin")
QBIT_PASS = os.environ.get("QBITTORRENT_PASS", "adminadmin")

SERVICES = {
    "影视控制台(本服务)": ("127.0.0.1", PORT),
    "proxy-forwarder": ("proxy-forwarder", 3128),
    "flaresolverr": ("media-flaresolverr", 8191),
    "prowlarr": ("media-prowlarr", 9696),
    "radarr": ("media-radarr", 7878),
    "sonarr": ("media-sonarr", 8989),
    "qbittorrent": ("media-qbittorrent", 8085),
}

TMDB_BASE = "https://api.themoviedb.org/3"
IMG_BASE = "https://image.tmdb.org/t/p/w342"

LIB_KEYS = ["RADARR_URL", "RADARR_API_KEY", "SONARR_URL", "SONARR_API_KEY", "TMDB_API_KEY"]


# ---------- .env helpers ----------
def read_env():
    d = {}
    try:
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return d


def write_env(updates):
    d = read_env()
    d.update(updates)
    with open(ENV_PATH, "w") as f:
        for k, v in d.items():
            f.write(f"{k}={v}\n")


def current_proxy_url():
    d = read_env()
    h, p, a = d.get("UPSTREAM_PROXY_HOST", ""), d.get("UPSTREAM_PROXY_PORT", ""), d.get("UPSTREAM_PROXY_AUTH", "")
    if not h or not p:
        return ""
    return f"http://{a + '@' if a else ''}{h}:{p}"


def parse_proxy_url(url):
    try:
        u = urllib.parse.urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname or not u.port:
            return None
        auth = f"{u.username}:{u.password or ''}" if u.username else ""
        return {"UPSTREAM_PROXY_HOST": u.hostname,
                "UPSTREAM_PROXY_PORT": str(u.port),
                "UPSTREAM_PROXY_AUTH": auth}
    except Exception:
        return None


# ---------- outbound helpers ----------
def req(url, params=None, headers=None, method="GET", data=None, timeout=15):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    r = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    # 注意：urllib.request.urlopen 不接受 proxies= 参数（那是 urllib2 旧式写法），
    # 必须用 ProxyHandler + build_opener 指定出口，否则任何请求都会 TypeError。
    if EGRESS:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": EGRESS, "https": EGRESS}))
        return opener.open(r, timeout=timeout)
    return urllib.request.urlopen(r, timeout=timeout)


def tmdb_get(path, params):
    d = read_env()
    key = d.get("TMDB_API_KEY", "")
    if not key:
        return None, "未配置 TMDB_API_KEY（在「出网配置→媒体库设置」里填）"
    params = dict(params or {})
    params["api_key"] = key
    try:
        with req(TMDB_BASE + path, params) as r:
            return json.loads(r.read().decode("utf-8")), None
    except Exception as e:
        msg = str(e)
        # 网络层失败（无外网 / 代理不可用）=> 友好提示，而非原始异常；
        # 这样「没境外网络」只表现为「无数据 + 一行提示」，配好出口刷新即可。
        if any(k in msg for k in ("urlopen error", "Connection", "timed out",
                                  "Name or service", "Network is unreachable", "getaddrinfo", "<urlopen error")):
            return None, "外网未连通：请在「出网配置」填写境外 HTTP 代理后刷新本页"
        return None, f"TMDB 请求失败：{e}"


def arr_request(method, url, api_key, data=None):
    headers = {"X-Api-Key": api_key, "Content-Type": "application/json", "Accept": "application/json"}
    body = json.dumps(data).encode() if data is not None else None
    with req(url, headers=headers, method=method, data=body) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------- actions ----------
def restart_squid():
    subprocess.run(["docker", "restart", "proxy-forwarder"], check=True, capture_output=True, timeout=90)


def egress_ok():
    try:
        with req("http://www.gstatic.com/generate_204", timeout=10) as r:
            return r.status == 204
    except Exception:
        return False


def tcp_ok(host, port):
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except Exception:
        return False


def qb_set_proxy():
    try:
        data = urllib.parse.urlencode({"username": QBIT_USER, "password": QBIT_PASS}).encode()
        resp = urllib.request.urlopen(urllib.request.Request(QBIT_URL + "/api/v2/auth/login", data=data, method="POST"), timeout=10)
        jar = http.cookies.SimpleCookie(resp.headers.get("Set-Cookie", ""))
        sidv = jar.get("SID", "").value if "SID" in jar else ""
        if not sidv:
            return False, "qB 登录失败（检查 QBITTORRENT_USER/PASS）"
        headers = {"Cookie": f"SID={sidv}"}
        prefs = {"proxyType": "http", "proxyHost": "proxy-forwarder", "proxyPort": 3128,
                 "proxyUsername": "", "proxyPassword": "", "proxyTorrentsOnly": False}
        body = json.dumps({"json": json.dumps(prefs)}).encode()
        urllib.request.urlopen(urllib.request.Request(QBIT_URL + "/api/v2/app/setPreferences", data=body, headers=headers, method="POST"), timeout=10)
        return True, "qB 代理已指向出口 (proxy-forwarder:3128)"
    except Exception as e:
        return False, f"qB 代理配置失败：{e}"


# ---------- discover ----------
def tmdb_discover(t, genre, country, decade, min_rating, max_runtime, page):
    params = {"sort_by": "popularity.desc", "page": page or 1, "language": "zh-CN"}
    if genre:
        params["with_genres"] = genre
    if country:
        params["with_origin_country"] = country
    if decade:
        params["primary_release_date.gte"] = f"{decade}-01-01"
        params["primary_release_date.lte"] = f"{int(decade)+9}-12-31"
    if min_rating:
        params["vote_average.gte"] = min_rating
    if max_runtime and t == "movie":
        params["with_runtime.lte"] = max_runtime
    path = "/discover/movie" if t == "movie" else "/discover/tv"
    if t == "tv":
        # tv 用首播年份
        if decade:
            params.pop("primary_release_date.gte", None); params.pop("primary_release_date.lte", None)
            params["first_air_date.gte"] = f"{decade}-01-01"
            params["first_air_date.lte"] = f"{int(decade)+9}-12-31"
        params.pop("with_runtime.lte", None)
    data, err = tmdb_get(path, params)
    if err:
        return None, err
    out = []
    for it in data.get("results", []):
        title = it.get("title") or it.get("name") or ""
        date = it.get("release_date") or it.get("first_air_date") or ""
        out.append({
            "tmdbId": it.get("id"), "type": t, "title": title,
            "year": date[:4], "rating": it.get("vote_average"),
            "overview": (it.get("overview") or "")[:220],
            "poster": it.get("poster_path") or "",
        })
    return out, None


def tmdb_search(q, page):
    data, err = tmdb_get("/search/multi", {"query": q, "page": page or 1, "language": "zh-CN"})
    if err:
        return None, err
    out = []
    for it in data.get("results", []):
        mt = it.get("media_type")
        if mt not in ("movie", "tv"):
            continue
        title = it.get("title") or it.get("name") or ""
        date = it.get("release_date") or it.get("first_air_date") or ""
        out.append({
            "tmdbId": it.get("id"), "type": mt, "title": title,
            "year": date[:4], "rating": it.get("vote_average"),
            "overview": (it.get("overview") or "")[:220],
            "poster": it.get("poster_path") or "",
        })
    return out, None


def arr_add(t, tmdb_id):
    d = read_env()
    if t == "movie":
        base, key = d.get("RADARR_URL", ""), d.get("RADARR_API_KEY", "")
        if not base or not key:
            return False, "未配置 Radarr 地址/API Key"
        try:
            movie = arr_request("GET", f"{base.rstrip('/')}/api/v3/movie/lookup/tmdb/{tmdb_id}", key)
            # 补全默认路径/画质（若 lookup 未带）
            if not movie.get("rootFolderPath"):
                rf = arr_request("GET", f"{base.rstrip('/')}/api/v3/rootfolder", key)
                if rf:
                    movie["rootFolderPath"] = rf[0]["path"]
            if not movie.get("qualityProfileId"):
                qp = arr_request("GET", f"{base.rstrip('/')}/api/v3/qualityprofile", key)
                if qp:
                    movie["qualityProfileId"] = qp[0]["id"]
            movie["minimumAvailability"] = movie.get("minimumAvailability", "released")
            movie["monitor"] = True
            arr_request("POST", f"{base.rstrip('/')}/api/v3/movie", key, movie)
            return True, f"已添加到 Radarr：{movie.get('title')}"
        except Exception as e:
            return False, f"添加到 Radarr 失败：{e}"
    else:
        base, key = d.get("SONARR_URL", ""), d.get("SONARR_API_KEY", "")
        if not base or not key:
            return False, "未配置 Sonarr 地址/API Key"
        try:
            show = arr_request("GET", f"{base.rstrip('/')}/api/v3/series/lookup/tmdb/{tmdb_id}", key)
            if not show.get("rootFolderPath"):
                rf = arr_request("GET", f"{base.rstrip('/')}/api/v3/rootfolder", key)
                if rf:
                    show["rootFolderPath"] = rf[0]["path"]
            if not show.get("qualityProfileId"):
                qp = arr_request("GET", f"{base.rstrip('/')}/api/v3/qualityprofile", key)
                if qp:
                    show["qualityProfileId"] = qp[0]["id"]
            arr_request("POST", f"{base.rstrip('/')}/api/v3/series", key, show)
            return True, f"已添加到 Sonarr：{show.get('title')}"
        except Exception as e:
            return False, f"添加到 Sonarr 失败：{e}"


# ---------- HTTP handler ----------
class H(BaseHTTPRequestHandler):
    def _auth(self):
        return True if not TOKEN else self.headers.get("X-Token", "") == TOKEN

    def _send(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _qs(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers()
            self.wfile.write(PAGE.encode("utf-8")); return
        if p == "/api/config":
            return self._send(200, {"proxy_url": current_proxy_url()})
        if p == "/api/library":
            d = read_env()
            return self._send(200, {k: bool(d.get(k)) for k in LIB_KEYS})
        if p == "/api/egress/test":
            return self._send(200, {"ok": egress_ok()})
        if p == "/api/status":
            return self._send(200, {k: tcp_ok(h, pp) for k, (h, pp) in SERVICES.items()})
        if p == "/api/discover":
            q = self._qs()
            out, err = tmdb_discover(q.get("type", ["movie"])[0], q.get("genre", [""])[0], q.get("country", [""])[0],
                                      q.get("decade", [""])[0], q.get("minRating", [""])[0], q.get("maxRuntime", [""])[0],
                                      q.get("page", ["1"])[0])
            return self._send(200, {"items": out or [], "error": err} if out is not None else {"items": [], "error": err})
        if p == "/api/search":
            q = self._qs()
            out, err = tmdb_search(q.get("q", [""])[0], q.get("page", ["1"])[0])
            return self._send(200, {"items": out or [], "error": err} if out is not None else {"items": [], "error": err})
        if p == "/img":
            q = self._qs()
            pth = q.get("p", [""])[0]
            if not pth:
                return self._send(404, {"error": "no path"})
            try:
                with req(IMG_BASE + pth, timeout=15) as r:
                    self.send_response(200); self.send_header("Content-Type", "image/jpeg"); self.end_headers()
                    self.wfile.write(r.read()); return
            except Exception as e:
                self._send(502, {"error": str(e)}); return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth():
            return self._send(401, {"error": "unauthorized"})
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except Exception:
            data = {}
        p = self.path.split("?")[0]
        if p == "/api/config":
            url = (data.get("proxy_url") or "").strip()
            if url:
                pv = parse_proxy_url(url)
                if not pv:
                    return self._send(400, {"error": "代理地址解析失败，需形如 http://host:port 或 http://user:pass@host:port"})
                write_env(pv)
            else:
                write_env({"UPSTREAM_PROXY_HOST": "", "UPSTREAM_PROXY_PORT": "", "UPSTREAM_PROXY_AUTH": ""})
            try:
                restart_squid()
            except Exception as e:
                return self._send(200, {"ok": True, "restart": "warn", "detail": str(e)})
            return self._send(200, {"ok": True, "proxy_url": current_proxy_url()})
        if p == "/api/library":
            updates = {}
            for k in LIB_KEYS:
                v = (data.get(k) or "").strip()
                if v:
                    updates[k] = v
            if updates:
                write_env(updates)
            return self._send(200, {"ok": True})
        if p == "/api/qb/proxy":
            ok, msg = qb_set_proxy()
            return self._send(200, {"ok": ok, "msg": msg})
        if p == "/api/add":
            ok, msg = arr_add(data.get("type", "movie"), data.get("tmdbId"))
            return self._send(200, {"ok": ok, "msg": msg})
        self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>影视下载台</title>
<style>
 body{font-family:system-ui,'Microsoft YaHei',sans-serif;margin:0;background:#0f1115;color:#e6e6e6}
 .wrap{max-width:980px;margin:0 auto;padding:20px}
 h1{font-size:22px;margin:0 0 4px}
 .sub{color:#8b93a1;font-size:13px;margin-bottom:14px}
 .tabs{display:flex;gap:8px;margin-bottom:14px}
 .tab{padding:8px 14px;border-radius:8px;background:#1a1d24;cursor:pointer;font-size:14px;border:1px solid #2a2f3a}
 .tab.on{background:#2f7d4f;color:#fff;border-color:#2f7d4f}
 .card{background:#1a1d24;border:1px solid #2a2f3a;border-radius:12px;padding:16px;margin-bottom:14px}
 .card h2{font-size:15px;margin:0 0 10px;color:#9fd0ff}
 input,select{width:100%;padding:9px;border-radius:8px;border:1px solid #2a2f3a;background:#0f1115;color:#e6e6e6;font-size:14px;box-sizing:border-box}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
 .row{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
 button{background:#2f7d4f;color:#fff;border:0;border-radius:8px;padding:9px 14px;font-size:14px;cursor:pointer}
 button.sec{background:#2a2f3a}
 .status{font-size:13px;margin-top:10px;color:#8b93a1}
 .ok{color:#5fd38a}.bad{color:#ff7a7a}
 table{width:100%;border-collapse:collapse;font-size:13px}
 td{padding:6px 4px;border-bottom:1px solid #232733}
 .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:middle}
 code{background:#0f1115;padding:1px 5px;border-radius:4px;color:#9fd0ff}
 .movie{background:#1a1d24;border:1px solid #2a2f3a;border-radius:10px;overflow:hidden}
 .movie img{width:100%;aspect-ratio:2/3;object-fit:cover;background:#0f1115;display:block}
 .movie .m{border-top:1px solid #232733;padding:8px}
 .movie .t{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .movie .m2{font-size:11px;color:#8b93a1;margin:2px 0 6px}
 .movie button{width:100%;padding:6px;font-size:12px}
 .hidden{display:none}
</style></head>
<body><div class="wrap">
<h1>影视下载台</h1>
<div class="sub">媒体栈统一控制台</div>
<div class="tabs">
 <div class="tab on" data-t="cfg" onclick="switchT('cfg')">出网配置</div>
 <div class="tab" data-t="disc" onclick="switchT('disc')">发现</div>
 <div class="tab" data-t="stat" onclick="switchT('stat')">状态</div>
</div>

<div id="cfg">
 <div class="card">
  <h2>境外外网访问（出网代理）</h2>
  <div>填入你的 HTTP 代理地址，例如 <code>http://user:pass@host:port</code>。留空 = 仅内网。</div>
  <input id="url" placeholder="http://host:port">
  <div class="row">
   <button onclick="save()">保存并重启出口</button>
   <button class="sec" onclick="test()">出网自检</button>
   <button class="sec" onclick="qb()">把 qB 代理指向出口</button>
  </div>
  <div class="status" id="msg"></div>
 </div>
 <div class="card">
  <h2>媒体库设置（发现 / 下载用，可选）</h2>
  <div class="grid" style="grid-template-columns:1fr 1fr">
   <input id="radarr_url" placeholder="Radarr 地址 http://media-radarr:7878">
   <input id="radarr_key" placeholder="Radarr API Key">
   <input id="sonarr_url" placeholder="Sonarr 地址 http://media-sonarr:8989">
   <input id="sonarr_key" placeholder="Sonarr API Key">
   <input id="tmdb_key" placeholder="TMDB API Key" style="grid-column:1/3">
  </div>
  <div class="row"><button class="sec" onclick="saveLib()">保存媒体库设置</button></div>
  <div class="status" id="msg2"></div>
 </div>
</div>

<div id="disc" class="hidden">
 <div class="card">
  <h2>发现墙</h2>
  <div class="grid" style="grid-template-columns:repeat(auto-fill,minmax(140px,1fr))">
   <select id="f_type"><option value="movie">电影</option><option value="tv">剧集</option></select>
   <select id="f_genre"><option value="">类型(不限)</option>
    <option value="28">动作</option><option value="35">喜剧</option><option value="18">剧情</option>
    <option value="878">科幻</option><option value="27">恐怖</option><option value="10749">爱情</option>
    <option value="16">动画</option><option value="80">犯罪</option><option value="53">惊悚</option>
    <option value="99">纪录片</option><option value="10752">战争</option><option value="9648">悬疑</option></select>
   <select id="f_country"><option value="">国家(不限)</option>
    <option value="US">美国</option><option value="CN">中国</option><option value="JP">日本</option>
    <option value="KR">韩国</option><option value="GB">英国</option><option value="FR">法国</option>
    <option value="DE">德国</option><option value="HK">香港</option><option value="TW">台湾</option></select>
   <select id="f_decade"><option value="">年代(不限)</option>
    <option value="2020">2020s</option><option value="2010">2010s</option>
    <option value="2000">2000s</option><option value="1990">1990s</option><option value="1980">1980s</option></select>
   <input id="f_rating" placeholder="最低评分 如 7">
   <input id="f_runtime" placeholder="最长时长(分) 电影">
  </div>
  <div class="row">
   <button onclick="discover()">按条件发现</button>
   <input id="f_q" placeholder="或直接搜片名" style="max-width:240px">
   <button class="sec" onclick="search()">搜索</button>
  </div>
  <div class="status" id="msg3"></div>
 </div>
 <div class="grid" id="results" style="margin-top:4px"></div>
</div>

<div id="stat" class="hidden">
 <div class="card">
  <h2>服务状态</h2>
  <table id="st"><tbody></tbody></table>
  <div class="row"><button class="sec" onclick="loadStatus()">刷新</button></div>
 </div>
</div>
</div>
<script>
function switchT(t){document.querySelectorAll('.tab').forEach(e=>e.classList.toggle('on',e.dataset.t===t));['cfg','disc','stat'].forEach(id=>document.getElementById(id).classList.toggle('hidden',id!==t));if(t==='stat')loadStatus();if(t==='disc')loadLib();}
async function api(p,body){const r=await fetch(p,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});return r.json();}
async function load(){const c=await api('/api/config');document.getElementById('url').value=c.proxy_url||'';}
async function save(){const url=document.getElementById('url').value.trim();const r=await api('/api/config',{proxy_url:url});document.getElementById('msg').innerHTML=r.ok?'<span class="ok">已保存，出口已重启</span>':('<span class="bad">'+(r.error||'失败')+'</span>');}
async function test(){const r=await api('/api/egress/test');document.getElementById('msg').innerHTML=r.ok?'<span class="ok">出网正常 (204)</span>':'<span class="bad">出网失败（检查代理地址）</span>';}
async function qb(){const r=await api('/api/qb/proxy');document.getElementById('msg').innerHTML=r.ok?'<span class="ok">'+r.msg+'</span>':'<span class="bad">'+(r.msg||'失败')+'</span>';}
async function loadLib(){const r=await api('/api/library');document.getElementById('radarr_url').placeholder=r.RADARR_URL?'Radarr 已配置':'Radarr 地址 http://media-radarr:7878';document.getElementById('radarr_key').placeholder=r.RADARR_API_KEY?'Radarr 已配置':'Radarr API Key';document.getElementById('sonarr_url').placeholder=r.SONARR_URL?'Sonarr 已配置':'Sonarr 地址 http://media-sonarr:8989';document.getElementById('sonarr_key').placeholder=r.SONARR_API_KEY?'Sonarr 已配置':'Sonarr API Key';document.getElementById('tmdb_key').placeholder=r.TMDB_API_KEY?'TMDB 已配置':'TMDB API Key';}
async function saveLib(){const d={};['radarr_url','radarr_key','sonarr_url','sonarr_key','tmdb_key'].forEach(id=>{const v=document.getElementById(id).value.trim();if(v)d[id.toUpperCase()]=v;});const r=await api('/api/library',d);document.getElementById('msg2').innerHTML=r.ok?'<span class="ok">已保存</span>':'<span class="bad">失败</span>';['radarr_url','radarr_key','sonarr_url','sonarr_key','tmdb_key'].forEach(id=>document.getElementById(id).value='');loadLib();}
function card(it){const img=it.poster?('/img?p='+encodeURIComponent(it.poster)):'';const addt=it.type==='movie'?'添加到电影库':'添加到剧集库';return '<div class="movie"><img src="'+img+'" loading="lazy" onerror="this.style.visibility=\'hidden\'"><div class="m"><div class="t">'+it.title+'</div><div class="m2">'+(it.year||'')+' · ★'+(it.rating||'-')+'</div><div class="m2" style="white-space:normal">'+((it.overview||'').slice(0,80))+'…</div><button onclick="add(\''+it.type+'\','+it.tmdbId+',this)">'+addt+'</button></div></div>';}
function render(items){document.getElementById('results').innerHTML=items.map(card).join('')||'<div class="status">无结果</div>';}
async function discover(){const q={type:f_type.value,genre:f_genre.value,country:f_country.value,decade:f_decade.value,minRating:f_rating.value,maxRuntime:f_runtime.value};const r=await api('/api/discover?'+new URLSearchParams(q).toString());if(r.error)return document.getElementById('msg3').innerHTML='<span class="bad">'+r.error+'</span>';render(r.items||[]);}
async function search(){const q=document.getElementById('f_q').value.trim();if(!q)return;const r=await api('/api/search?q='+encodeURIComponent(q));if(r.error)return document.getElementById('msg3').innerHTML='<span class="bad">'+r.error+'</span>';render(r.items||[]);}
async function add(t,id,btn){btn.disabled=true;const r=await api('/api/add',{type:t,tmdbId:id});btn.textContent=r.ok?('✓ '+r.msg.slice(0,12)):'失败';btn.style.background=r.ok?'#2f7d4f':'#a33';if(!r.ok)setTimeout(()=>{btn.disabled=false;btn.textContent=t==='movie'?'添加到电影库':'添加到剧集库';btn.style.background='';},2500);}
async function loadStatus(){const s=await api('/api/status');const tb=document.querySelector('#st tbody');tb.innerHTML='';for(const k in s){const v=s[k];tb.innerHTML+='<tr><td><span class="dot" style="background:'+(v?'#5fd38a':'#ff7a7a')+'"></span>'+k+'</td><td>'+(v?'可达':'不可达')+'</td></tr>';}}
load();loadLib();
</script></body></html>"""


if __name__ == "__main__":
    os.makedirs("/data/downloads", exist_ok=True)
    os.makedirs("/data/movies", exist_ok=True)
    os.makedirs("/data/tv", exist_ok=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
