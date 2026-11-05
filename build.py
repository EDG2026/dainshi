# -*- coding: utf-8 -*-
"""生成 zhujiang.m3u — 广东珠江 + 广东影视，各取最快的 2 条。

源站清单来自 tonkiang 的实测结果（2026-11-05）。
每个源站同时提供两个频道，用 ProgramID 区分：
  广东珠江 = 639731952
  广东影视 = 614961829
"""
import io
import re
import sys
import subprocess
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36"

# 实测可用的源站（IP:端口）
HOSTS = [
    "112.80.138.207:1234",
    "183.233.155.69:8001",
    "27.211.184.159:3000",
    "139.224.111.181:3000",
    "183.134.129.249:10002",
    "123.115.202.110:5001",
    "180.137.128.21:1234",
    "14.118.89.3:7788",
    "123.183.160.229:7788",
    "113.232.4.18:1234",
    "114.240.228.159:7788",
    "175.169.108.44:1234",
    "152.32.226.172:1234",
    "114.249.16.185:1905",
    "58.210.97.102:1234",
    "119.233.186.100:1234",
    "27.211.184.159:3000",
    "123.53.42.5:5678",
    "123.131.38.84:1234",
]

CHANNELS = [
    ("广东珠江", "639731952"),
    ("广东影视", "614961829"),
]

OUTPUT = "zhujiang.m3u"
GROUP = "广东"
KEEP = 2
TIMEOUT = 10


def test(url):
    """返回 (ok, ttfb)"""
    try:
        r = subprocess.run(
            ["curl", "-sL", "-o", "/dev/null" if sys.platform != "win32" else "NUL",
             "-w", "%{http_code}|%{time_starttransfer}",
             "--max-time", str(TIMEOUT), "--connect-timeout", "6", "-A", UA, url],
            capture_output=True, text=True, timeout=TIMEOUT + 8)
        out = (r.stdout or "").strip()
        parts = out.split("|")
        if len(parts) >= 2 and parts[0] == "200":
            return True, float(parts[1] or 0)
        return False, 99.0
    except Exception:
        return False, 99.0


def main():
    out = ["#EXTM3U"]
    total = 0

    for ch_name, pid in CHANNELS:
        print("== %s ==" % ch_name)
        # 并发测所有源站
        tasks = ["http://%s/%s" % (h, pid) for h in HOSTS]
        results = []
        with ThreadPoolExecutor(max_workers=10) as pool:
            futs = {pool.submit(test, u): u for u in tasks}
            for f in futs:
                u = futs[f]
                ok, ttfb = f.result()
                if ok:
                    results.append((ttfb, u))
                    print("  OK  %.3fs  %s" % (ttfb, u))
                else:
                    print("  --  %s" % u)
        results.sort(key=lambda x: x[0])
        picked = results[:KEEP]
        if not picked:
            print("  无可用源")
            continue
        for ttfb, u in picked:
            out.append('#EXTINF:-1 tvg-id="%s" tvg-name="%s" group-title="%s",%s'
                       % (ch_name, ch_name, GROUP, ch_name))
            out.append(u)
            total += 1
        print("  -> 采用 %d 条" % len(picked))

    if total == 0:
        print("no usable source")
        return 1

    with io.open(OUTPUT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    print("\nwritten %d entries to %s" % (total, OUTPUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
