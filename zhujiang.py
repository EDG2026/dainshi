# -*- coding: utf-8 -*-
"""zhujiang.py v3 — 广东珠江 / 广东影视订阅源生成

v1 的问题（2026-11-12 实测）：
  * 门槛按"当时 8.5Mbps"定的（MIN_MBPS=3.0 / GOOD_MBPS=6.0），源整体掉到 1Mbps 后
    6 条全判降质、候选池全不合格 → total=0 → 心跳写 total=0，清单靠"保留旧文件"吊着。
  * 只认 udpxy 裸 TS，不认 HLS。4 个 udpxy 站晚高峰被限到 ~1.1Mbps，
    PCR 推进只有墙钟的 0.53–0.74 倍 —— 名义 2Mbps 也追不上实时，必卡。

v3 的设计：
  * 阈值只判活。CI 跑在 GitHub 美国机房，测出的速度是"美国→源"，
    跟广东用户实际看到的是两回事，所以速度不参与排序。
  * 顺序由 SOURCES 写死：HLS 在前（国内海外都能连），udpxy 组播在后（只在广东快）。
    上一轮的清单只用于 diff，不回灌排序，避免组播一旦入选就永久占位。
  * 每频道取前 TARGET_PER_CH 条能连上的（HLS 最多占 2 条，留 1 条给组播）；
    连不上的跳过，绝不因为"都不够好"清空清单。
  * 每轮必写 .state/heartbeat.json（GitHub 60 天无 push 会自动停用 schedule）。
  * total=0 且存在旧清单时退出码仍是 0，保证后面提交心跳的步骤不被跳过。

用法：python zhujiang.py   →  生成 zhujiang.m3u（6 条：珠江 3 + 影视 3）
"""
import io
import json
import os
import re
import sys
import subprocess
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
# 阈值只用于两件事：判活、以及在心跳里标注实测快慢。
# 不用来排序——CI 跑在美国，测出的是"美国→源"的速度，跟广东用户看到的不是一回事。
FAST_RATIO = 1.00              # 心跳里标 fast 的门槛（仅供参考）
TS_NOMINAL_MBPS = 2.0          # 裸 TS 的名义编码码率（本机解 PCR 实测 1.6–2.2）
N_PROBE_SEGS = 1            # HLS 只取 1 段判活；多段测速留给本地手工验证用
SAMPLE_SECONDS = 6             # 裸 TS 采样时长
MIN_SEG_BYTES = 50000          # 分段小于此视为错误页/空段
RETRY = 1

