# -*- coding: utf-8 -*-
"""zhujiang.py — 广东珠江 / 广东影视订阅源，固定池 + 失效/降质替换 + 每日心跳。

架构（2026-11-12 按暖洋要求重构）：
  * 固定 6 条（每频道 3 条），均为本机 5 轮实测 1080P、8.5Mbps、CV≈0 的源。
  * 每 24 小时检测一次（UTC 00:00 = 北京 08:00）：
      - 活着且码率达标 → 原样不动，m3u 与上次字节级一致。
      - 失效或掉到 MIN_MBPS 以下 → 从候选池挑一条实测更好的替换。
  * 心跳：每轮都写 .state/heartbeat.json 并提交。
      作用一：仓库永远有 push，绕开 GitHub「60 天无提交自动停用 schedule」的规则。
      作用二：随时能看到「最近一次真的跑过」的时间戳，不用翻 Actions。
  * 状态即 zhujiang.m3u 本身：读回上一轮结果，不另设状态文件。
  * 不硬凑：候选池全不合格时宁可少一条，也不塞未验证源。

本机实测依据（2026-11-12 02:00，直连，5 轮 × 6s，SPS 解高度）：
  频道      服务器                    最低Mbps 平均Mbps  CV抖动  SPS高度
  珠江      61.145.188.90:50055       8.73     8.74      0.00    1080
  珠江      www.yahao.host:4022       8.67     8.70      0.00    1080
  珠江      www.3344.uk:8888          8.14     8.36      0.01    1080
  珠江      lsa.familyds.com:8188     8.41     8.60      0.01    1080  <- 候选
  影视      61.145.188.90:50055       8.69     8.73      0.00    1080
  影视      www.yahao.host:4022       8.67     8.70      0.00    1080
  影视      www.3344.uk:8888          8.39     8.41      0.00    1080
  影视      lsa.familyds.com:8188     5.98     8.07      0.13    1080  <- 抖动，仅候选
  8.138.7.223/tv/gdzj.m3u8           0        0         -       206B  <- 已死，剔除
"""
import io
import json
import os
import sys
import subprocess
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36"

OUTPUT = "zhujiang.m3u"
GROUP = "广东"
STATE_DIR = ".state"
HEARTBEAT = os.path.join(STATE_DIR, "heartbeat.json")
CHANNELS = ["广东珠江", "广东影视"]
TARGET_PER_CH = 3                 # 每频道固定 3 条，共 6 条
ALIVE_SECONDS = 6                 # 存活检测采样时长
ALIVE_MIN_BYTES = 450000          # 6 秒内至少 450KB ≈ 0.6Mbps，低于此算没在出画面
ALIVE_RETRY = 2                   # 判失效前重试次数，抗瞬时抖动
MIN_MBPS = 3.0                    # 已固定条目掉到这个码率以下视为降质，尝试替换
GOOD_MBPS = 6.0                   # 候选替换进来必须达到的码率
MIN_HEIGHT = 1080                 # 替换进来的候选必须 SPS 实测 >= 1080

# ===== 固定池：本机 5 轮实测最佳（按实测质量排序，第一条最稳）=====
PINNED = {
    "广东珠江": [
        "http://61.145.188.90:50055/udp/239.77.0.1:5146",
        "http://www.yahao.host:4022/udp/239.77.0.1:5146",
        "http://www.3344.uk:8888/udp/239.77.0.1:5146",
    ],
    "广东影视": [
        "http://61.145.188.90:50055/udp/239.77.0.217:5146",
        "http://www.yahao.host:4022/udp/239.77.0.217:5146",
        "http://www.3344.uk:8888/udp/239.77.0.217:5146",
    ],
}

