# -*- coding: utf-8 -*-
"""zhujiang.py v4 — 广东珠江 / 广东影视直播源实测与订阅生成

- HLS：同一连接连续下载 3 段，以分段 EXTINF/下载耗时的中位数衡量播放能力。
- 裸 TS：读取 MPEG-TS PCR 节目时钟，PCR 推进/墙钟 >=0.85 才视为流畅；
  不再用固定的 2Mbps 估算，避免把 8Mbps 源误算成 ratio=4。
- 每频道选 3 条；能流畅播放的排前，旧源只在同级时优先，慢 HLS 不会顶到第一。
- 输出按「珠江一条、影视一条」交替，每条内部按流畅度降序。
- 无 PCR 的源只判活并放在最后；频道故障保留最近清单，每轮写心跳保活。

用法：python zhujiang.py
"""
import io
import json
import os
import re
import sys
import subprocess
import statistics
import datetime
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

OUTPUT = "zhujiang.m3u"
GROUP = "广东"
STATE_DIR = ".state"
HEARTBEAT = os.path.join(STATE_DIR, "heartbeat.json")
TMP = ".probe.tmp"

CHANNELS = ["广东珠江", "广东影视"]
TARGET_PER_CH = 3
MIN_HEIGHT = 720
SAMPLE_SECONDS = 10
N_PROBE_SEGS = 3
MIN_SEG_BYTES = 50000
RETRY = 1
FAST_RATIO = 1.00
FLUENT_RATIO = 0.85
# HLS 采用同一连接连续下载的分段计时中位数，排除首段握手和偶发抖动。
HLS_PER_SEG_TIMEOUT = 35
HLS_HARD_TIMEOUT = 150
# PCR 能直接给出组播的节目时钟；PCR 推进 / 墙钟推进 >=1 才跟得上直播。
# 少量源不发 PCR，回退到「字节速率 / 节目时长」；没有两种时钟时只判活，不冒充测速。
MIN_STREAM_BYTES = 10000

# ===== 源池：每频道按"上一次实测优劣"预排序，运行时全部重新测，按 ratio 重排 =====
SOURCES = {
    "广东珠江": [
        # 组播优先： hidns 在 2026-11-13 00:00 实测连续 3 段只有 0.23–0.27 倍实时，必卡。
        "http://www.3344.uk:8888/udp/239.77.0.1:5146",
        "http://www.yahao.host:4022/udp/239.77.0.1:5146",
        "http://61.145.188.90:50055/udp/239.77.0.1:5146",
        "http://lsa.familyds.com:8188/udp/239.77.0.1:5146",
        # HLS 只作异地/组播失效时的备用，不再排第一
        "https://gxyd.484947.xyz/PLTV/77777777/224/3221225717/index.m3u8"
        "?servicetype=1&&IASHttpSessionId=RR778520250111113355399717&ispcode=1108",
        "https://2.fast.hidns.vip/163189/gdzj",
    ],
    "广东影视": [
        # 没有 HLS 源可用（荔枝网官方线全带签名、gxyd 无此频道、公开列表只有组播），
        # 只能靠 udpxy 组播。4 条同一组播地址，走 4 个不同的转发站，互为备份。
        "http://www.3344.uk:8888/udp/239.77.0.217:5146",
        "http://www.yahao.host:4022/udp/239.77.0.217:5146",
        "http://61.145.188.90:50055/udp/239.77.0.217:5146",
        "http://lsa.familyds.com:8188/udp/239.77.0.217:5146",
        # 荔枝网 720P 转发，窗口期随机可用，放最后当惊喜
        "http://tv.8610000.cn:555/gdtv/gdtv.php?id=53",
    ],
}
# 防串台：每个频道只认自己列出的地址
ALLOWED = {ch: list(v) for ch, v in SOURCES.items()}