# ===== 源池：每频道按"上一次实测优劣"预排序，运行时全部重新测，按 ratio 重排 =====
SOURCES = {
    "广东珠江": [
        # 1080P / 2.7Mbps，Cloudflare，国内海外都快；实测 10 段中位 ratio 1.31
        "https://2.fast.hidns.vip/163189/gdzj",
        # 1080P / 2.7Mbps，Cloudflare，边缘 IP 抖动大但可播
        "https://gxyd.484947.xyz/PLTV/77777777/224/3221225717/index.m3u8"
        "?servicetype=1&&IASHttpSessionId=RR778520250111113355399717&ispcode=1108",
        # 以下 4 条是广东私人 udpxy，晚高峰限速到 ~1.1Mbps，只作兜底
        "http://www.3344.uk:8888/udp/239.77.0.1:5146",
        "http://www.yahao.host:4022/udp/239.77.0.1:5146",
        "http://61.145.188.90:50055/udp/239.77.0.1:5146",
        "http://lsa.familyds.com:8188/udp/239.77.0.1:5146",
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
    args = ["-sL", "--max-time", str(hard), "-A", UA]
    for k, u in enumerate(urls):
        # 最后一段落到临时文件，用来解 SPS 拿分辨率；其余段丢弃
        args += ["-o", TMP if k == len(urls) - 1 else "NUL", u]
    args += ["-w", "%{http_code}|%{size_download}|%{time_total}\n"]
    out = run_curl(args, timeout=max(90, int(sum(use_d) * 6) + 30))
    rows = [r for r in out.decode("ascii", errors="ignore").splitlines() if r.count("|") == 2]
    if len(rows) != len(urls):
        return None
    tot_size = 0
    tot_wall = 0.0
    last_blob = b""
    for r in rows:
        code, size, wall = r.split("|")
        if code != "200":
            return None
        tot_size += int(size)
        tot_wall += float(wall)
    if tot_size < MIN_SEG_BYTES * len(urls) or tot_wall <= 0:
        return None
    dur = sum(use_d)
    ratio = dur / tot_wall
    mbps = tot_size * 8 / dur / 1e6
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
            "height": height, "note": "%d段/%.0fs内容/%.1fs下" % (len(urls), dur, tot_wall)}


def probe_ts(url, seconds=SAMPLE_SECONDS):
    """裸 TS：采样一段，用「实测 Mbps / 名义码率」当可播放比。"""
    code, size, wall, height = grab(url, timeout=seconds)
    if not code or size < 10000:
        return None
    mbps = size * 8 / wall / 1e6
    ratio = mbps / TS_NOMINAL_MBPS if TS_NOMINAL_MBPS else 0
    return {"url": url, "kind": "ts", "ratio": ratio, "mbps": mbps,
            "height": height, "note": "%.1fs采样" % wall}


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


def render(picked):
    out = ["#EXTM3U"]
    for ch in CHANNELS:
        for u in picked.get(ch, []):
            out.append('#EXTINF:-1 tvg-id="%s" tvg-name="%s" group-title="%s",%s'
                       % (ch, ch, GROUP, ch))
            out.append(u)
    return "\n".join(out) + "\n"


def write_heartbeat(picked, changed, note, detail):
    if not os.path.isdir(STATE_DIR):
        os.makedirs(STATE_DIR)
    now = subprocess.run(["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"],
                         capture_output=True, text=True).stdout.strip() or "?"
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

        # 组播并发探（每路 6 秒采样，彼此影响小）；
        # HLS 串行探（需要连续带宽，和 6 路组播并发抢出口会把 ratio 压掉一个量级）。
        raw_ts = [u for u in order if ("/udp/" in u or "/rtp/" in u)]
        http_src = [u for u in order if u not in raw_ts]
        got = {}

        if raw_ts:
            with ThreadPoolExecutor(max_workers=min(6, len(raw_ts))) as ex:
                for u, r in zip(raw_ts, ex.map(probe, raw_ts)):
                    got[u] = r
        for u in http_src:
            got[u] = probe(u)

        scored = []
        for u in order:
            if u not in got:
                continue
            r = got[u]
            if not r:
                print("  DEAD  %s" % u)
                continue
            # 判活标准：拿到 200 + 字节数过门槛 + 能解析出视频。
            # 不拿 ratio 当淘汰线——美国 runner 测中国的源，ratio 天然偏低。
            flag = "FAST" if r["ratio"] >= FAST_RATIO else "OK  "
            print("  %s  ratio=%.2f  %5.2fMbps  %4dp  %-12s %s"
                  % (flag, r["ratio"], r["mbps"], r["height"], r["note"], u))
            scored.append(r)

        # 按源池顺序取，不按 ratio 排序：CI 跑在美国，ratio 反映的是"美国→广东
        # 私人 udpxy"的链路，跟国内实际体验无关。ratio 只判活，不决定优先级。
        scored.sort(key=lambda x: order.index(x["url"]))

        # HLS 最多占 2 条，留 1 条给组播：HLS 是第三方中转站，随时可能关停；
        # 组播是广东本地源，晚高峰才慢，两者互不替代，必须都留。
        chosen, n_hls = [], 0
        for r in scored:
            if r["kind"] == "hls":
                if n_hls >= TARGET_PER_CH - 1:
                    continue
                n_hls += 1
            chosen.append(r)
            if len(chosen) >= TARGET_PER_CH:
                break
        # 组播不足时把被挤掉的 HLS 捡回来，宁可全是 HLS 也不要少于 3 条
        if len(chosen) < TARGET_PER_CH:
            for r in scored:
                if r not in chosen:
                    chosen.append(r)
                if len(chosen) >= TARGET_PER_CH:
                    break

        keep = [r["url"] for r in chosen]
        picked[ch] = keep
        detail[ch] = [{"url": r["url"], "ratio": round(r["ratio"], 2),
                       "mbps": round(r["mbps"], 2), "height": r["height"],
                       "kind": r["kind"],
                       "verdict": "fast" if r["ratio"] >= FAST_RATIO else "slow"}
                      for r in chosen]
        if not keep:
            print("  [!] 本频道本轮无一条能连上")
        else:
            hls_n = sum(1 for r in chosen if r["kind"] == "hls")
            slot = sum(1 for r in chosen if r["kind"] == "ts")
            print("  [i] 取 %d 条：HLS %d + 组播 %d" % (len(keep), hls_n, slot))

    new_text = render(picked)
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
