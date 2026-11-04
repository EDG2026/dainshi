# -*- coding: utf-8 -*-
"""
从 migu.m3u 中筛出指定频道，对同一频道的候选测速，输出最快的若干条。
生成 zhujiang.m3u —— 供订阅使用。

测速用系统 curl（requests 会被咪咕服务端按客户端特征拒绝返回 502，
curl 能正常拿到 302 -> 200 的 HLS 链路），因此不依赖 requests。

用法：
    python pick_best.py
"""
import re
import sys
import json
import time
import shutil
import subprocess

# ====================== 配置 ======================
INPUT_FILE = "migu.m3u"
OUTPUT_FILE = "zhujiang.m3u"

TARGETS = [
    {"out_name": "广东珠江", "exact": ["广东珠江频道", "广东珠江", "珠江频道"], "keywords": ["珠江"]},
    {"out_name": "广东影视", "exact": ["南方影视", "广东影视", "南方影视频道"], "keywords": ["南方影视", "广东影视"]},
]

GROUP_NAME = "广东"
KEEP_PER_CHANNEL = 2
TIMEOUT = 10
MAX_CANDIDATES = 12
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36"
# =================================================


def find_curl():
    for exe in ("curl", "curl.exe"):
        p = shutil.which(exe)
        if p:
            return p
    return None


CURL = find_curl()


def parse_m3u(content):
    channels = []
    lines = content.splitlines()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i].strip()
        if line.startswith("#EXTINF:"):
            extinf = line
            url = ""
            j = i + 1
            while j < n and not lines[j].strip().startswith("#EXTINF:"):
                s = lines[j].strip()
                if s and not s.startswith("#"):
                    url = s
                    break
                j += 1
            m = re.search(r",([^,]+)$", extinf)
            name = m.group(1).strip() if m else ""
            if url:
                channels.append((extinf, url, name))
            i = j
            continue
        i += 1
    return channels


def match_channel(channels, exact, keywords):
    for t in exact:
        hits = [c for c in channels if c[2] == t]
        if hits:
            return hits
    for kw in keywords:
        hits = [c for c in channels if kw in c[2]]
        if hits:
            return hits
    return []


def probe(url):
    """用 curl 测速。返回 (ok, rtt_ms, reason)"""
    if not CURL:
        return False, 0, "无curl"
    # -s 静默 -L 跟随重定向 -o NUL 丢弃正文 -w 输出指标
    fmt = "%{http_code}|%{content_type}|%{time_starttransfer}"
    cmd = [CURL, "-s", "-L", "-o", "NUL", "-w", fmt,
           "--max-time", str(TIMEOUT), "-A", UA, url]
    t0 = time.time()
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT + 5)
        rtt = (time.time() - t0) * 1000
        raw = (out.stdout or "").strip()
        if not raw:
            return False, rtt, "无响应"
        parts = raw.split("|")
        code = parts[0] if parts else "000"
        ctype = parts[1] if len(parts) > 1 else ""
        starttransfer = parts[2] if len(parts) > 2 else "0"
        if code != "200":
            return False, rtt, "HTTP %s" % code
        if "mpegurl" not in ctype.lower() and "m3u8" not in ctype.lower():
            return False, rtt, "非HLS(%s)" % (ctype or "?")
        try:
            st_ms = float(starttransfer) * 1000
        except Exception:
            st_ms = rtt
        return True, st_ms, "ok"
    except subprocess.TimeoutExpired:
        return False, 0, "超时"
    except Exception as e:
        return False, 0, type(e).__name__


def rebuild_extinf(extinf, out_name):
    logo = ""
    m = re.search(r'tvg-logo="([^"]*)"', extinf)
    if m:
        logo = m.group(1)
    parts = ['#EXTINF:-1', 'tvg-id="%s"' % out_name, 'tvg-name="%s"' % out_name]
    if logo:
        parts.append('tvg-logo="%s"' % logo)
    parts.append('group-title="%s"' % GROUP_NAME)
    return " ".join(parts) + "," + out_name


def main():
    if not CURL:
        print("[ERROR] 找不到 curl，无法测速")
        return 2

    try:
        with open(INPUT_FILE, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        print("[ERROR] 找不到 %s" % INPUT_FILE)
        return 1

    channels = parse_m3u(content)
    print("[INFO] %s 共 %d 个频道；curl=%s" % (INPUT_FILE, len(channels), CURL))

    out = ['#EXTM3U']
    ok_count = 0
    missing = []

    for tgt in TARGETS:
        name = tgt["out_name"]
        cands = match_channel(channels, tgt["exact"], tgt["keywords"])[:MAX_CANDIDATES]
        print("\n[%s] 候选 %d 条" % (name, len(cands)))
        if not cands:
            missing.append(name)
            continue

        ranked = []
        for extinf, url, real in cands:
            ok, rtt, reason = probe(url)
            print("    [%s] %6.0fms  %-8s  %s" % ("OK " if ok else "BAD", rtt, reason, real))
            if ok:
                ranked.append((rtt, extinf, url, real))

        if not ranked:
            print("    [WARN] 无可用")
            missing.append(name)
            continue

        ranked.sort(key=lambda x: x[0])
        for rtt, extinf, url, real in ranked[:KEEP_PER_CHANNEL]:
            out.append("# 延迟 %.0fms  来源: %s" % (rtt, real))
            out.append(rebuild_extinf(extinf, name))
            out.append(url)
            ok_count += 1

    if ok_count == 0:
        print("\n[ERROR] 无可用源，保留原 %s" % OUTPUT_FILE)
        return 1

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")

    print("\n[DONE] %s 写入 %d 条" % (OUTPUT_FILE, ok_count))
    if missing:
        print("[WARN] 缺失: %s" % ", ".join(missing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
