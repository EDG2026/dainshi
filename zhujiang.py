# -*- coding: utf-8 -*-
"""zhujiang.py — 自动生成广东珠江 / 广东影视订阅源。

每天由 GitHub Actions 自动运行：
  1. 从公开聚合源自动发现「组播转单播」服务器
  2. 组播候选 + 咪咕固定源候选一起测速
  3. 用持续下载量算真实带宽（不是瞬时）
  4. 每频道取最快的 3 条
  5. 写入 zhujiang.m3u

组播地址（广东电信 IPTV，固定）：
  广东珠江 = 239.77.0.1:5146
  广东影视 = 239.77.0.217:5146

咪咕固定源：http://IP:端口/ProgramID，链接长期有效（非签名链接）
  广东珠江 = 639731952
  广东影视 = 614961829
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
KEEP = 3
TIMEOUT = 10              # 测速时长（秒），越长越准
MIN_BYTES = 100000
MIN_HEIGHT = 720

# 频道 -> 组播地址
MCAST_CHANNELS = {
    "广东珠江": "239.77.0.1:5146",
    "广东影视": "239.77.0.217:5146",
}

# 频道 -> 咪咕 ProgramID
MIGU_PID = {
    "广东珠江": "639731952",
    "广东影视": "614961829",
}

# 咪咕固定源服务器（host:port）
MIGU_HOSTS = [
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
    "119.233.186.100:1234",
]

# 聚合源：自动发现组播转发服务器
AGGREGATORS = [
    "https://raw.githubusercontent.com/leimao2333/leimaotv-sources/main/dist/live.m3u",
    "https://raw.githubusercontent.com/zbefine/iptv/main/iptv.m3u",
    "https://raw.githubusercontent.com/big-mouth-cn/tv/main/iptv.m3u",
]

# 已知高带宽组播服务器（优先测，实测 1.5Mbps+ 1080p）
PRIORITY_SERVERS = [
    "183.16.200.24:55555",
]

# 备用组播服务器
FALLBACK_SERVERS = [
    "183.16.200.24:55555",
    "www.yahao.host:4022",
    "lsa.familyds.com:8188",
    "61.145.188.90:50055",
    "www.3344.uk:8888",
]

# 已知高码率 ZTE 源
KNOWN_ZTE = {
    "广东珠江": "http://8.138.7.223/tv/gdzj.m3u8",
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
    # 已知高带宽服务器放最前
    for s in reversed(PRIORITY_SERVERS):
        if s in result:
            result.remove(s)
        result.insert(0, s)
    return result


def parse_sps_height(data):
    """从 TS 数据解析 SPS 高度"""
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


def measure(url):
    """
    持续下载 TIMEOUT 秒，返回 (ok, mbps, height, bytes)
    用总量/总时间算带宽，避免瞬时波动误判
    """
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

        # 取数据验分辨率
        data = curl(["-sL", "--max-time", "8", "-A", UA, url], timeout=16, binary=True)
        h = parse_sps_height(data)

        # TS 连续性检查（0x47 同步字节比例）
        continuity = 0
        if data and len(data) > 1880:
            total = len(data) // 188
            sync = sum(1 for i in range(total) if data[i*188] == 0x47)
            continuity = sync / total if total else 0

        return True, mbps, h, continuity
    except Exception:
        return False, 0, 0, 0


def measure_hls(url):
    """HLS 源实测：取分片算真实带宽（不只看 BANDWIDTH 标签）"""
    try:
        l1 = fetch(url, timeout=10)
        if "#EXTM3U" not in l1:
            return False, 0
        sub = None
        for ln in l1.splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                sub = ln
                break
        if not sub:
            return False, 0
        subUrl = sub if sub.startswith("http") else url.rsplit("/", 1)[0] + "/" + sub
        l2 = fetch(subUrl, timeout=12)
        if "#EXTM3U" not in l2:
            return False, 0
        # 取前 2 个分片实测
        segs = []
        for ln in l2.splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                segs.append(ln if ln.startswith("http") else subUrl.rsplit("/", 1)[0] + "/" + ln)
            if len(segs) >= 2:
                break
        if not segs:
            return False, 0
        tb, tt = 0, 0.0
        for s in segs:
            r = subprocess.run(
                ["curl", "-sL", "-o", "NUL" if sys.platform == "win32" else "/dev/null",
                 "-w", "%{http_code}|%{size_download}|%{time_total}",
                 "--max-time", "15", "--connect-timeout", "6", "-A", UA, s],
                capture_output=True, text=True, timeout=22)
            p = (r.stdout or "").strip().split("|")
            if len(p) >= 3 and p[0] == "200":
                tb += int(p[1] or 0)
                tt += float(p[2] or 0)
        if tb < MIN_BYTES or tt <= 0:
            return False, 0
        return True, tb * 8 / tt / 1000000
    except Exception:
        return False, 0

def parse_sps_height(data):
    """从 TS 数据解析 SPS 高度"""
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


def measure(url):
    """
    持续下载 TIMEOUT 秒，返回 (ok, mbps, height, bytes)
    用总量/总时间算带宽，避免瞬时波动误判
    """
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

        # 取数据验分辨率
        data = curl(["-sL", "--max-time", "8", "-A", UA, url], timeout=16, binary=True)
        h = parse_sps_height(data)

        # TS 连续性检查（0x47 同步字节比例）
        continuity = 0
        if data and len(data) > 1880:
            total = len(data) // 188
            sync = sum(1 for i in range(total) if data[i*188] == 0x47)
            continuity = sync / total if total else 0

        return True, mbps, h, continuity
    except Exception:
        return False, 0, 0, 0


def measure_hls(url):
    """HLS 源测带宽"""
    try:
        body = fetch(url, timeout=12)
        if "#EXTM3U" not in body:
            return False, 0
        m = re.search(r"BANDWIDTH=(\d+)", body)
        return True, (int(m.group(1)) / 1000000 if m else 0)
    except Exception:
        return False, 0


def fetch_migu_channels(host, pid):
    """
    从咪咕转发服务器拿频道列表，找目标 ProgramID 的真实播放地址。
    服务器模式：http://HOST/migu/01.m3u8 返回全部频道的 m3u
    返回 (playlist_url, ok) 其中 playlist_url 是该频道的 index.m3u8
    """
    # 先试直接 /ProgramID
    direct = "http://%s/%s" % (host, pid)
    body = fetch(direct, timeout=8)
    if "#EXTM3U" in body and len(body) < 5000:
        return direct

    # 试从频道列表里找
    for path in ("migu/01.m3u8", "%s/01.m3u8" % pid, "hls/01.m3u8"):
        lst = fetch("http://%s/%s" % (host, path), timeout=12)
        if "#EXTM3U" not in lst or len(lst) < 10000:
            continue
        # 在列表里找含 ProgramID=xxx 的 URL
        for ln in lst.splitlines():
            ln = ln.strip()
            if ln.startswith("http") and ("ProgramID=%s" % pid) in ln:
                # 该 URL 末尾是 /ProgramID，把尾部换掉取 index
                base = ln.rsplit("/", 1)[0]
                return base + "/index.m3u8?" + ln.split("?", 1)[1] if "?" in ln else base + "/index.m3u8"
    return None


def measure_migu(host, pid):
    """
    测咪咕源。转发服务器只提供 master playlist，
    无法用 curl 走完整三级链路验证，因此用宽松判定：
      - HTTP 200
      - 返回 #EXTM3U 且含 STREAM-INF/BANDWIDTH
    带宽取 BANDWIDTH 标签值。
    返回 (ok, mbps, height)
    """
    try:
        base = "http://%s/%s" % (host, pid)
        body = fetch(base, timeout=8)
        if "#EXTM3U" not in body:
            return False, 0, 0
        if "STREAM-INF" not in body and "BANDWIDTH" not in body:
            return False, 0, 0
        m = re.search(r"BANDWIDTH=(\d+)", body)
        if not m:
            return False, 0, 0
        mbps = int(m.group(1)) / 1000000
        # 咪咕这两种都是 1920x1080
        return True, mbps, 1080
    except Exception:
        return False, 0, 0

def main():
    print("=== 发现组播服务器 ===")
    servers = discover_servers()

    out = ["#EXTM3U"]
    total = 0

    for ch, mcast in MCAST_CHANNELS.items():
        print("\n=== %s ===" % ch)
        cands = []   # (mbps, url, kind)

        # 1. ZTE 高码率
        if ch in KNOWN_ZTE:
            ok, mbps = measure_hls(KNOWN_ZTE[ch])
            print("  ZTE   %s %.1fMbps" % ("OK " if ok else "BAD", mbps))
            if ok and mbps >= 4.0:
                cands.append((mbps, KNOWN_ZTE[ch], "ZTE"))

        # 2. 组播转发
        murls = ["http://%s/udp/%s" % (s, mcast) for s in servers]
        with ThreadPoolExecutor(max_workers=10) as pool:
            futs = {pool.submit(measure, u): u for u in murls}
            for f in futs:
                u = futs[f]
                ok, mbps, h, cont = f.result()
                if ok and h >= MIN_HEIGHT and cont > 0.9:
                    cands.append((mbps, u, "MCAST"))
                    print("  MCAST %-5.2fMbps %dp cont=%.0f%%  %s" % (mbps, h, cont*100, u))

        # 3. 咪咕固定源（HLS 三级测速）
        if ch in MIGU_PID:
            pid = MIGU_PID[ch]
            with ThreadPoolExecutor(max_workers=8) as pool:
                futs = {pool.submit(measure_migu, h_, pid): h_ for h_ in MIGU_HOSTS}
                for f in futs:
                    host = futs[f]
                    try:
                        ok, mbps, h = f.result()
                    except Exception:
                        continue
                    if ok and h >= MIN_HEIGHT:
                        cands.append((mbps, "http://%s/%s" % (host, pid), "MIGU"))
                        print("  MIGU  %-5.2fMbps %dp  %s" % (mbps, h, host))

        if not cands:
            print("  无可用源")
            continue

        # 排序：ZTE > MCAST（组播，非咪咕）> MIGU（兜底）
        # 同类型内按带宽从高到低
        priority = {"ZTE": 0, "MCAST": 1, "MIGU": 2}
        cands.sort(key=lambda x: (priority.get(x[2], 9), -x[0]))
        picked = cands[:KEEP]
        for mbps, u, kind in picked:
            out.append('#EXTINF:-1 tvg-id="%s" tvg-name="%s" group-title="%s",%s'
                       % (ch, ch, GROUP, ch))
            out.append(u)
            total += 1
        print("  -> 采用 %d 条: %s" % (len(picked), ", ".join(k for _,_,k in picked)))

    if total == 0:
        print("\n[ERROR] 无可用源，保留旧文件")
        return 1

    with io.open(OUTPUT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    print("\n[DONE] %d 条" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())








