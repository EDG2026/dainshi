# -*- coding: utf-8 -*-
"""生成 zhujiang.m3u：从 migu.m3u 筛出广东珠江与广东影视，测速后输出可用源。
输出格式刻意保持最小：只写 #EXTM3U + #EXTINF + URL，不写任何自定义注释，
不带 tvg-logo，避免部分电视端播放器解析失败。
"""
import io
import os
import re
import sys
import subprocess

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36"

TARGETS = [
    ("广东珠江", ["广东珠江频道", "广东珠江", "珠江频道"], ["珠江"]),
    ("广东影视", ["南方影视", "广东影视", "南方影视频道"], ["南方影视", "广东影视"]),
]

INPUT = "migu.m3u"
OUTPUT = "zhujiang.m3u"
GROUP = "广东"
KEEP = 2


def parse(path):
    if not os.path.exists(path):
        return []
    with io.open(path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.read().splitlines()
    ch = []
    i, n = 0, len(lines)
    while i < n:
        s = lines[i].strip()
        if s.startswith("#EXTINF:"):
            url = ""
            j = i + 1
            while j < n and not lines[j].strip().startswith("#EXTINF:"):
                t = lines[j].strip()
                if t and not t.startswith("#"):
                    url = t
                    break
                j += 1
            m = re.search(r",([^,]+)$", s)
            name = m.group(1).strip() if m else ""
            if url:
                ch.append((url, name))
            i = j
            continue
        i += 1
    return ch


def probe(url):
    try:
        r = subprocess.run(
            ["curl", "-sL", "-o", os.devnull, "-w", "%{http_code}|%{content_type}",
             "--max-time", "15", "-A", UA, url],
            capture_output=True, text=True, timeout=20)
        raw = (r.stdout or "").strip()
        parts = raw.split("|")
        code = parts[0] if parts else "000"
        ctype = parts[1] if len(parts) > 1 else ""
        if code != "200":
            return False
        low = ctype.lower()
        if "mpegurl" not in low and "m3u8" not in low:
            return False
        return True
    except Exception:
        return False


def pick(ch, exact, kws):
    for e in exact:
        hits = [c for c in ch if c[1] == e]
        if hits:
            return hits
    for kw in kws:
        hits = [c for c in ch if kw in c[1]]
        if hits:
            return hits
    return []


def main():
    ch = parse(INPUT)
    print("channels in %s: %d" % (INPUT, len(ch)))
    if not ch:
        print("input missing or empty")
        return 1

    out = ["#EXTM3U"]
    total = 0
    for outname, exact, kws in TARGETS:
        cands = pick(ch, exact, kws)
        print("%s candidates: %d" % (outname, len(cands)))
        good = []
        for url, name in cands:
            ok = probe(url)
            print("  %s  %s" % ("OK " if ok else "BAD", name))
            if ok:
                good.append(url)
        for url in good[:KEEP]:
            out.append('#EXTINF:-1 tvg-id="%s" tvg-name="%s" group-title="%s",%s'
                       % (outname, outname, GROUP, outname))
            out.append(url)
            total += 1

    if total == 0:
        print("no working stream")
        return 1

    with io.open(OUTPUT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    print("written %d entries to %s" % (total, OUTPUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
