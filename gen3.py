# -*- coding: utf-8 -*-
"""生成 zhujiang.m3u：广东珠江（优先高码率/UDP转发）+ 广东影视（咪咕兜底）。
只输出 tvg-id / tvg-name / group-title，不带 logo，不带自定义注释。
"""
import io
import os
import re
import sys
import subprocess

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36"

# 珠江的来源：多个聚合仓库
ZJ_SOURCES = [
    "https://raw.githubusercontent.com/leimao2333/leimaotv-sources/main/dist/live.m3u",
    "https://raw.githubusercontent.com/zbefine/iptv/main/iptv.m3u",
    "https://raw.githubusercontent.com/big-mouth-cn/tv/main/iptv.m3u",
]
# 广东影视的来源：咪咕（由 MIGU.py 生成）
NF_INPUT = "migu.m3u"

OUTPUT = "zhujiang.m3u"
GROUP = "广东"
KEEP_ZJ = 4
KEEP_NF = 2
TIMEOUT = 12


def curl(url, timeout=30, binary=False):
    try:
        r = subprocess.run(["curl", "-sL", "--max-time", str(timeout), "-A", UA, url],
                           capture_output=True, timeout=timeout + 10)
        if binary:
            return r.stdout or b""
        return (r.stdout or b"").decode("utf-8", errors="ignore")
    except Exception:
        return b"" if binary else ""


def parse_m3u(text):
    out = []
    lines = text.splitlines()
    i, n = 0, len(lines)
    while i < n:
        s = lines[i].strip()
        if s.startswith("#EXTINF:"):
            m = re.search(r",([^,]+)$", s)
            name = m.group(1).strip() if m else ""
            url = ""
            j = i + 1
            while j < n and not lines[j].strip().startswith("#EXTINF:"):
                t = lines[j].strip()
                if t and not t.startswith("#"):
                    url = t
                    break
                j += 1
            if url:
                out.append((name, url))
            i = j
            continue
        i += 1
    return out


def probe(url):
    """返回 (ok, bw, is_udp, real_bytes)"""
    try:
        # 先拿一级内容，判断状态
        r = subprocess.run(
            ["curl", "-sL", "--max-time", str(TIMEOUT), "-A", UA,
             "-w", "\n__C__%{http_code}", url],
            capture_output=True, timeout=TIMEOUT + 8)
        raw = (r.stdout or b"").decode("utf-8", errors="ignore")
        m = re.search(r"__C__(\d+)\s*$", raw)
        code = m.group(1) if m else "000"
        body = re.sub(r"\n__C__\d+\s*$", "", raw)
        if code != "200" or len(body) < 30:
            return False, 0, False, 0

        # BANDWIDTH
        bw = 0
        bm = re.search(r"BANDWIDTH=(\d+)", body)
        if bm:
            bw = int(bm.group(1))

        # 进二级找分片，确认真能出流
        if "#EXTM3U" in body and bw > 0:
            sub = None
            for line in body.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    sub = line
                    break
            if sub:
                if not sub.startswith("http"):
                    base = url.rsplit("/", 1)[0]
                    sub = base + "/" + sub
                subbody = curl(sub, timeout=15)
                if "#EXTM3U" in subbody:
                    # 取第一个分片实测
                    seg = None
                    for line in subbody.splitlines():
                        line = line.strip()
                        if line and not line.startswith("#"):
                            seg = line
                            break
                    if seg:
                        if not seg.startswith("http"):
                            seg = sub.rsplit("/", 1)[0] + "/" + seg
                        data = curl(seg, timeout=15, binary=True)
                        if len(data) > 10000 and data[0:1] == b"\x47":
                            return True, bw, ("/udp/" in url or "/rtp/" in url), len(data)

        # 无 BANDWIDTH 的（如 udpxy 直推 TS）：直接看能否拿到 TS 数据
        data = curl(url, timeout=10, binary=True)
        if len(data) > 50000 and data[0:1] == b"\x47":
            # 按单位时间估算码率
            return True, 0, ("/udp/" in url or "/rtp/" in url), len(data)

        return False, bw, False, 0
    except Exception:
        return False, 0, False, 0


def main():
    # ---- 珠江 ----
    all_ch = []
    for s in ZJ_SOURCES:
        txt = curl(s, timeout=40)
        ch = parse_m3u(txt)
        print("source %s -> %d channels" % (s.split("/")[3], len(ch)))
        all_ch.extend(ch)

    seen = set()
    uniq = []
    for name, url in all_ch:
        if url in seen:
            continue
        seen.add(url)
        uniq.append((name, url))
    print("unique channels:", len(uniq))

    zj_cands = [(nm, u) for nm, u in uniq if "珠江" in nm and "广东" in nm]
    if not zj_cands:
        zj_cands = [(nm, u) for nm, u in uniq if "珠江" in nm]
    print("ZhuJiang candidates:", len(zj_cands))

    zj_ok = []
    for nm, u in zj_cands:
        ok, bw, is_udp, sz = probe(u)
        print("  %s bw=%s udp=%s bytes=%s %s" % ("OK " if ok else "BAD", bw, is_udp, sz, u[:60]))
        if ok:
            zj_ok.append({"url": u, "bw": bw, "udp": is_udp, "bytes": sz})

    # 排序：udp 优先（画质最好），其次码率高
    zj_ok.sort(key=lambda x: (0 if x["udp"] else 1, -x["bw"], -x["bytes"]))
    zj_ok = zj_ok[:KEEP_ZJ]

    # ---- 广东影视（咪咕）----
    nf_ok = []
    if os.path.exists(NF_INPUT):
        with io.open(NF_INPUT, "r", encoding="utf-8", errors="ignore") as f:
            nfch = parse_m3u(f.read())
        nf_cands = [(nm, u) for nm, u in nfch if ("南方影视" in nm or "广东影视" in nm)]
        print("GuangDongYingShi candidates:", len(nf_cands))
        for nm, u in nf_cands:
            ok, bw, is_udp, sz = probe(u)
            print("  %s bw=%s %s" % ("OK " if ok else "BAD", bw, u[:60]))
            if ok:
                nf_ok.append(u)
    else:
        print("migu.m3u not found, skip")

    # ---- 输出 ----
    out = ["#EXTM3U"]
    total = 0
    for s in zj_ok:
        out.append('#EXTINF:-1 tvg-id="广东珠江" tvg-name="广东珠江" group-title="%s",广东珠江' % GROUP)
        out.append(s["url"])
        total += 1
    for u in nf_ok[:KEEP_NF]:
        out.append('#EXTINF:-1 tvg-id="广东影视" tvg-name="广东影视" group-title="%s",广东影视' % GROUP)
        out.append(u)
        total += 1

    if total == 0:
        print("no usable stream, keep old file")
        return 1

    with io.open(OUTPUT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    print("written %d entries (zj=%d, nf=%d)" % (total, len(zj_ok), len(nf_ok)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