# ---------------------------------------------------------------- 基础工具
def run_curl(args, timeout=90):
    try:
        r = subprocess.run(["curl"] + args, capture_output=True, timeout=timeout)
        return r.stdout or b""
    except Exception:
        return b""


def fetch(url, timeout=20):
    out = run_curl(["-sL", "--max-time", str(timeout), "-A", UA, url], timeout + 15)
    return out.decode("utf-8", errors="ignore")


def grab(url, timeout=40):
    """下载到临时文件，返回 (code, bytes, wall_seconds, sps_height)。"""
    if os.path.exists(TMP):
        try:
            os.remove(TMP)
        except Exception:
            pass
    out = run_curl(["-sL", "--max-time", str(timeout), "-A", UA, "-o", TMP,
                    "-w", "%{http_code}|%{size_download}|%{time_total}", url],
                   timeout + 20)
    try:
        code, size, wall = out.decode("ascii", errors="ignore").strip().split("|")
        size = int(size)
        wall = float(wall) or 0.001
    except Exception:
        return 0, 0, 0.001, 0
    height = 0
    if os.path.exists(TMP):
        try:
            with open(TMP, "rb") as f:
                blob = f.read()
            if len(blob) > 2000:
                height = parse_sps_height(blob)
        except Exception:
            pass
        try:
            os.remove(TMP)
        except Exception:
            pass
    return (1 if code == "200" else 0), size, wall, height


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
    if not data or len(data) < 188:
        return 0
    n = len(data); i = 0
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


# ---------------------------------------------------------------- 探针
def _seg_base(url):
    """分段基准：根相对路径取 origin，相对路径取 manifest 目录。"""
    p = urlsplit(url)
    return "%s://%s" % (p.scheme, p.netloc)


def probe_hls(url):
    """HLS：顺序拉最后 3 个分段（同一条 keep-alive 连接），
    用 sum(EXTINF) / sum(下载耗时) 当可播放比。
    单段测量每次都重建 TLS 握手，会把 ratio 压低 2-5 倍，必须多段。"""
    # manifest 都很小；加体积上限，裸 TS 源不会在这里把几十 MB 灌进内存
    m = run_curl(["-sL", "--max-time", "10", "--max-filesize", "300000",
                  "-A", UA, url], 40).decode("utf-8", errors="ignore")
    if "#EXTM3U" not in m:
        return None
    durs, segs = [], []
    cur = None
    for ln in m.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith("#EXTINF:"):
            try:
                cur = float(s.split(":", 1)[1].split(",")[0])
            except Exception:
                cur = None
        elif not s.startswith("#"):
            segs.append(s)
            durs.append(cur if cur else 10.0)
            cur = None
    if not segs:
        return None
    # 避开两头的坑：第一段常是冷启动（源端刚切片，慢一个量级），
    # 最后一段可能还在写入（拿到 403 或半截文件）。取中间靠后的段。
    pool_s, pool_d = segs, durs
    if len(segs) > N_PROBE_SEGS + 2:
        pool_s, pool_d = segs[1:-1], durs[1:-1]
    take = min(N_PROBE_SEGS, len(pool_s))
    use_s, use_d = pool_s[-take:], pool_d[-take:]
    base_dir = url.rsplit("/", 1)[0] + "/"
    root = _seg_base(url)
    urls = []
    for s in use_s:
        if s.startswith("http"):
            urls.append(s)
        elif s.startswith("/"):
            urls.append(root + s)
        else:
            urls.append(base_dir + s)
    if os.path.exists(TMP):
        try:
            os.remove(TMP)
        except Exception:
            pass
    hard = 25
    # --next 让每个分段独立计时，同时复用同一条连接；不能用整批总时间，
    # 否则一个 60 秒的坏段会污染全部样本。
    args = ["-sL", "-A", UA]
    for k, u in enumerate(urls):
        if k:
            args += ["--next", "-sL", "-A", UA]
        args += ["--max-time", str(HLS_PER_SEG_TIMEOUT),
                 "-o", TMP if k == len(urls) - 1 else "NUL", u,
                 "-w", "%{http_code}|%{size_download}|%{time_total}\n"]
    out = run_curl(args, timeout=HLS_HARD_TIMEOUT)
    rows = [r for r in out.decode("ascii", errors="ignore").splitlines() if r.count("|") == 2]
    if len(rows) != len(urls):
        return None
    samples = []
    for k, r in enumerate(rows):
        code, size, wall = r.split("|")
        size, wall = int(size), float(wall)
        if code != "200" or size < MIN_SEG_BYTES or wall <= 0:
            continue
        samples.append((use_d[k] / wall, size * 8 / use_d[k] / 1e6))
    if not samples:
        return None
    ratios = [x[0] for x in samples]
    ratio = statistics.median(ratios)
    mbps = statistics.median([x[1] for x in samples])
    dur = sum(use_d)
    tot_wall = sum(float(r.split("|")[2]) for r in rows)
    height = 0
    if os.path.exists(TMP):
        try:
            blob = open(TMP, "rb").read()
            if len(blob) > 2000:
                height = parse_sps_height(blob)
        except Exception:
            pass
        try:
            os.remove(TMP)
        except Exception:
            pass
    return {"url": url, "kind": "hls", "ratio": ratio, "mbps": mbps,
            "height": height, "note": "%d/%d段,中位播放比" % (len(samples), len(urls)),
            "pcr_ok": False, "clock": "hls", "samples": len(samples)}


