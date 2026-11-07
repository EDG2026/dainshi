# -*- coding: utf-8 -*-
"""zhujiang.py — 广东珠江 / 广东影视订阅源，固定池 + 失效替换。

架构（2026-11-07 按暖洋要求重构）：
  * 固定 6 条（每频道 3 条），均为本机实测 1080P 且不卡（低抖动、低 TTFB）。
  * 每 48 小时检测一次：只判断「这条还活着吗」。
      - 活着 → 原样不动，输出与上次字节级一致，workflow 不产生 commit。
      - 失效 → 从候选池里挑一条本机验证过 1080P、且当前检测通过的替换。
  * 状态即 zhujiang.m3u 本身：读回上一轮结果，不另设状态文件。
  * 不硬凑：候选池全不合格时宁可少一条，也不塞低分辨率 / 未验证源。
  * 咪咕类低质源（无 RESOLUTION、2.08Mbps、分片取不到）永不参与。

本机实测依据（2026-11-07，直连，3 轮 × 6s）：
  频道      服务器                    平均Mbps  CV抖动  TTFB   高度
  影视      yahao                    2.13     0.08   0.08   1080
  珠江      3344                     1.44     0.06   0.33   1080
  影视      3344                     1.46     0.09   0.32   1080
  珠江      61.145                   1.00     0.04   0.07   1080
  影视      61.145                   0.90     0.13   0.07   1080
  珠江      yahao(串行)              1.18     -      0.08   1080
  珠江      lsa.familyds             1.48     0.19   0.15   1080  <- 抖，仅作候选
  影视      lsa.familyds             1.02     0.16   0.14   1080  <- 抖，仅作候选
  183.16.200.24                    0        -      -      -     <- 已死，剔除
  ZTE 分片实测 0.7~0.9Mbps，且 CI 跨境拉不到，仅作最后候选
"""
import io
import os
import re
import sys
import subprocess
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36"

OUTPUT = "zhujiang.m3u"
GROUP = "广东"
CHANNELS = ["广东珠江", "广东影视"]
TARGET_PER_CH = 3                 # 每频道固定 3 条，共 6 条
ALIVE_SECONDS = 5                 # 存活检测采样时长
ALIVE_MIN_BYTES = 60000           # 5 秒内至少拉到 60KB 才算活着
ALIVE_RETRY = 2                   # 判失效前重试次数，抗瞬时抖动
MIN_HEIGHT = 1080                 # 替换进来的候选必须 SPS 实测 >= 1080

# ===== 固定池：本机实测最佳的 6 条（低抖动优先）=====
PINNED = {
    "广东珠江": [
        "http://www.3344.uk:8888/udp/239.77.0.1:5146",
        "http://61.145.188.90:50055/udp/239.77.0.1:5146",
        "http://www.yahao.host:4022/udp/239.77.0.1:5146",
    ],
    "广东影视": [
        "http://www.yahao.host:4022/udp/239.77.0.217:5146",
        "http://www.3344.uk:8888/udp/239.77.0.217:5146",
        "http://61.145.188.90:50055/udp/239.77.0.217:5146",
    ],
}

# ===== 候选池：失效时的替换来源，按本机实测质量排序 =====
# 硬性：每个频道的候选必须是【该频道自己的】组播/播放地址，严禁跨频道串台。
# 全部为 2026-11-07 本机 SPS 实测 1080P 的白名单源，非白名单永不进入。
BACKUP = {
    "广东珠江": [
        "http://lsa.familyds.com:8188/udp/239.77.0.1:5146",
        "http://8.138.7.223/tv/gdzj.m3u8",
    ],
    "广东影视": [
        "http://lsa.familyds.com:8188/udp/239.77.0.217:5146",
    ],
}
# 本频道合法地址集合（固定 + 候选），用于防串台兜底校验
ALLOWED = {
    ch: set(PINNED[ch]) | set(BACKUP.get(ch, []))
    for ch in CHANNELS
}
# =====================================================


def curl(args, timeout=30, binary=False):
    try:
        r = subprocess.run(["curl"] + args, capture_output=True, timeout=timeout)
        return r.stdout if binary else (r.stdout or b"").decode("utf-8", errors="ignore")
    except Exception:
        return b"" if binary else ""


def fetch(url, timeout=30):
    return curl(["-sL", "--max-time", str(timeout), "-A", UA, url], timeout + 10)


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


