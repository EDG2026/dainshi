# -*- coding: utf-8 -*-
"""zhujiang.py — 自动生成广东珠江 / 广东影视订阅源（全非咪咕源，1080P）。

每天由 GitHub Actions 自动运行，流程：
  1. 从公开聚合源抓取「组播转单播」服务器列表（自动发现，不写死）
  2. 从聚合源抓取 ZTE PLTV 高码率源
  3. 对每个候选实测：HTTP 200 + 有真实数据 + 解析 SPS 确认 1920x1080
  4. 按实际带宽排序，每个频道取最快的 KEEP 条
  5. 写入 zhujiang.m3u

组播地址（广东电信 IPTV）：
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
KEEP = 2
TIMEOUT = 8
MIN_BYTES = 120000          # 少于这个字节认为没数据
MIN_HEIGHT = 720            # 低于这个分辨率不采用

# 频道 -> 组播地址
MCAST_CHANNELS = {
    "广东珠江": "239.77.0.1:5146",
    "广东影视": "239.77.0.217:5146",
}

# 聚合源：从中自动发现转发服务器和 ZTE 源
AGGREGATORS = [
    "https://raw.githubusercontent.com/leimao2333/leimaotv-sources/main/dist/live.m3u",
    "https://raw.githubusercontent.com/zbefine/iptv/main/iptv.m3u",
    "https://raw.githubusercontent.com/big-mouth-cn/tv/main/iptv.m3u",
]

# 备用服务器：聚合源全挂时兜底
FALLBACK_SERVERS = [
    "www.yahao.host:4022",
    "61.145.188.90:50055",
    "www.3344.uk:8888",
    "lbyjlt.vv5678.cn:8880",
    "lsa.familyds.com:8188",
]
# ==============================


def curl(args, timeout=30, binary=False):
    try:
        r = subprocess.run(["curl"] + args, capture_output=True, timeout=timeout)
        return r.stdout if binary else (r.stdout or b"").decode("utf-8", errors="ignore")
    except Exception:
        return b"" if binary else ""


def fetch(url, timeout=30):
    return curl(["-sL", "--max-time", str(timeout), "-A", UA, url], timeout + 10)


# ---------- 自动发现 ----------

def discover_servers():
    """从聚合源里找出所有 udp/xxx 转发服务器（host:port）"""
    servers = set()
    zte = {}   # 频道名 -> ZTE url

    for src in AGGREGATORS:
        print("  抓取 %s" % src.split("/")[3])
        txt = fetch(src, timeout=40)
        if not txt:
            continue
        lines = txt.splitlines()
        # 提取所有 udp 地址对应的服务器
        for m in re.finditer(r"https?://([^/\s]+)/udp/\d+\.\d+\.\d+\.\d+:\d+", txt):
            servers.add(m.group(1))
        # 找 ZTE PLTV 源（8Mbps 以上）
        cur_name = ""
        for i, ln in enumerate(lines):
            s = ln.strip()
            if s.startswith("#EXTINF:"):
                mm = re.search(r",([^,]+)$", s)
                cur_name = mm.group(1).strip() if mm else ""
            elif s.startswith("http") and ("珠江" in cur_name or "影视" in cur_name):
                # 探测是否高码率 HLS
                if "/tv/gdzj" in s or "PLTV" in s:
                    zte.setdefault(cur_name, s)

    result = sorted(servers)
    if not result:
        print("  [!] 未发现任何服务器，使用兜底列表")
        result = list(FALLBACK_SERVERS)
    else:
        print("  发现 %d 个转发服务器" % len(result))
    return result, zte


# ---------- 校验 ----------

def parse_sps_height(data):
    """从 TS 数据解析 SPS 拿高度，失败返回 0"""
    if not data or len(data) < 188:
        return 0
    n = len(data)
    i = 0
    while i < n - 5:
        if data[i] == 0 and data[i+1] == 0:
            if data[i+2] == 1:
                ns = i + 3
            elif data[i+2] == 0 and data[i+3] == 1:
                ns = i + 4
            else:
                i += 1
                continue
            if ns < n and (data[ns] & 0x1F) == 7:
                h = _sps_h(data[ns:min(ns+64, n)])
                if h:
                    return h
            i = ns
        else:
            i += 1
    return 0


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
        if profile in (100,110,122,244,44,83,86,118,128,138,139,134,135):
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
        w_mbs = ue(); h_map = ue()
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


def probe(url):
    """返回 (ok, mbps, height)"""
    try:
        # 先取 8 秒数据算带宽
        r = subprocess.run(
            ["curl", "-sL", "-o", "NUL" if sys.platform == "win32" else "/dev/null",
             "-w", "%{http_code}|%{size_download}|%{time_total}",
             "--max-time", str(TIMEOUT), "--connect-timeout", "6", "-A", UA, url],
            capture_output=True, text=True, timeout=TIMEOUT + 8)
        p = (r.stdout or "").strip().split("|")
        if len(p) < 3 or p[0] != "200":
            return False, 0, 0
        sz = int(p[1] or 0)
        t = float(p[2] or 1)
        if sz < MIN_BYTES or t <= 0:
            return False, 0, 0
        mbps = sz * 8 / t / 1000000

        # 取一小段验证分辨率
        data = curl(["-sL", "--max-time", "8", "-A", UA, url], timeout=16, binary=True)
        h = parse_sps_height(data)
        return True, mbps, h
    except Exception:
        return False, 0, 0


def probe_hls(url):
    """HLS 源：取二级列表确认有 BANDWIDTH"""
    try:
        body = fetch(url, timeout=12)
        if "#EXTM3U" not in body:
            return False, 0, 0
        m = re.search(r"BANDWIDTH=(\d+)", body)
        mbps = int(m.group(1)) / 1000000 if m else 0
        # 取分片验分辨率
        sub = None
        for ln in body.splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                sub = ln
                break
        if not sub:
            return False, 0, 0
        if not sub.startswith("http"):
            sub = url.rsplit("/", 1)[0] + "/" + sub
        body2 = fetch(sub, timeout=12)
        if "#EXTM3U" not in body2:
            return False, 0, 0
        seg = None
        for ln in body2.splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                seg = ln
                break
        if not seg:
            return False, 0, 0
        if not seg.startswith("http"):
            seg = sub.rsplit("/", 1)[0] + "/" + seg
        data = curl(["-sL", "--max-time", "12", "-A", UA, seg], timeout=20, binary=True)
        h = parse_sps_height(data)
        return True, mbps, h
    except Exception:
        return False, 0, 0


# ---------- 主流程 ----------

def main():
    print("=== 发现源 ===")
    servers, zte = discover_servers()

    out = ["#EXTM3U"]
    total = 0

    for ch, mcast in MCAST_CHANNELS.items():
        print("\n=== %s ===" % ch)
        picks = []

        # ZTE 高码率优先
        if ch in zte:
            ok, mbps, h = probe_hls(zte[ch])
            print("  ZTE %-8s %.1fMbps %dp" % ("OK " if ok else "BAD", mbps, h))
            if ok and h >= MIN_HEIGHT:
                picks.append((mbps, zte[ch]))

        # 组播转发
        urls = ["http://%s/udp/%s" % (s, mcast) for s in servers]
        with ThreadPoolExecutor(max_workers=10) as pool:
            futs = {pool.submit(probe, u): u for u in urls}
            for f in futs:
                u = futs[f]
                ok, mbps, h = f.result()
                if ok and h >= MIN_HEIGHT:
                    picks.append((mbps, u))
                    print("  MCAST %.2fMbps %dp  %s" % (mbps, h, u))

        if not picks:
            print("  无可用 1080P 源")
            continue

        picks.sort(key=lambda x: -x[0])
        for mbps, u in picks[:KEEP]:
            out.append('#EXTINF:-1 tvg-id="%s" tvg-name="%s" group-title="%s",%s'
                       % (ch, ch, GROUP, ch))
            out.append(u)
            total += 1
        print("  -> %d 条" % min(KEEP, len(picks)))

    if total == 0:
        print("\n[ERROR] 无可用源，保留旧文件")
        return 1

    with io.open(OUTPUT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    print("\n[DONE] %d 条写入 %s" % (total, OUTPUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