def pcr_clock(blob):
    """返回 (主PCR PID的时长秒, 采样数)。PCR 五字节在 adaptation flags 之后。"""
    clocks = {}
    for i in range(0, len(blob) - 188 + 1, 188):
        if blob[i] != 0x47:
            continue
        pid = ((blob[i + 1] & 0x1f) << 8) | blob[i + 2]
        if ((blob[i + 3] >> 4) & 3) not in (2, 3):
            continue
        q = i + 4
        if q >= len(blob):
            continue
        alen = blob[q]
        if alen >= 7 and q + 6 < len(blob) and (blob[q + 1] & 0x10):
            b = blob[q + 2:q + 7]
            pcr = ((b[0] << 25) | (b[1] << 17) | (b[2] << 9)
                   | (b[3] << 1) | (b[4] >> 7)) / 90000.0
            clocks.setdefault(pid, []).append(pcr)
    if not clocks:
        return 0.0, 0
    vals = max(clocks.values(), key=len)
    if len(vals) < 3:
        return 0.0, len(vals)
    return vals[-1] - vals[0], len(vals)


def probe_ts(url, seconds=SAMPLE_SECONDS):
    """裸 TS 使用 PCR 节目时钟，不再假定所有频道都是 2Mbps。"""
    tmp = TMP + "." + str(abs(hash(url)) % 1000000)
    # 用 time_total - time_starttransfer 当有效下载窗口：
    # time_total 含 DNS/TCP/TLS 握手，把它算进播放速度会让人为偏低 2–7%。
    args = ["-sL", "--max-time", str(seconds), "-A", UA,
            "-o", tmp,
            "-w", "%{http_code}|%{size_download}|%{time_total}|%{time_starttransfer}", url]
    out = run_curl(args, timeout=seconds + 15).decode("ascii", errors="ignore")
    try:
        code, size, total, first = out.strip().split("|")
        size = int(size)
        wall = max(0.001, float(total) - float(first))
    except Exception:
        code, size, wall = "000", 0, 0.0
    try:
        blob = io.open(tmp, "rb").read() if size >= MIN_STREAM_BYTES else b""
    except Exception:
        blob = b""
    try:
        os.remove(tmp)
    except Exception:
        pass
    if code != "200" or not blob or wall <= 0:
        return None
    height = parse_sps_height(blob)
    pcr_span, pcr_n = pcr_clock(blob)
    mbps = len(blob) * 8 / wall / 1e6
    if pcr_span > 0.05:
        ratio = pcr_span / wall
        clock = "pcr"
        encoded = len(blob) * 8 / pcr_span / 1e6
        note = "PCR %.2fs/墙钟%.1fs" % (pcr_span, wall)
    else:
        # 没有 PCR 时不能伪造 2Mbps 基准。标为 unknown，仅作最后保底。
        ratio = 0.0
        clock = "unknown"
        encoded = 0.0
        note = "无PCR,只判活"
    return {"url": url, "kind": "ts", "ratio": ratio, "mbps": mbps,
            "encoded_mbps": encoded, "height": height, "note": note,
            "pcr_ok": pcr_span > 0.05, "clock": clock, "samples": pcr_n}


