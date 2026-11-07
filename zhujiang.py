# -*- coding: utf-8 -*-
"""zhujiang.py — 生成广东珠江 / 广东影视订阅源。

原则：只收「SPS 实测 >= 1080」的源，宁缺毋滥，不塞低分辨率 / 未验证源。
  1. 从公开聚合源自动发现「组播转单播」服务器
  2. 组播 / ZTE HLS 一律拉 TS 分片解析 SPS 高度，实测 1080 才收录
  3. 按「分辨率降序 > 带宽降序」排序，每频道最多 KEEP 条，不硬凑
  4. 测速全灭时用本机已实测 1080P 的源兜底，绝不退回低质源
  5. 写入 zhujiang.m3u

咪咕转发源已整体移除：其 master playlist 只有 BANDWIDTH 无 RESOLUTION，
分片为带签名的三级链接、curl 取不到真实 TS 数据（实测仅 18 字节），
标签码率 2.08Mbps，分辨率无法验证 —— 属于低质源，不再参与兜底。

组播地址（广东电信 IPTV，固定）：
  广东珠江 = 239.77.0.1:5146
  广东影视 = 239.77.0.217:5146
"""
import io
import re
import sys
import subprocess
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36"

# ============ 配置 ============
OUTPUT = "zhujiang.m3u"
GROUP = "广东"
KEEP = 3               # 每频道最多保留条数，不硬凑
TIMEOUT = 10           # 测速时长（秒）
MIN_BYTES = 100000
MIN_HEIGHT = 1080      # 硬门槛：SPS 实测高度必须 >= 1080

# 频道 -> 组播地址
MCAST_CHANNELS = {
    "广东珠江": "239.77.0.1:5146",
    "广东影视": "239.77.0.217:5146",
}

# 已知高码率 ZTE 源（master -> index -> 分片）
KNOWN_ZTE = {
    "广东珠江": "http://8.138.7.223/tv/gdzj.m3u8",
}

# 聚合源：自动发现组播转发服务器
AGGREGATORS = [
    "https://raw.githubusercontent.com/leimao2333/leimaotv-sources/main/dist/live.m3u",
    "https://raw.githubusercontent.com/zbefine/iptv/main/iptv.m3u",
    "https://raw.githubusercontent.com/big-mouth-cn/tv/main/iptv.m3u",
]

# 备用组播服务器（已剔除实测失效的 183.16.200.24:55555）
FALLBACK_SERVERS = [
    "www.yahao.host:4022",
    "lsa.familyds.com:8188",
    "61.145.188.90:50055",
    "www.3344.uk:8888",
]

# 兜底白名单：本机 SPS 实测 = 1080 的源（2026-11-07 验证）。
# 仅当 runner 测速全灭时使用，保证输出永远是高分辨率源，不退回咪咕。
VERIFIED_1080 = {
    "广东珠江": [
        "http://8.138.7.223/tv/gdzj.m3u8",
        "http://www.yahao.host:4022/udp/239.77.0.1:5146",
        "http://www.3344.uk:8888/udp/239.77.0.1:5146",
        "http://61.145.188.90:50055/udp/239.77.0.1:5146",
        "http://lsa.familyds.com:8188/udp/239.77.0.1:5146",
    ],
    "广东影视": [
        "http://www.3344.uk:8888/udp/239.77.0.217:5146",
        "http://www.yahao.host:4022/udp/239.77.0.217:5146",
        "http://61.145.188.90:50055/udp/239.77.0.217:5146",
        "http://lsa.familyds.com:8188/udp/239.77.0.217:5146",
    ],
}
# ==============================


def curl(args, timeout=30, binary=False):
    try:
        r = subprocess.run(["curl"] + args, capture_output=True, timeout=timeout)
        return r.stdout if binary else (r.stdout or b"").decode("utf-8", errors="ignore")
    except Exception:
        return b"" if binary else ""


def fetch(url, timeout=30):
    return curl(["-sL", "--max-time", str(timeout), "-A", UA, url], timeout + 10)