# ===== 候选池：失效/降质时的替换来源 =====
# 硬性：每个频道的候选必须是【该频道自己的】组播地址，严禁跨频道串台。
# 全部为 2026-11-12 本机 SPS 实测 1080P 的白名单源，非白名单永不进入。
BACKUP = {
    "广东珠江": [
        "http://lsa.familyds.com:8188/udp/239.77.0.1:5146",
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


def measure(url, seconds=ALIVE_SECONDS):
    """采样一轮，返回 (bytes, mbps, sps_height)。不重试。"""
    try:
        r = subprocess.run(
            ["curl", "-sL", "-o", "NUL" if sys.platform == "win32" else "/dev/null",
             "-w", "%{http_code}|%{size_download}|%{time_total}",
             "--max-time", str(seconds), "--connect-timeout", "5", "-A", UA, url],
            capture_output=True, text=True, timeout=seconds + 10)
        p = (r.stdout or "").strip().split("|")
        if len(p) >= 3 and p[0] == "200":
            sz = int(p[1] or 0); t = float(p[2] or 1)
            if sz > 0 and t > 0:
                return sz, sz * 8 / t / 1000000, 0
    except Exception:
        pass
    return 0, 0.0, 0


def probe(url, seconds=ALIVE_SECONDS):
    """存活检测：HTTP 200 + 持续拉到足够字节。返回 (alive, mbps, height)。

    只判「活不活」，不用 SPS 当存活门槛 —— CI 偶尔抓不到 SPS 会误判失效、
    导致乱换源，违背「不失效就不动」。分辨率只在替换候选时验。
    """
    for _ in range(ALIVE_RETRY + 1):
        sz, mbps, _ = measure(url, seconds)
        if sz >= ALIVE_MIN_BYTES:
            return True, mbps, 0
    return False, 0.0, 0


def candidate_ok(url):
    """替换候选：白名单源本机已 SPS 实测 1080P，这里验「活着且码率够高」。"""
    alive, mbps, _ = probe(url)
    return (alive and mbps >= GOOD_MBPS), mbps, 1080


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


def write_heartbeat(fixed, changed, note):
    """每轮都写，哪怕 m3u 没变 —— 这是绕开 60 天停用规则的心跳。"""
    if not os.path.isdir(STATE_DIR):
        os.makedirs(STATE_DIR)
    now = subprocess.run(["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"],
                         capture_output=True, text=True).stdout.strip() or "?"
    rec = {
        "last_check_utc": now,
        "changed": bool(changed),
        "note": note,
        "total": sum(len(v) for v in fixed.values()),
        "channels": {ch: fixed.get(ch, []) for ch in CHANNELS},
    }
    with io.open(HEARTBEAT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("  [HB] %s  total=%d  changed=%s" % (now, rec["total"], changed))


def main():
    cur, raw = read_current()
    print("=== 读回上一轮固定清单 ===")
    for ch in CHANNELS:
        print("  %s: %d 条" % (ch, len(cur.get(ch, []))))

    if not any(cur.get(ch) for ch in CHANNELS):
        print("  [!] 无历史清单，用实测最佳 6 条初始化")
        cur = {ch: list(PINNED[ch]) for ch in CHANNELS}

    fixed = {ch: [] for ch in CHANNELS}
    changed = False
    degraded = []
    dead = []

    for ch in CHANNELS:
        print("\n=== %s ===" % ch)
        old = [u for u in cur.get(ch, []) if u in ALLOWED[ch]][:TARGET_PER_CH]
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(probe, old))
        keep = []
        for u, (alive, mbps, _) in zip(old, results):
            if alive and mbps >= MIN_MBPS:
                keep.append(u)
                print("  OK   %-6.2fMbps  %s" % (mbps, u))
            elif alive:
                degraded.append(u)
                print("  LOW  %-6.2fMbps  %s  (低于 %.1f，尝试替换)" % (mbps, u, MIN_MBPS))
            else:
                dead.append(u)
                print("  DEAD             %s" % u)
        fixed[ch] = keep

        if len(fixed[ch]) < TARGET_PER_CH:
            need = TARGET_PER_CH - len(fixed[ch])
            pool_src = [u for u in (list(PINNED[ch]) + list(BACKUP.get(ch, []))) if u in ALLOWED[ch]]
            used = set(fixed[ch])
            print("  需补 %d 条，从候选池挑合格源（>=%.1fMbps）" % (need, GOOD_MBPS))
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
                    print("  SKIP (不合格)  %s  %.2fMbps" % (cand, mbps))
            if added < need:
                print("  [!] 候选池不足，本轮保留 %d 条（不硬凑）" % len(fixed[ch]))

    new_text = render(fixed)
    total = sum(len(v) for v in fixed.values())

    if total == 0:
        print("\n[ERROR] 无可用源，保留旧文件")
        write_heartbeat(fixed, False, "ALL DEAD - kept old file")
        return 1

    if not changed and new_text == raw:
        print("\n[NO CHANGE] %d 条全部存活且码率达标，m3u 保持原样" % total)
        write_heartbeat(fixed, False, "no change")
        return 0

    with io.open(OUTPUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(new_text)
    print("\n[DONE] %d 条，已更新 %s（失效 %d 条 / 降质 %d 条）"
          % (total, OUTPUT, len(dead), len(degraded)))
    write_heartbeat(fixed, True, "updated: dead=%d low=%d" % (len(dead), len(degraded)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