def probe(url):
    """先按 HLS 试（看返回体有没有 #EXTM3U），不是 HLS 再当裸 TS 采样。
    不能靠 URL 里有没有 .m3u8 判断——很多站的 manifest 是无后缀路径。"""
    is_raw_ts = "/udp/" in url or "/rtp/" in url
    for _ in range(RETRY + 1):
        if not is_raw_ts:
            r = probe_hls(url)
            if r:
                return r
        r = probe_ts(url)
        if r:
            return r
    return None


# ---------------------------------------------------------------- 读写
def read_current():
    if not os.path.exists(OUTPUT):
        return {}, ""
    with io.open(OUTPUT, "r", encoding="utf-8") as f:
        raw = f.read()
    cur = {ch: [] for ch in CHANNELS}
    ch = None
    for ln in raw.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith("#EXTINF"):
            name = s.rsplit(",", 1)[-1].strip()
            ch = name if name in CHANNELS else None
        elif s.startswith("http") and ch:
            cur[ch].append(s)
            ch = None
    return cur, raw


def play_ratio(r):
    """统一口径的「可播放比」，只回答一件事：能不能跟上实时。上限恒为 1.0。

    HLS 的 ratio 是「分段时长 / 下载耗时」，下载有富余就能算出 6.5 这种值；
    PCR 的 ratio 是「节目时钟推进 / 墙钟推进」，物理上限就是 1.0。
    这两个数不同量纲，直接比大小会把 720P/1.4Mbps 的窗口源排到
    1080P/8.2Mbps 的组播前面。带宽富余属于"还能更快"，不属于"更好看"。
    """
    if r["clock"] == "unknown":
        return 0.0
    return min(r["ratio"], 1.0)


def quality_score(r):
    """排序用的综合分，从高到低依次比：画质档位、播放余量、源类型稳定性。

    - 画质档位：1080P=2，已知 >=720P=1，高度未知=1（按及格处理，不冤杀），
      明确低于 720P=0。
    - 播放余量：统一后的 play_ratio 分三档（满速 / 够用 / 不够），
      不按连续值比较，避免测量噪声压过源类型的真实差异。
    - 源类型：同样条件下组播优先于 HLS —— HLS 是第三方中转站、带时效签名，
      历史上整站 302 过；组播是广东本地源，晚高峰才慢。
    """
    height = r["height"] or 0
    if height >= 1080:
        h_tier = 2
    elif height == 0 or height >= MIN_HEIGHT:
        h_tier = 1
    else:
        h_tier = 0
    # 播放余量也分档，不做连续比较：0.99 和 1.00 的差别是测量噪声，
    # 拿它去压过"组播比 HLS 稳"这种真实差异不合理。
    p = play_ratio(r)
    if p >= 0.95:
        p_tier = 2
    elif p >= FLUENT_RATIO:
        p_tier = 1
    else:
        p_tier = 0
    stable = 1 if r["kind"] == "ts" else 0
    return (h_tier, p_tier, stable)


