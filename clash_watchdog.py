#!/usr/bin/env python3
# clash_watchdog.py — NAS 出网自动恢复（经 OpenClash VM 192.168.68.2）
#
# 重要背景：OpenClash 的「自动选择」(URLTest) 组会卡死在已失效节点，且本版 Clash
# 没有可用的 urltest 强制刷新端点（404），靠它自己恢复不了。本看门狗由 NAS root
# crontab 每 3 分钟调用，做"务实"的自愈：
#   1. 经本地 squid(127.0.0.1:3128) 测出网（generate_204，稳定不重定向）；
#   2. 出网正常 -> 不动（保持当前 GLOBAL 选中的具体节点）；
#   3. 出网失败 -> 调 Clash API 遍历候选节点延迟，挑最快可用者钉到 GLOBAL。
# GLOBAL 钉到具体节点后，即便该节点后续死亡，下次出网探测失败即会改钉别的，形成自愈。
# 注意：本脚本不会把 GLOBAL 设回「自动选择」组（那组本身有卡死 bug），维持具体节点最稳。
import json, subprocess, urllib.request, urllib.parse, datetime

CLASH_API = "http://192.168.68.2:9090"
CLASH_SECRET = "spIu7LxC"
PROXY = "http://127.0.0.1:3128"
AUTOSELECT = "♻️ 自动选择"
EGRESS_TEST = "https://www.gstatic.com/generate_204"
TIMEOUT = 10
NODE_TIMEOUT = 4000
LOG = "/opt/media/clash_watchdog.log"


def log(m):
    line = "[%s] %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), m)
    print(line)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def api(method, path, body=None):
    req = urllib.request.Request(
        CLASH_API + path,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": "Bearer " + CLASH_SECRET,
                 "Content-Type": "application/json"},
        method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            b = r.read().decode()
            return json.loads(b) if b.strip() else None
    except Exception:
        return None


def egress_ok():
    try:
        r = subprocess.run(
            ["curl", "-s", "-m", str(TIMEOUT), "-x", PROXY, "-o", "/dev/null",
             "-w", "%{http_code}", EGRESS_TEST],
            capture_output=True, text=True, timeout=TIMEOUT + 5)
        return r.stdout.strip() in ("200", "204", "302")
    except Exception:
        return False


def best_node():
    g = api("GET", "/proxies/" + urllib.parse.quote(AUTOSELECT))
    if not g:
        return None
    cands = [x for x in g.get("all", [])
             if x not in ("DIRECT", "REJECT")
             and not any(k in x for k in ("流量", "到期", "重置", "GB", "官网", "更新软件"))]
    best, best_d = None, 1e9
    for n in cands:
        d = api("GET", "/proxies/" + urllib.parse.quote(n) +
                "/delay?url=" + urllib.parse.quote(EGRESS_TEST) +
                "&timeout=" + str(NODE_TIMEOUT))
        if isinstance(d, dict):
            dl = d.get("delay")
            if isinstance(dl, (int, float)) and dl < best_d:
                best_d, best = dl, n
    return best


def set_global(node):
    return api("PUT", "/proxies/GLOBAL", {"name": node})


def main():
    if egress_ok():
        log("EGRESS_OK")
        return
    log("EGRESS_DOWN -> 选最快节点")
    node = best_node()
    if node:
        set_global(node)
        log("PIN GLOBAL -> " + node)
    else:
        log("NO_WORKING_NODE（订阅失效/外部故障，需人工）")


if __name__ == "__main__":
    main()
