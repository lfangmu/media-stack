#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
影视下载台 · 媒体栈统一控制台
- 单页 UI：配置境外外网访问（一个 HTTP 代理地址）
- 出网配置写入 .env，并重启 squid（proxy-forwarder）使其生效
- 提供 qB 代理一键指向出口、出网自检、服务状态
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


# ---------- actions ----------
def restart_squid():
    subprocess.run(["docker", "restart", "proxy-forwarder"], check=True, capture_output=True, timeout=90)


def egress_ok():
    try:
        req = urllib.request.Request("http://www.gstatic.com/generate_204")
        with urllib.request.urlopen(req, timeout=10, proxies={"http": EGRESS, "https": EGRESS}) as r:
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
        req = urllib.request.Request(QBIT_URL + "/api/v2/auth/login", data=data, method="POST")
        resp = urllib.request.urlopen(req, timeout=10)
        sid = resp.headers.get("Set-Cookie", "")
        jar = http.cookies.SimpleCookie(sid)
        sidv = jar.get("SID", "").value if "SID" in jar else ""
        if not sidv:
            return False, "qB 登录失败（检查 QBITTORRENT_USER/PASS）"
        headers = {"Cookie": f"SID={sidv}"}
        prefs = {"proxyType": "http", "proxyHost": "proxy-forwarder", "proxyPort": 3128,
                 "proxyUsername": "", "proxyPassword": "", "proxyTorrentsOnly": False}
        body = json.dumps({"json": json.dumps(prefs)}).encode()
        req2 = urllib.request.Request(QBIT_URL + "/api/v2/app/setPreferences", data=body, headers=headers, method="POST")
        urllib.request.urlopen(req2, timeout=10)
        return True, "qB 代理已指向出口 (proxy-forwarder:3128)"
    except Exception as e:
        return False, f"qB 代理配置失败：{e}"


# ---------- HTTP handler ----------
class H(BaseHTTPRequestHandler):
    def _auth(self):
        return True if not TOKEN else self.headers.get("X-Token", "") == TOKEN

    def _send(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(PAGE.encode("utf-8"))
            return
        if self.path == "/api/config":
            return self._send(200, {"proxy_url": current_proxy_url()})
        if self.path == "/api/egress/test":
            return self._send(200, {"ok": egress_ok()})
        if self.path == "/api/status":
            return self._send(200, {k: tcp_ok(h, p) for k, (h, p) in SERVICES.items()})
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
        if self.path == "/api/config":
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
        if self.path == "/api/qb/proxy":
            ok, msg = qb_set_proxy()
            return self._send(200, {"ok": ok, "msg": msg})
        self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>影视下载台</title>
<style>
 body{font-family:system-ui,'Microsoft YaHei',sans-serif;margin:0;background:#0f1115;color:#e6e6e6}
 .wrap{max-width:820px;margin:0 auto;padding:24px}
 h1{font-size:22px;margin:0 0 4px}
 .sub{color:#8b93a1;font-size:13px;margin-bottom:20px}
 .card{background:#1a1d24;border:1px solid #2a2f3a;border-radius:12px;padding:18px;margin-bottom:16px}
 .card h2{font-size:15px;margin:0 0 12px;color:#9fd0ff}
 input{width:100%;padding:10px;border-radius:8px;border:1px solid #2a2f3a;background:#0f1115;color:#e6e6e6;font-size:14px;box-sizing:border-box}
 .row{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
 button{background:#2f7d4f;color:#fff;border:0;border-radius:8px;padding:9px 14px;font-size:14px;cursor:pointer}
 button.sec{background:#2a2f3a}
 .status{font-size:13px;margin-top:10px;color:#8b93a1}
 .ok{color:#5fd38a}.bad{color:#ff7a7a}
 table{width:100%;border-collapse:collapse;font-size:13px}
 td{padding:6px 4px;border-bottom:1px solid #232733}
 .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:middle}
 code{background:#0f1115;padding:1px 5px;border-radius:4px;color:#9fd0ff}
</style></head>
<body><div class="wrap">
<h1>影视下载台</h1>
<div class="sub">媒体栈统一控制台 · 部署后在此配置境外外网访问</div>

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
 <h2>服务状态</h2>
 <table id="st"><tbody></tbody></table>
 <div class="row"><button class="sec" onclick="loadStatus()">刷新</button></div>
</div>
</div>
<script>
async function api(p,body){const r=await fetch(p,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});return r.json();}
async function load(){const c=await api('/api/config');document.getElementById('url').value=c.proxy_url||'';}
async function save(){const url=document.getElementById('url').value.trim();const r=await api('/api/config',{proxy_url:url});document.getElementById('msg').innerHTML=r.ok?'<span class="ok">已保存，出口已重启</span>':('<span class="bad">'+(r.error||'失败')+'</span>');}
async function test(){const r=await api('/api/egress/test');document.getElementById('msg').innerHTML=r.ok?'<span class="ok">出网正常 (204)</span>':'<span class="bad">出网失败（检查代理地址）</span>';}
async function qb(){const r=await api('/api/qb/proxy');document.getElementById('msg').innerHTML=r.ok?'<span class="ok">'+r.msg+'</span>':'<span class="bad">'+(r.msg||'失败')+'</span>';}
async function loadStatus(){const s=await api('/api/status');const tb=document.querySelector('#st tbody');tb.innerHTML='';for(const k in s){const v=s[k];tb.innerHTML+='<tr><td><span class="dot" style="background:'+(v?'#5fd38a':'#ff7a7a')+'"></span>'+k+'</td><td>'+(v?'可达':'不可达')+'</td></tr>';}}
load();loadStatus();
</script></body></html>"""


if __name__ == "__main__":
    os.makedirs("/data/downloads", exist_ok=True)
    os.makedirs("/data/movies", exist_ok=True)
    os.makedirs("/data/tv", exist_ok=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