def fluent(r):
    """能跟上实时、且画质不明确低于 720P。"""
    return (r["clock"] != "unknown" and play_ratio(r) >= FLUENT_RATIO
            and not (r["kind"] == "hls" and 0 < r["height"] < MIN_HEIGHT))


def render(picked, measured=None):
    """两个频道逐条交替；每条按本轮流畅度排序。
    播放器的顺序是全局的，不能先塞满珠江再放影视。"""
    measured = measured or {}
    rows = []
    for ch in CHANNELS:
        rs = {r["url"]: r for r in measured.get(ch, [])}
        for u in picked.get(ch, []):
            r = rs.get(u)
            # 频道全死沿用旧清单时没有本轮测量，放该频道末尾
            score = quality_score(r) if r and fluent(r) else (-1, -1.0, -1)
            rows.append((ch, u, score))
    out = ["#EXTM3U"]
    for i in range(max((len(picked.get(ch, [])) for ch in CHANNELS), default=0)):
        round_rows = [(ch, u, s) for ch, u, s in rows
                      if picked.get(ch, []).index(u) == i]
        # score 现在是 (画质档位, 播放比, 源类型) 元组，逐项降序
        round_rows.sort(key=lambda x: x[2], reverse=True)
        for ch, u, score in round_rows:
            out.append('#EXTINF:-1 tvg-id="%s" tvg-name="%s" group-title="%s",%s'
                       % (ch, ch, GROUP, ch))
            out.append(u)
    return "\n".join(out) + "\n"