def discover_servers():
    """从聚合源自动发现组播转发服务器"""
    servers = set()
    for src in AGGREGATORS:
        name = src.split("/")[3]
        txt = fetch(src, timeout=40)
        if not txt:
            print("  %s 抓取失败" % name)
            continue
        found = set(re.findall(r"https?://([^/\s]+)/udp/\d+\.\d+\.\d+\.\d+:\d+", txt))
        print("  %s -> %d 个服务器" % (name, len(found)))
        servers |= found
    result = sorted(servers)
    if not result:
        print("  [!] 未发现，用兜底列表")
        result = list(FALLBACK_SERVERS)
    else:
        # 兜底里已验证 1080P 的服务器补进候选，提高命中率
        for s in FALLBACK_SERVERS:
            if s not in result:
                result.append(s)
    return result


def _sps_h(sps):
    bits = []
    for b in sps[1:]:
        for k in range(7, -1, -1):
            bits.append((b >> k) & 1)
    pos = [0]

    def rd(k):
        v = 0
        for _ in range(k):
            if pos[0] < len(bits):
                v = (v << 1) | bits[pos[0]]
                pos[0] += 1
        return v

    def ue():
        z = 0
        while pos[0] < len(bits) and bits[pos[0]] == 0:
            z += 1
            pos[0] += 1
        if pos[0] >= len(bits):
            return 0
        pos[0] += 1
        val = 0
        for _ in range(z):
            if pos[0] < len(bits):
                val = (val << 1) | bits[pos[0]]
                pos[0] += 1
        return (1 << z) - 1 + val

    try:
        profile = rd(8); rd(8); rd(8)
        ue()
        if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
            cf = ue()
            if cf == 3:
                rd(1)
            ue(); ue(); rd(1)
            if rd(1):
                return 0
        ue()
        poc = ue()
        if poc == 0:
            ue()
        elif poc == 1:
            rd(1); ue(); ue()
            for _ in range(ue()):
                ue()
        ue(); rd(1)
        ue(); h_map = ue()
        fmo = rd(1)
        if not fmo:
            rd(1)
        rd(1)
        crop = rd(1)
        h = (h_map + 1) * 16 * (2 - fmo)
        if crop:
            ue(); ue(); t = ue(); b = ue()
            h -= (t + b) * 2 * (2 - fmo)
        return h
    except Exception:
        return 0


def parse_sps_height(data):
    """从 TS 数据解析 SPS 高度"""
    if not data or len(data) < 188:
        return 0
    n = len(data)
    i = 0
    while i < n - 5:
        if data[i] == 0 and data[i + 1] == 0:
            if data[i + 2] == 1:
                ns = i + 3
            elif data[i + 2] == 0 and data[i + 3] == 1:
                ns = i + 4
            else:
                i += 1
                continue
            if ns < n and (data[ns] & 0x1F) == 7:
                h = _sps_h(data[ns:min(ns + 64, n)])
                if h:
                    return h
            i = ns
        else:
            i += 1
    return 0


def measure(url):
    """组播 TS：持续下载算带宽 + 拉数据解析 SPS 高度。
    返回 (ok, mbps, height, continuity)。continuity 仅作日志，不再作为硬门槛。"""
    try:
        r = subprocess.run(
            ["curl", "-sL", "-o", "NUL" if sys.platform == "win32" else "/dev/null",
             "-w", "%{http_code}|%{size_download}|%{time_total}",
             "--max-time", str(TIMEOUT), "--connect-timeout", "6", "-A", UA, url],
            capture_output=True, text=True, timeout=TIMEOUT + 10)
        p = (r.stdout or "").strip().split("|")
        if len(p) < 3 or p[0] != "200":
            return False, 0, 0, 0
        sz = int(p[1] or 0)
        t = float(p[2] or 1)
        if sz < MIN_BYTES or t <= 0:
            return False, 0, 0, 0
        mbps = sz * 8 / t / 1000000

        data = curl(["-sL", "--max-time", "8", "-A", UA, url], timeout=16, binary=True)
        h = parse_sps_height(data)

        continuity = 0
        if data and len(data) > 1880:
            total = len(data) // 188
            sync = sum(1 for i in range(total) if data[i * 188] == 0x47)
            continuity = sync / total if total else 0

        return True, mbps, h, continuity
    except Exception:
        return False, 0, 0, 0