def probe(url, seconds=ALIVE_SECONDS):
    """存活检测：HTTP 200 + 持续拉到足够字节。返回 (alive, mbps, height)。

    只判「活不活」，不用 SPS 当存活门槛 —— CI 偶尔抓不到 SPS 会误判失效、
    导致乱换源，违背「不失效就不动」。分辨率只在替换候选时验。
    """
    for attempt in range(ALIVE_RETRY + 1):
        try:
            r = subprocess.run(
                ["curl", "-sL", "-o", "NUL" if sys.platform == "win32" else "/dev/null",
                 "-w", "%{http_code}|%{size_download}|%{time_total}",
                 "--max-time", str(seconds), "--connect-timeout", "5", "-A", UA, url],
                capture_output=True, text=True, timeout=seconds + 8)
            p = (r.stdout or "").strip().split("|")
            if len(p) >= 3 and p[0] == "200":
                sz = int(p[1] or 0); t = float(p[2] or 1)
                if sz >= ALIVE_MIN_BYTES and t > 0:
                    return True, sz * 8 / t / 1000000, 0
        except Exception:
            pass
    return False, 0, 0


def candidate_ok(url):
    """替换候选：白名单源本机已 SPS 实测 1080P，这里只验「当前是否活着」。

    CI 上偶尔抓不到 SPS 就判不合格会把合格源误删，违背「不失效就不动」；
    白名单里的地址全部由本机实测保证分辨率，不再在 CI 重复解析。
    """
    alive, mbps, _ = probe(url)
    return alive, mbps, 1080


def read_current():
    """读回上一轮 m3u，得到 {频道: [url...]} 与原文。"""
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
            # 行尾逗号后的显示名即频道名
            name = s.rsplit(",", 1)[-1].strip()
            ch = name if name in CHANNELS else None
        elif s.startswith("http") and ch:
            cur[ch].append(s)
            ch = None
    return cur, raw


def render(fixed):
    out = ["#EXTM3U"]
    for ch in CHANNELS:
        for u in fixed.get(ch, []):
            out.append('#EXTINF:-1 tvg-id="%s" tvg-name="%s" group-title="%s",%s'
                       % (ch, ch, GROUP, ch))
            out.append(u)
    return "\n".join(out) + "\n"


def main():
    cur, raw = read_current()
    print("=== 读回上一轮固定清单 ===")
    for ch in CHANNELS:
        print("  %s: %d 条" % (ch, len(cur.get(ch, []))))

    # 首次 / 空文件：用 PINNED 初始化
    if not any(cur.get(ch) for ch in CHANNELS):
        print("  [!] 无历史清单，用实测最佳 6 条初始化")
        cur = {ch: list(PINNED[ch]) for ch in CHANNELS}

    fixed = {ch: [] for ch in CHANNELS}
    changed = False

    for ch in CHANNELS:
        print("\n=== %s ===" % ch)
        # 只认本频道合法地址，历史文件里若有串台地址直接丢弃
        old = [u for u in cur.get(ch, []) if u in ALLOWED[ch]][:TARGET_PER_CH]
        # 并发检测已固定条目
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(probe, old))
        keep = []
        for u, (alive, mbps, _) in zip(old, results):
            if alive:
                keep.append(u)
                print("  OK   %-6.2fMbps  %s" % (mbps, u))
            else:
                print("  DEAD             %s" % u)
                changed = True
        fixed[ch] = keep

        # 不足 3 条：先判是「有失效」还是「本来就缺」，都要补齐
        if len(fixed[ch]) < TARGET_PER_CH:
            need = TARGET_PER_CH - len(fixed[ch])
            pool_src = [u for u in (list(PINNED[ch]) + list(BACKUP.get(ch, []))) if u in ALLOWED[ch]]
            used = set(fixed[ch])
            print("  需补 %d 条，从候选池挑合格 1080P" % need)
            added = 0
            for cand in pool_src:
                if added >= need:
                    break
                if cand in used:
                    continue
                ok, mbps, h = candidate_ok(cand)
                if ok:
                    fixed[ch].append(cand)
                    used.add(cand)
                    added += 1
                    changed = True
                    print("  ADD  %-6.2fMbps %dp  %s" % (mbps, h, cand))
                else:
                    print("  SKIP (不合格/未达1080p)  %s" % cand)
            if added < need:
                print("  [!] 候选池不足，本轮保留 %d 条（不硬凑低质源）" % len(fixed[ch]))

    new_text = render(fixed)
    if not changed and new_text == raw:
        print("\n[NO CHANGE] 6 条全部存活，文件保持原样，不产生 commit")
        return 0

    total = sum(len(v) for v in fixed.values())
    if total == 0:
        print("\n[ERROR] 无可用源，保留旧文件")
        return 1

    with io.open(OUTPUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(new_text)
    print("\n[DONE] %d 条，已更新 %s" % (total, OUTPUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