def write_heartbeat(picked, changed, note, detail):
    if not os.path.isdir(STATE_DIR):
        os.makedirs(STATE_DIR)
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rec = {
        "last_check_utc": now,
        "changed": bool(changed),
        "note": note,
        "total": sum(len(v) for v in picked.values()),
        "channels": {ch: picked.get(ch, []) for ch in CHANNELS},
        "measured": detail,
    }
    with io.open(HEARTBEAT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("  [HB] %s  total=%d  changed=%s  %s"
          % (now, rec["total"], changed, note))


def main():
    prev, raw = read_current()
    print("=== 上一轮清单 ===")
    for ch in CHANNELS:
        print("  %s: %d 条" % (ch, len(prev.get(ch, []))))

    picked, detail, changed = {}, {}, False

    for ch in CHANNELS:
        print("\n=== %s ===" % ch)
        # 顺序完全由 SOURCES 定义：HLS 在前（任何地区都能连），udpxy 组播在后（只在广东快）。
        # 上一轮的清单只用于 diff，不参与排序 —— 否则一旦某轮取了组播，
        # prefer 会把组播永久顶到前面，HLS 再也回不来。
        order = list(ALLOWED[ch])

        # 所有源串行测量：测速本身就是带宽测试，并发会把自己的出口打满，
        # 得出"源慢"的假结论。每频道最多 6 条，优先复测上一轮在用的源。
        prev_urls = [u for u in prev.get(ch, []) if u in order]
        order = prev_urls + [u for u in order if u not in prev_urls]
        got = {}
        for u in order:
            got[u] = probe(u)

        scored = []
        for u in order:
            r = got.get(u)
            if not r:
                print("  DEAD  %s" % u)
                continue
            if r["kind"] == "hls" and 0 < r["height"] < MIN_HEIGHT:
                print("  LOW-HD  %4dp  %s" % (r["height"], u))
                continue
            if r["clock"] == "unknown":
                flag = "LIVE"
            elif r["ratio"] >= 1.0:
                flag = "FAST"
            elif r["ratio"] >= 0.85:
                flag = "OK  "
            else:
                flag = "SLOW"
            print("  %s  play=%.2f  wire=%5.2fMbps  %4dp  %-18s %s"
                  % (flag, r["ratio"], r["mbps"], r["height"], r["note"], u))
            scored.append(r)

        # 播放能力优先；上一轮已用的源在同档时优先，避免轻微抖动引起频繁换源。
        # 没有 PCR 的源永远排在有时钟的源后面，但可用来保底。
        def rank(r):
            # quality_score 三项都按"越大越好"，取负号让 sorted 升序等于降序。
            return (0 if fluent(r) else 1,
                    tuple(-x for x in quality_score(r)),
                    0 if r["url"] in prev_urls else 1,
                    order.index(r["url"]))

        chosen = sorted(scored, key=rank)[:TARGET_PER_CH]

        # 本频道一条活的都没测到时，单独把旧源再探一遍。
        # 不能把整份旧清单当成"活着"直接续命：那样坏源会一直被保留。
        if not chosen:
            old_live = []
            for u in prev.get(ch, []):
                if u not in ALLOWED[ch]:
                    continue
                print("  RETRY 本轮无活源，单独复测旧源  %s" % u)
                r = probe(u)
                if not r:
                    continue
                if r["kind"] == "hls" and 0 < r["height"] < MIN_HEIGHT:
                    continue
                old_live.append(r)
                break
            if old_live:
                chosen = old_live
                print("  [i] 旧源复测仍有 1 条可用，保留")
            else:
                print("  [!] 旧源也全部连不上，本轮不写入本频道")

        keep = [r["url"] for r in chosen]
        picked[ch] = keep
        if not keep:
            print("  [!] 本频道本轮无一条能连上")
        else:
            hls_n = sum(1 for r in chosen if r["kind"] == "hls")
            slot = sum(1 for r in chosen if r["kind"] == "ts")
            print("  [i] 取 %d 条：HLS %d + 组播 %d" % (len(keep), hls_n, slot))

        # 心跳只记最终选中的源；旧源复测命中的也在 chosen 里，一并写入。
        # 沿用的死频道没有本轮测量，不伪造数值。
        detail[ch] = [{"url": r["url"],
                       "ratio": round(r["ratio"], 2),
                       "play": round(play_ratio(r), 3),
                       "mbps": round(r["mbps"], 2),
                       "encoded_mbps": round(r.get("encoded_mbps", 0), 2),
                       "height": r["height"],
                       "kind": r["kind"],
                       "clock": r["clock"],
                       "verdict": ("unknown" if r["clock"] == "unknown"
                                   else "fast" if play_ratio(r) >= 1.0 else "slow")}
                      for r in chosen]

    # 只有一个频道全死时，另一个频道照常更新；死亡的频道沿用上一轮旧清单，
    # 不让整份 m3u 被一个频道的故障拖成"保留旧文件"。
    final = {}
    for ch in CHANNELS:
        if picked.get(ch):
            final[ch] = picked[ch]
        elif prev.get(ch):
            final[ch] = prev[ch]
            print("\n[KEEP] %s 本轮无活源，沿用上一轮 %d 条旧清单"
                  % (ch, len(prev[ch])))
        else:
            final[ch] = []
    picked = final

    new_text = render(picked, detail)
    total = sum(len(v) for v in picked.values())

    if total == 0:
        if prev and os.path.exists(OUTPUT):
            print("\n[ERROR] 全部源都连不上，保留旧文件不动（旧清单 %d 条照常提供）"
                  % sum(len(v) for v in prev.values()))
            write_heartbeat(prev, False, "ALL DEAD - kept old file", detail)
            return 0          # 退出码保持 0，否则下面提交心跳的步骤会被跳过，60 天保活断掉
        else:
            print("\n[ERROR] 全部源都连不上，且没有旧清单可用")
            write_heartbeat(picked, False, "ALL DEAD - empty", detail)
            return 1

    if new_text == raw:
        print("\n[NO CHANGE] %d 条清单不变，m3u 保持原样" % total)
        write_heartbeat(picked, False, "no change", detail)
        return 0

    with io.open(OUTPUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(new_text)
    print("\n[DONE] %d 条，已更新 %s" % (total, OUTPUT))
    write_heartbeat(picked, True, "updated", detail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