def measure_hls(url):
    """ZTE HLS：master 取 BANDWIDTH，拉分片解析 SPS 实测高度。
    返回 (ok, mbps, height)。分辨率无法实测则不收。"""
    try:
        l1 = fetch(url, timeout=12)
        if "#EXTM3U" not in l1:
            return False, 0, 0
        m = re.search(r"BANDWIDTH=(\d+)", l1)
        mbps = int(m.group(1)) / 1000000 if m else 0
        sub = None
        for ln in l1.splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                sub = ln
                break
        if not sub:
            return False, 0, 0
        subUrl = sub if sub.startswith("http") else url.rsplit("/", 1)[0] + "/" + sub
        l2 = fetch(subUrl, timeout=12)
        if "#EXTM3U" not in l2:
            return False, 0, 0
        segs = []
        for ln in l2.splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                segs.append(ln if ln.startswith("http")
                            else subUrl.rsplit("/", 1)[0] + "/" + ln)
            if len(segs) >= 2:
                break
        h = 0
        for s in segs:
            d = curl(["-sL", "--max-time", "12", "-A", UA, s], timeout=18, binary=True)
            h = parse_sps_height(d)
            if h:
                break
        return (h >= MIN_HEIGHT), mbps, h
    except Exception:
        return False, 0, 0


def main():
    print("=== 发现组播服务器 ===")
    servers = discover_servers()

    out = ["#EXTM3U"]
    total = 0

    for ch, mcast in MCAST_CHANNELS.items():
        print("\n=== %s ===" % ch)
        cands = []   # (height, mbps, url, kind)

        # 1. ZTE 高码率 HLS
        if ch in KNOWN_ZTE:
            ok, mbps, h = measure_hls(KNOWN_ZTE[ch])
            print("  ZTE   %s %.1fMbps %dp" % ("OK " if ok else "BAD", mbps, h))
            if ok and h >= MIN_HEIGHT:
                cands.append((h, mbps, KNOWN_ZTE[ch], "ZTE"))

        # 2. 组播转发（SPS 实测 1080 才收）
        murls = ["http://%s/udp/%s" % (s, mcast) for s in servers]
        with ThreadPoolExecutor(max_workers=10) as pool:
            futs = {pool.submit(measure, u): u for u in murls}
            for f in futs:
                u = futs[f]
                ok, mbps, h, cont = f.result()
                if ok and h >= MIN_HEIGHT:
                    cands.append((h, mbps, u, "MCAST"))
                    print("  MCAST %-5.2fMbps %dp cont=%.0f%%  %s"
                          % (mbps, h, cont * 100, u))

        # 3. 全灭兜底：本机实测 1080P 的已验证源，绝不退回低质/咪咕
        if not cands:
            print("  [!] 测速无可用源，启用已验证 1080P 兜底")
            for u in VERIFIED_1080.get(ch, []):
                cands.append((1080, 0, u, "VERIFIED"))

        if not cands:
            print("  无可用源")
            continue

        # 排序：分辨率降序 > 带宽降序（高分辨率优先，宁缺毋滥）
        cands.sort(key=lambda x: (-x[0], -x[1]))
        picked = cands[:KEEP]
        for h, mbps, u, kind in picked:
            out.append('#EXTINF:-1 tvg-id="%s" tvg-name="%s" group-title="%s",%s'
                       % (ch, ch, GROUP, ch))
            out.append(u)
            total += 1
        print("  -> 采用 %d 条: %s"
              % (len(picked), ", ".join("%s(%dp)" % (k, h) for h, _, _, k in picked)))

    if total == 0:
        print("\n[ERROR] 无可用源，保留旧文件")
        return 1

    with io.open(OUTPUT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    print("\n[DONE] %d 条" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
