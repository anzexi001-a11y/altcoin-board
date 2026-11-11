#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""altcoin_radar.py — 山寨币合约「仓位结构雷达」

只用 Python 标准库，不需要 pip install，不需要 API Key。

数据源: Binance USDT-M 永续合约公开接口
输出  : output/radar_<时间>.html   自包含看板, 直接截图发推
        output/radar_<时间>.csv    数据快照, Excel 可直接打开
        output/tweets_<时间>.md    推文草稿

用法:
    python altcoin_radar.py                     抓实时数据, 生成全部输出
    python altcoin_radar.py --demo               离线演示, 用内置样本数据
    python altcoin_radar.py --top 30              只看成交额前 30 的山寨币
    python altcoin_radar.py --symbols PEPEUSDT,WIFUSDT
    python altcoin_radar.py --proxy http://127.0.0.1:7890   走本地代理

核心框架: 价格 × 持仓量 (OI) 四象限
    价涨 + OI 增  ->  新多进场 (趋势健康, 拥挤度上升)
    价涨 + OI 减  ->  空头平仓 (逼空行情, 燃料将尽)
    价跌 + OI 增  ->  新空进场 (下跌有燃料, 可能被反杀)
    价跌 + OI 减  ->  多头离场 (去杠杆, 可能接近阶段底)
"""

import argparse
import bisect
import csv
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

FAPI = "https://fapi.binance.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36 altcoin-radar/1.0")
TIMEOUT = 20
CST = timezone(timedelta(hours=8))

# 参照币, 不参与山寨币排名, 只放表头做背景
BENCH = ["BTCUSDT", "ETHUSDT"]

_OPENER = None


# --------------------------------------------------------------------------
# 网络
# --------------------------------------------------------------------------
def init_net(proxy=None):
    global _OPENER
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        _OPENER = urllib.request.build_opener(handler)
    else:
        _OPENER = urllib.request.build_opener()


def _get(path, params=None, retries=3):
    url = FAPI + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    last = None
    for attempt in range(retries):
        try:
            with _OPENER.open(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (418, 429, 451) or e.code >= 500:
                time.sleep(2 + attempt * 2)
                continue
            raise
        except Exception as e:  # 网络抖动 / 超时 / DNS
            last = e
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("请求失败: %s (%s)" % (url, last))


def _safe(path, params):
    """单币某个指标拿不到就返回 None, 不能拖垮整张表"""
    try:
        return _get(path, params, retries=2)
    except Exception:
        return None


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def fnum(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def pct(cur, prev):
    if prev is None or cur is None or prev == 0:
        return None
    return (cur - prev) / prev * 100.0


def pct_rank(sorted_vals, v):
    if not sorted_vals or v is None:
        return 0.0
    n = len(sorted_vals)
    if n < 2:
        return 0.0
    idx = bisect.bisect_left(sorted_vals, v)
    return idx / float(n - 1)


def sgn_pct(v, digits=2):
    if v is None:
        return "—"
    return ("+" if v > 0 else "") + ("%.*f%%" % (digits, v))


def money(v):
    if v is None:
        return "—"
    if v >= 1e9:
        return "%.2fB" % (v / 1e9)
    if v >= 1e6:
        return "%.1fM" % (v / 1e6)
    if v >= 1e3:
        return "%.1fK" % (v / 1e3)
    return "%.2f" % v


def price_str(p):
    if p is None:
        return "—"
    if p >= 1000:
        return "%.1f" % p
    if p >= 1:
        return "%.4f" % p
    if p >= 0.01:
        return "%.5f" % p
    return "%.8f" % p


def quadrant(px_chg, oi_chg):
    if px_chg is None or oi_chg is None:
        return ("未知", "数据缺失", "n/a")
    if px_chg >= 0 and oi_chg >= 0:
        return ("价涨+OI增", "新多进场", "q1")
    if px_chg >= 0 and oi_chg < 0:
        return ("价涨+OI减", "空头平仓", "q2")
    if px_chg < 0 and oi_chg >= 0:
        return ("价跌+OI增", "新空进场", "q3")
    return ("价跌+OI减", "多头离场", "q4")


def crowd_tag(funding_ann, oi_chg_24, px_chg_24):
    """拥挤度标签: 用资金费率年化 + 价格方向判断哪一边在付费"""
    if funding_ann is None:
        return "—"
    if funding_ann >= 120:
        return "多头极度拥挤"
    if funding_ann >= 60:
        return "多头拥挤"
    if funding_ann <= -60:
        return "空头拥挤"
    if funding_ann < 0 and (oi_chg_24 or 0) > 0 and (px_chg_24 or 0) < 0:
        return "空头加仓且付费"
    return "中性"


# --------------------------------------------------------------------------
# 抓取
# --------------------------------------------------------------------------
def fetch_universe():
    info = _get("/fapi/v1/exchangeInfo")
    perp = set()
    for s in info.get("symbols", []):
        if (s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == "USDT"
                and s.get("status") == "TRADING"):
            perp.add(s["symbol"])
    tickers = {t["symbol"]: t for t in _get("/fapi/v1/ticker/24hr") if t["symbol"] in perp}
    prem = {p["symbol"]: p for p in _get("/fapi/v1/premiumIndex") if p["symbol"] in perp}
    return perp, tickers, prem


def fetch_one(symbol, tickers, prem, light=False):
    row = {"symbol": symbol, "base": symbol[:-4].replace("USDT", "")}
    t = tickers.get(symbol) or {}
    p = prem.get(symbol) or {}

    row["price"] = fnum(t.get("lastPrice"))
    row["px_chg_24"] = fnum(t.get("priceChangePercent"))
    row["quote_vol"] = fnum(t.get("quoteVolume"))

    fr = fnum(p.get("lastFundingRate"))
    row["funding_rate"] = fr
    row["funding_ann"] = fr * 3 * 365 * 100 if fr is not None else None   # 年化 %
    row["mark"] = fnum(p.get("markPrice"))
    nft = fnum(p.get("nextFundingTime"))
    row["next_funding"] = (
        datetime.fromtimestamp(nft / 1000, CST).strftime("%H:%M") if nft else None
    )

    # ---- OI 历史: 25 根 1h K, 覆盖 24 小时 ----
    row["oi_usd"] = None
    row["oi_1h"] = row["oi_4h"] = row["oi_24h"] = None
    row["oi_contracts_24h"] = None
    row["px_chg_oi"] = None          # 与 OI 序列完全同窗口的隐含价格变化
    row["supply"] = None
    hist = _safe("/futures/data/openInterestHist",
                 {"symbol": symbol, "period": "1h", "limit": 25})
    if isinstance(hist, list) and len(hist) >= 2:
        pairs = []
        for h in hist:
            c = fnum(h.get("sumOpenInterest"))
            v = fnum(h.get("sumOpenInterestValue"))
            if c and v:
                pairs.append((c, v))
        if len(pairs) >= 2:
            cs = [p[0] for p in pairs]
            vs = [p[1] for p in pairs]
            cur = vs[-1]
            row["oi_usd"] = cur
            row["oi_1h"] = pct(cur, vs[-2])
            row["oi_4h"] = pct(cur, vs[-5]) if len(vs) >= 5 else None
            row["oi_24h"] = pct(cur, vs[0])
            row["oi_series"] = vs
            # 名义 OI 会被价格污染: 必须同时记录"合约张数"的变化
            row["oi_contracts_24h"] = pct(cs[-1], cs[0])
            # 隐含价 = 名义 ÷ 张数。用它算价格变化, 就和持仓变化完全同窗口,
            # 避免"ticker 滚动24h"与"整点快照"错配(这是最隐蔽的错源)。
            row["px_chg_oi"] = pct(vs[-1] / cs[-1], vs[0] / cs[0])
            row["supply"] = fnum(hist[-1].get("CMCCirculatingSupply"))

    # ---- 多空比 / 大户持仓比 / 主动买卖 ----
    # 筛选模式下这些不是必需项: 每币少 3 次请求, 才扫得动全市场
    row["ls_account"] = None
    row["ls_top"] = None
    row["taker"] = None
    if not light:
        ls = _safe("/futures/data/globalLongShortAccountRatio",
                   {"symbol": symbol, "period": "1h", "limit": 1})
        if isinstance(ls, list) and ls:
            row["ls_account"] = fnum(ls[0].get("longShortRatio"))

        top = _safe("/futures/data/topLongShortPositionRatio",
                    {"symbol": symbol, "period": "1h", "limit": 1})
        if isinstance(top, list) and top:
            row["ls_top"] = fnum(top[0].get("longShortRatio"))

        tk = _safe("/futures/data/takerlongshortRatio",
                   {"symbol": symbol, "period": "1h", "limit": 1})
        if isinstance(tk, list) and tk:
            row["taker"] = fnum(tk[0].get("buySellRatio"))

    # ---- 杠杆拥挤度: 持仓 / 24h 成交 ----
    if row["oi_usd"] and row["quote_vol"]:
        row["leverage"] = row["oi_usd"] / row["quote_vol"]
    else:
        row["leverage"] = None

    # 四象限用"同窗口"的 (价格, 张数) 配对, 而不是 (滚动24h价, 名义OI)
    row["quad"], row["quad_desc"], row["quad_key"] = quadrant(
        row["px_chg_oi"], row["oi_contracts_24h"])
    row["crowd"] = crowd_tag(row["funding_ann"], row["oi_contracts_24h"], row["px_chg_oi"])
    return row


def fetch_rows(top, symbols, workers, light=False, rank_by="volume", minvol=0.0):
    perp, tickers, prem = fetch_universe()

    if symbols:
        cands = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        dropped = [c for c in cands if c not in perp]
        if dropped:
            print("  [跳过] 这些不是有效的 Binance USDT-M 永续合约: %s" % ", ".join(dropped))
            print("         提示: 很多小币在合约市场带 1000 前缀, 如 1000PEPEUSDT")
        cands = [c for c in cands if c in perp]
    elif rank_by == "change":
        # 涨跌榜模式: 涨跌幅两个方向各取前 top 名
        pool = []
        skipped = 0
        for s in tickers:
            if s in BENCH:
                continue
            qv = fnum(tickers[s].get("quoteVolume"), 0) or 0
            if qv < minvol:
                skipped += 1
                continue
            pool.append((s, fnum(tickers[s].get("priceChangePercent"), 0) or 0))
        print("  成交额 >= %s 的合约 %d 个 (剔除 %d 个低流动性)"
              % (money(minvol), len(pool), skipped))
        up = sorted(pool, key=lambda x: x[1], reverse=True)[:top]
        down = sorted(pool, key=lambda x: x[1])[:top]
        seen = set()
        cands = []
        for s, _ in up + down:
            if s not in seen:
                seen.add(s)
                cands.append(s)
        print("  涨跌榜各取前 %d 名 -> 去重后 %d 个" % (top, len(cands)))
    else:
        pool = [s for s in tickers if s not in BENCH
                and (fnum(tickers[s].get("quoteVolume"), 0) or 0) >= minvol]
        pool.sort(key=lambda s: fnum(tickers[s].get("quoteVolume"), 0) or 0, reverse=True)
        cands = pool[:top]

    if not cands:
        raise SystemExit("没有可分析的合约, 检查 --symbols 拼写")

    print("  扫描 %d 个永续合约 ..." % len(cands))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        rows = list(ex.map(lambda s: fetch_one(s, tickers, prem, light), cands))

    bench = [fetch_one(b, tickers, prem) for b in BENCH if b in perp]
    return rows, bench


# --------------------------------------------------------------------------
# 打分
# --------------------------------------------------------------------------
def score_rows(rows):
    def col(key):
        return sorted([r[key] for r in rows if r.get(key) is not None])

    s_oi = col("oi_24h")
    s_px = col("px_chg_24")
    s_fr = sorted([abs(r["funding_ann"]) for r in rows if r.get("funding_ann") is not None])
    s_lev = col("leverage")

    for r in rows:
        a = pct_rank(s_oi, abs(r["oi_24h"]) if r["oi_24h"] is not None else None)
        b = pct_rank(s_px, abs(r["px_chg_24"]) if r["px_chg_24"] is not None else None)
        c = pct_rank(s_fr, abs(r["funding_ann"]) if r["funding_ann"] is not None else None)
        d = pct_rank(s_lev, r["leverage"])
        r["score"] = round(100 * (0.40 * a + 0.25 * b + 0.20 * c + 0.15 * d), 1)
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


# --------------------------------------------------------------------------
# 演示数据 (离线也能看到成品长什么样)
# --------------------------------------------------------------------------
def compute_yao(rows):
    """妖币三件套: 流通市值 / OI占市值 / 换手率, 以及综合妖币分。

    妖币分 = |24h涨跌幅%| × OI占市值%
    逻辑: 涨跌幅大说明有资金在推; OI占市值高说明合约盘子相对真实盘子过大,
          两者相乘就是在筛"盘子薄但杠杆重"的标的 —— 这类最容易双向插针。
    注意: 供给为 0 的是代币化股票/ETF, 不是加密币, mcap 记为 None 以便剔除。
    """
    for r in rows:
        px, sup, oi = r.get("price"), r.get("supply"), r.get("oi_usd")
        r["mcap"] = px * sup if (px and sup) else None
        if r.get("mcap"):
            r["oi_mcap"] = (oi / r["mcap"]) if oi else None
            r["turnover"] = (r["quote_vol"] / r["mcap"]) if r.get("quote_vol") else None
        else:
            r["oi_mcap"] = None
            r["turnover"] = None
        # 妖币分用同窗口价格, 否则分子分母口径不一致
        chg = r.get("px_chg_oi")
        if chg is None:
            chg = r.get("px_chg_24") or 0
        chg = abs(chg)
        om = r.get("oi_mcap")
        r["yao"] = round(chg * om * 100, 1) if om else 0.0
    return rows


def demo_rows():
    # symbol, price, px24, vol, fr, oi1h, oi4h, oi24h, ls_acc, ls_top, taker, mcap, oi_frac
    raw = [
        ("PEPEUSDT", 0.00001082, 14.3, 1.92e9, 0.00042, 3.1, 9.8, 41.2, 2.31, 1.42, 1.18, 2.2e9, 0.30),
        ("WIFUSDT", 1.8340, 9.7, 4.10e8, 0.00031, 1.4, 5.2, 28.6, 1.88, 1.21, 1.09, 1.8e9, 0.22),
        ("FLOKIUSDT", 0.0001724, 6.2, 2.30e8, 0.00012, 0.6, 2.1, 12.4, 1.52, 1.05, 1.02, 1.6e9, 0.15),
        ("1000BONKUSDT", 0.02315, -8.4, 3.60e8, -0.00018, -1.9, -6.4, 19.7, 0.94, 0.81, 0.88, 1.7e9, 0.18),
        ("ORDIUSDT", 38.42, -12.1, 5.80e8, 0.00008, -4.2, -11.3, 33.8, 0.86, 0.72, 0.79, 8.0e8, 0.28),
        ("INJUSDT", 21.06, 3.4, 1.50e8, 0.00095, 0.4, 1.2, -9.6, 1.71, 1.34, 1.05, 2.1e9, 0.12),
        ("SEIUSDT", 0.4128, -2.8, 9.20e7, -0.00104, -0.3, -1.1, 8.1, 0.79, 0.68, 0.91, 2.4e9, 0.06),
        ("TIAUSDT", 6.914, 5.1, 1.10e8, 0.00022, 0.9, 3.7, -14.2, 1.44, 1.18, 1.03, 1.3e9, 0.05),
        ("SUIUSDT", 3.2840, 11.6, 7.40e8, 0.00067, 2.2, 6.8, 22.9, 2.05, 1.51, 1.12, 11.8e9, 0.04),
        ("APTUSDT", 9.128, -5.6, 1.30e8, 0.00004, -0.8, -3.2, 6.3, 1.12, 0.94, 0.96, 5.6e9, 0.05),
    ]
    rows = []
    for (sym, px, pc, qv, fr, o1, o4, o24, la, lt, tk, mcap, frac) in raw:
        oi = mcap * frac                      # 持仓名义 = 市值 × 占比
        # 名义变化里剔掉价格影响, 得到"张数"变化 —— 与真实数据的算法一致
        oi_c = ((1 + o24 / 100.0) / (1 + pc / 100.0) - 1) * 100
        r = {
            "symbol": sym, "base": sym.replace("USDT", ""), "price": px,
            "px_chg_24": pc, "px_chg_oi": pc, "quote_vol": qv, "funding_rate": fr,
            "funding_ann": fr * 3 * 365 * 100, "mark": px,
            "next_funding": "16:00", "oi_usd": oi,
            "oi_1h": o1, "oi_4h": o4, "oi_24h": o24,
            "oi_contracts_24h": round(oi_c, 1),
            "supply": mcap / px if px else None,
            "ls_account": la, "ls_top": lt, "taker": tk,
            "leverage": oi / qv if qv else None,
        }
        r["quad"], r["quad_desc"], r["quad_key"] = quadrant(pc, oi_c)
        r["crowd"] = crowd_tag(r["funding_ann"], oi_c, pc)
        rows.append(r)
    bench = []
    for sym, px, pc, qv, fr in [("BTCUSDT", 96420.0, 1.8, 2.1e10, 0.0001),
                                ("ETHUSDT", 3320.5, 2.4, 9.4e9, 0.00013)]:
        bench.append({"symbol": sym, "base": sym.replace("USDT", ""), "price": px,
                      "px_chg_24": pc, "quote_vol": qv, "funding_rate": fr,
                      "funding_ann": fr * 3 * 365 * 100, "oi_usd": qv * 0.4,
                      "oi_1h": 0.2, "oi_4h": 1.1, "oi_24h": 4.2,
                      "ls_account": 1.9, "ls_top": 1.2, "taker": 1.04,
                      "leverage": 0.4, "quad": "价涨+OI增", "quad_desc": "新多进场",
                      "quad_key": "q1", "crowd": "中性", "mark": px, "next_funding": "16:00"})
    print("  [演示模式] 使用内置样本数据, 非真实行情")
    return rows, bench


# --------------------------------------------------------------------------
# 输出: HTML 看板
# --------------------------------------------------------------------------
CSS = """
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0b0e11;color:#eaecef;font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
     padding:28px 20px 40px;line-height:1.5}
.wrap{max-width:1360px;margin:0 auto}
.top{display:flex;flex-wrap:wrap;align-items:flex-end;gap:14px;border-bottom:1px solid #232830;padding-bottom:16px;margin-bottom:20px}
h1{font-size:24px;font-weight:700;letter-spacing:.5px}
h1 span{color:#f0b90b}
.stamp{color:#848e9c;font-size:13px;margin-left:auto;text-align:right}
.bench{display:flex;gap:18px;flex-wrap:wrap;margin-bottom:18px}
.bcard{background:#161a1e;border:1px solid #232830;border-radius:10px;padding:10px 16px;display:flex;gap:12px;align-items:baseline}
.bcard b{font-size:15px}
.stats{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:22px}
.stat{background:#161a1e;border:1px solid #232830;border-radius:10px;padding:12px 18px;min-width:130px}
.stat i{display:block;color:#848e9c;font-size:12px;font-style:normal;margin-bottom:4px}
.stat b{font-size:19px;font-weight:600}
h2{font-size:15px;color:#848e9c;font-weight:600;margin:26px 0 12px;letter-spacing:.5px}
.quad{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.qc{background:#161a1e;border:1px solid #232830;border-radius:10px;padding:14px 16px}
.qc.q1{border-left:3px solid #0ecb81}
.qc.q2{border-left:3px solid #f0b90b}
.qc.q3{border-left:3px solid #f6465d}
.qc.q4{border-left:3px solid #6c7a89}
.qc h3{font-size:14px;margin-bottom:2px}
.qc em{font-style:normal;color:#848e9c;font-size:12px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.chip{background:#22262b;border-radius:5px;padding:3px 8px;font-size:12px;color:#cdd3da}
.chip b{color:#f0b90b;font-weight:600;margin-left:5px}
table{width:100%;border-collapse:collapse;background:#161a1e;border-radius:10px;overflow:hidden;font-size:13px}
thead th{background:#1e2329;color:#848e9c;font-weight:600;text-align:right;padding:10px 9px;white-space:nowrap;font-size:12px}
thead th:first-child,tbody td:first-child{text-align:left;padding-left:14px}
tbody td{padding:9px;text-align:right;border-top:1px solid #232830;white-space:nowrap;font-variant-numeric:tabular-nums}
tbody tr:hover{background:#1c2126}
.sym{font-weight:600}
.sym small{color:#5e6673;font-weight:400;margin-left:4px;font-size:11px}
.up{color:#0ecb81}.down{color:#f6465d}.mut{color:#848e9c}
.tag{display:inline-block;padding:2px 7px;border-radius:4px;font-size:11px}
.t1{background:rgba(14,203,129,.14);color:#0ecb81}
.t2{background:rgba(240,185,11,.14);color:#f0b90b}
.t3{background:rgba(246,70,93,.14);color:#f6465d}
.t4{background:rgba(108,122,137,.16);color:#9aa6b2}
.bar{height:4px;background:#232830;border-radius:2px;width:52px;display:inline-block;vertical-align:middle;margin-right:7px}
.bar i{display:block;height:4px;border-radius:2px;background:#f0b90b}
footer{margin-top:26px;padding-top:14px;border-top:1px solid #232830;color:#5e6673;font-size:12px;line-height:1.8}
@media(max-width:820px){.quad{grid-template-columns:1fr}body{padding:16px 10px}}
"""

QUAD_META = [
    ("q1", "价涨 + OI 增", "新多进场 · 趋势健康但拥挤度上升"),
    ("q2", "价涨 + OI 减", "空头平仓 · 逼空行情，燃料将尽"),
    ("q3", "价跌 + OI 增", "新空进场 · 下跌有燃料，注意反杀"),
    ("q4", "价跌 + OI 减", "多头离场 · 去杠杆，可能接近阶段底"),
]


def cell_chg(v):
    if v is None:
        return '<td class="mut">—</td>'
    cls = "up" if v > 0 else ("down" if v < 0 else "mut")
    return '<td class="%s">%s</td>' % (cls, sgn_pct(v))


def build_html(rows, bench, stamp, demo=False):
    n = len(rows)
    hot = [r for r in rows if (r.get("funding_ann") or 0) >= 60 or (r.get("funding_ann") or 0) <= -60]
    levs = [r["leverage"] for r in rows if r.get("leverage")]
    avg_lev = sum(levs) / len(levs) if levs else None

    o = []
    o.append('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">')
    o.append('<meta name="viewport" content="width=device-width,initial-scale=1">')
    o.append('<title>山寨币合约仓位结构雷达 %s</title><style>%s</style></head><body>' % (html.escape(stamp), CSS))
    o.append('<div class="wrap">')

    # 头部
    o.append('<div class="top"><div><h1>山寨币合约 · <span>仓位结构雷达</span></h1>')
    o.append('<div style="color:#848e9c;font-size:13px;margin-top:4px">价格 × 持仓量四象限 · 资金费率拥挤度 · 大户持仓方向</div></div>')
    o.append('<div class="stamp">%s<br>Binance USDT-M 永续 · 公开数据</div></div>' % html.escape(stamp))

    # 参照币
    o.append('<div class="bench">')
    for b in bench:
        cls = "up" if (b.get("px_chg_24") or 0) >= 0 else "down"
        o.append('<div class="bcard"><b>%s</b><span>%s</span><span class="%s">%s</span>'
                 '<span class="mut">费率年化 %s</span></div>'
                 % (html.escape(b["base"]), price_str(b.get("price")), cls,
                    sgn_pct(b.get("px_chg_24")), sgn_pct(b.get("funding_ann"), 0)))
    o.append('</div>')

    # 统计条
    o.append('<div class="stats">')
    o.append('<div class="stat"><i>扫描合约</i><b>%d</b></div>' % n)
    o.append('<div class="stat"><i>费率极端 (|年化|≥60%%)</i><b>%d</b></div>' % len(hot))
    o.append('<div class="stat"><i>平均杠杆 (OI/24h成交)</i><b>%s</b></div>'
             % ("%.2fx" % avg_lev if avg_lev else "—"))
    top1 = rows[0] if rows else None
    if top1:
        o.append('<div class="stat"><i>异动榜首</i><b>%s</b></div>' % html.escape(top1["base"]))
    o.append('</div>')

    # 四象限
    o.append('<h2>四象限分布</h2><div class="quad">')
    for key, title, desc in QUAD_META:
        members = [r for r in rows if r.get("quad_key") == key]
        members.sort(key=lambda r: r["score"], reverse=True)
        o.append('<div class="qc %s"><h3>%s</h3><em>%s</em><div class="chips">' % (key, title, desc))
        if members:
            for r in members[:12]:
                v = r.get("oi_24h")
                o.append('<span class="chip">%s<b>OI %s</b></span>'
                         % (html.escape(r["base"]), sgn_pct(v, 0) if v is not None else "—"))
        else:
            o.append('<span class="chip mut">空</span>')
        o.append('</div></div>')
    o.append('</div>')

    # 主表
    o.append('<h2>明细</h2><table><thead><tr>')
    for h in ["币种", "价格", "24h价·同窗", "OI 24h·张", "OI 24h·名义", "OI 规模", "流通市值",
              "OI/市值", "换手", "费率 8h", "年化", "大户持仓比", "杠杆", "象限", "妖币分"]:
        o.append('<th>%s</th>' % h)
    o.append('</tr></thead><tbody>')

    for r in rows:
        o.append('<tr><td class="sym">%s<small>%s</small></td>' % (html.escape(r["base"]), "PERP"))
        o.append('<td>%s</td>' % price_str(r.get("price")))
        o.append(cell_chg(r.get("px_chg_oi") if r.get("px_chg_oi") is not None
                          else r.get("px_chg_24")))
        o.append(cell_chg(r.get("oi_contracts_24h")))
        o.append(cell_chg(r.get("oi_24h")))
        o.append('<td class="mut">%s</td>' % money(r.get("oi_usd")))
        o.append('<td class="mut">%s</td>' % money(r.get("mcap")))
        om = r.get("oi_mcap")
        o.append('<td class="%s">%s</td>'
                 % ("up" if (om or 0) >= 0.3 else "mut",
                    "%.1f%%" % (om * 100) if om is not None else "—"))
        tv = r.get("turnover")
        o.append('<td class="%s">%s</td>'
                 % ("up" if (tv or 0) >= 1 else "mut",
                    "%.2fx" % tv if tv is not None else "—"))
        fr = r.get("funding_rate")
        frs = "%.4f%%" % (fr * 100) if fr is not None else "—"
        o.append('<td class="%s">%s</td>' % ("up" if (fr or 0) > 0 else "down", frs))
        ann = r.get("funding_ann")
        acls = "up" if (ann or 0) >= 60 else ("down" if (ann or 0) <= -60 else "mut")
        o.append('<td class="%s">%s</td>' % (acls, sgn_pct(ann, 0)))
        lt = r.get("ls_top")
        o.append('<td class="%s">%s</td>'
                 % ("up" if (lt or 1) > 1 else "down", "%.2f" % lt if lt else "—"))
        lv = r.get("leverage")
        o.append('<td class="mut">%s</td>' % ("%.2fx" % lv if lv else "—"))
        qk = r.get("quad_key")
        if qk not in ("q1", "q2", "q3", "q4"):
            qk = "q4"
        o.append('<td><span class="tag t%s">%s</span></td>' % (qk[1], html.escape(r.get("quad", "—"))))
        yv = r.get("yao")
        o.append('<td><span class="bar"><i style="width:%d%%"></i></span>%s</td>'
                 % (min(100, int(yv or 0)), ("%.0f" % yv) if yv is not None else "—"))
        o.append('</tr>')
    o.append('</tbody></table>')

    o.append('<footer>四象限说明：持仓上升说明有新仓位进场，下降说明有仓位离场；'
             '和价格方向组合，可以区分「趋势由新钱推动」还是「由平仓推动」。<br>'
             '<b>所有指标取自同一时间窗口</b>：价格变化由持仓序列的隐含价（名义 ÷ 张数）算出，'
             '与持仓变化完全对齐。交易软件里的滚动 24 小时涨跌幅与这里会有差异，属正常。<br>'
             '<b>OI 张数 vs OI 名义</b>：名义 = 张数 × 价格。价格剧烈波动时名义会被价格污染，'
             '判断仓位到底增了还是减了请看张数；名义与张数不一致的差额，就是价格贡献的部分。<br>'
             '<b>流通市值</b> = 流通供给 × 合约价。供给取 CoinMarketCap 口径（Binance 接口内置字段），'
             '与 CoinGecko 等其他来源可能相差数倍，跨源比较必须注明口径。<br>'
             '<b>OI/市值</b> = 持仓名义 ÷ 流通市值，越高说明合约盘子相对真实盘子越大；'
             '<b>换手</b> = 24h 成交额 ÷ 流通市值；'
             '<b>妖币分</b> = 同窗口涨跌幅绝对值 × OI占市值比例。<br>'
             '<b>注意</b>：张数是净额，净额不变不代表没人止损——净额不变 + 高换手，'
             '恰恰说明多空在大量换手对冲掉了。单看净持仓无法判断谁离场，需要配合主动买卖比。'
             '无流通供给的标的是代币化股票/ETF，已从妖币排序中剔除。'
             '数据来源 Binance 公开接口，存在延迟与口径差异，仅用于结构观察，'
             '<b>不构成任何投资建议</b>。'
             '%s</footer>' % ("　·　当前为演示数据" if demo else ""))
    o.append('</div></body></html>')
    return "\n".join(o)


# --------------------------------------------------------------------------
# 输出: CSV
# --------------------------------------------------------------------------
CSV_COLS = [
    ("symbol", "币种"), ("price", "价格"), ("px_chg_24", "24h涨跌%"),
    ("px_chg_oi", "24h同窗涨跌%"),
    ("oi_usd", "持仓规模USD"), ("oi_1h", "OI_1h%"), ("oi_4h", "OI_4h%"), ("oi_24h", "OI_24h名义%"),
    ("oi_contracts_24h", "OI_24h张数%"), ("mcap", "流通市值USD"),
    ("oi_mcap", "OI占市值"), ("turnover", "换手率"), ("yao", "妖币分"),
    ("funding_rate", "费率8h"), ("funding_ann", "费率年化%"),
    ("ls_account", "多空人数比"), ("ls_top", "大户持仓比"), ("taker", "主动买卖比"),
    ("leverage", "杠杆倍数"), ("quad", "象限"), ("quad_desc", "含义"),
    ("crowd", "拥挤度"), ("score", "异动分"), ("quote_vol", "24h成交额USD"),
]


def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([c[1] for c in CSV_COLS])
        for r in rows:
            out = []
            for k, _ in CSV_COLS:
                v = r.get(k)
                if isinstance(v, float):
                    out.append(round(v, 6))
                else:
                    out.append("" if v is None else v)
            w.writerow(out)


# --------------------------------------------------------------------------
# 输出: 推文草稿
# --------------------------------------------------------------------------
def build_tweets(rows, bench, stamp):
    d = stamp.split(" ")[0]
    L = []
    a = L.append

    btc = next((b for b in bench), None)
    a("# %s 推文草稿" % stamp)
    a("")
    a("> 用法：选一条改写，**必须自己加一句判断**，别原样复制。数据是你的人设，判断是你的价值。")
    a("> 发之前过一遍 `docs/04-合规红线清单.md`。")
    a("")

    # ---- 1. 四象限早报 ----
    a("## 草稿 1 · 四象限早报（固定栏目，建议每天发）")
    a("")
    a("```")
    a("【山寨币仓位结构·早报 %s】" % d)
    a("")
    for key, title, desc in QUAD_META:
        m = [r["base"] for r in rows if r.get("quad_key") == key][:4]
        if m:
            a("%s（%s）: %s" % (title, desc.split(" · ")[0], " / ".join(m)))
    a("")
    hot = sorted([r for r in rows if r.get("funding_ann") is not None],
                 key=lambda r: abs(r["funding_ann"]), reverse=True)[:3]
    if hot:
        a("拥挤度最高: " + " · ".join(
            "%s 年化 %s" % (r["base"], sgn_pct(r["funding_ann"], 0)) for r in hot))
    if btc:
        a("")
        a("大饼参照: %s %s" % (price_str(btc.get("price")), sgn_pct(btc.get("px_chg_24"))))
    a("")
    a("数据: Binance USDT-M 永续 | 内容为结构观察，不构成投资建议")
    a("```")
    a("")

    # ---- 2. OI 异动 ----
    a("## 草稿 2 · OI 异动 Top3（差异化最强的栏目）")
    a("")
    a("```")
    a("今天山寨合约里持仓异动最猛的三个，以及它们各自意味着什么：")
    a("")
    for i, r in enumerate(rows[:3], 1):
        a("%d. %s  价格 %s  持仓张数 %s  名义 %s  费率年化 %s"
          % (i, r["base"], sgn_pct(r.get("px_chg_oi")),
             sgn_pct(r.get("oi_contracts_24h")), sgn_pct(r.get("oi_24h")),
             sgn_pct(r.get("funding_ann"), 0)))
        a("   → %s：%s" % (r.get("quad", "—"), r.get("quad_desc", "—")))
        a("")
    a("价格与持仓取自同一时间窗，可以直接对照。")
    a("名义 = 张数 × 价格，所以两者不一致的差额就是价格贡献的部分。")
    a("注意：张数是净额。净额不变不代表没人止损，只说明进出被对冲掉了。")
    a("要判断到底是谁在离场，得再看主动买卖比和资金费率。")
    a("")
    a("数据: Binance USDT-M | 不构成投资建议")
    a("```")
    a("")

    # ---- 3. 费率极端 ----
    a("## 草稿 3 · 资金费率极端榜（每周一次，收割收藏）")
    a("")
    a("```")
    a("资金费率是最被低估的免费数据 —— 它直接告诉你现在多头和空头谁在付钱持仓。")
    a("")
    srt = sorted([r for r in rows if r.get("funding_ann") is not None],
                 key=lambda r: r["funding_ann"], reverse=True)
    if srt:
        a("多头最拥挤（年化费率）:")
        for r in srt[:3]:
            a("  %s  %s" % (r["base"], sgn_pct(r["funding_ann"], 0)))
        a("")
        a("空头最拥挤:")
        for r in srt[-3:][::-1]:
            a("  %s  %s" % (r["base"], sgn_pct(r["funding_ann"], 0)))
    a("")
    a("年化超过 100% 意味着多头一年要付掉一倍仓位成本去维持这个方向 ——")
    a("这种位置不是不能涨，是一旦不涨，多头会自己先跑。")
    a("")
    a("数据: Binance USDT-M | 不构成投资建议")
    a("```")
    a("")

    # ---- 4. 单币结构卡 ----
    if rows:
        r = rows[0]
        a("## 草稿 4 · 单币结构卡（%s，用来看图说话）" % r["base"])
        a("")
        a("```")
        a("%s 现在的仓位结构：" % r["base"])
        a("")
        a("价格(同窗)    %s" % sgn_pct(r.get("px_chg_oi")))
        a("持仓张数      %s" % sgn_pct(r.get("oi_contracts_24h")))
        a("持仓名义      %s" % sgn_pct(r.get("oi_24h")))
        a("持仓规模      %s USD" % money(r.get("oi_usd")))
        a("流通市值      %s USD（CMC 口径）" % money(r.get("mcap")))
        a("资金费率年化  %s" % sgn_pct(r.get("funding_ann"), 0))
        a("大户持仓比    %s" % ("%.2f" % r["ls_top"] if r.get("ls_top") else "—"))
        a("杠杆拥挤度    %s" % ("%.2fx" % r["leverage"] if r.get("leverage") else "—"))
        a("")
        a("读法：%s —— %s。" % (r.get("quad"), r.get("quad_desc")))
        a("我不猜方向。我只想知道这个位置继续往上走，靠的是新钱进场，还是空头认输。")
        a("")
        a("数据: Binance USDT-M | 结构观察，不构成投资建议")
        a("```")
        a("")

    # ---- 5. 互动钩子 ----
    a("## 互动钩子（配上面任意一条发，评论区用）")
    a("")
    a("- 你手里仓位最多的是哪个？我下条把它的 OI 结构拆给你看。")
    a("- 想知道我怎么读「价涨 OI 减」的？评论区扣 1，我做张图。")
    a("- 这条如果对你有用，存下来比点赞有用 —— 下次费率飙到 100% 你会想起它。")
    a("- 反驳我。尤其是「OI 没用」这个说法，我特别想听。")
    a("")
    a("---")
    a("")
    a("数据口径提示：OI 与费率均取自 Binance 公开接口，快照时间 %s。" % stamp)
    a("不同交易所口径不同，跨所对比时需注明来源。")
    return "\n".join(L)


# --------------------------------------------------------------------------
# 输出: 每日三条 (单条版, 字少)
# --------------------------------------------------------------------------
def pick_daily(rows):
    """从涨跌榜样本里按三个不同角度各挑一条, 自动去重。"""
    if not rows:
        return []
    picks = []

    def add(r):
        if r and r["symbol"] not in [p["symbol"] for p in picks]:
            picks.append(r)

    add(max(rows, key=lambda r: r.get("yao") or 0))                    # 结构最脏
    add(max(rows, key=lambda r: abs(r.get("oi_contracts_24h") or 0)))  # 仓位变化最猛
    add(max(rows, key=lambda r: -((r.get("px_chg_oi") or 0)
                                  * (r.get("oi_contracts_24h") or 0))))  # 价与仓背离
    return picks[:3]


def _taker_line(r):
    tk = r.get("taker")
    if tk is None:
        return None
    if tk < 0.95:
        return "主动方是卖方（买卖比 %.2f）" % tk
    if tk > 1.05:
        return "主动方是买方（买卖比 %.2f）" % tk
    return "主动买卖基本平衡（%.2f）" % tk


def _why(r, i):
    if i == 1:
        return "妖币分最高（同窗涨跌 × 持仓占市值 = %.0f）" % (r.get("yao") or 0)
    if i == 2:
        return "持仓张数变化最大（%s），当天仓位结构变动最剧烈" % sgn_pct(r.get("oi_contracts_24h"))
    return "价格与持仓背离最极端"


def build_daily(picks, stamp):
    L = []
    a = L.append
    a("# %s · 每日三条（单条版）" % stamp)
    a("")
    a("> 每条都在 140 汉字内，可直接发。")
    a("> 发之前重跑一次：最后一根 K 线未收，数字会跳。")
    a("> NFA 别删。")
    a("")
    for i, r in enumerate(picks, 1):
        base = r["base"]
        px = sgn_pct(r.get("px_chg_oi"))
        ct = sgn_pct(r.get("oi_contracts_24h"))
        om = r.get("oi_mcap")
        oms = "%.1f%%" % (om * 100) if om is not None else "—"
        tv = r.get("turnover")
        tvs = "%.2fx" % tv if tv is not None else "—"
        a("## 第 %d 条 · %s" % (i, base))
        a("")
        a("```")
        if i == 1:
            a("%s 24h %s，持仓张数 %s。" % (base, px, ct))
            a("%s：%s。" % (r.get("quad", "—"), r.get("quad_desc", "—")))
            a("持仓占流通市值 %s，换手 %s。" % (oms, tvs))
            a("不判断方向。NFA")
        elif i == 2:
            a("%s 一天里持仓张数 %s，价格 %s。" % (base, ct, px))
            tk = r.get("taker")
            q = r.get("quad_key")
            if tk is not None and q == "q3" and tk > 1.05:
                a("价格跌、持仓增，但主动方是买方 —— 多头在硬接，不是空头在推。")
            elif tk is not None and q == "q3" and tk < 0.95:
                a("价格跌、持仓增、主动方是卖方 —— 新空在顺势建仓。")
            elif tk is not None and q == "q1" and tk < 0.95:
                a("价格涨、持仓增，但主动方是卖方 —— 这波涨得虚。")
            elif tk is not None and q == "q2":
                a("上涨伴随持仓减少 —— 空头在平仓，不是新钱进场。")
            else:
                tl = _taker_line(r)
                if tl:
                    a(tl + "。")
            a("换手 %s，流通市值 %s。" % (tvs, money(r.get("mcap"))))
            a("仓位在变，方向未定。NFA")
        else:
            a("%s 价格 %s，持仓 %s —— 两个方向反着走。" % (base, px, ct))
            if (r.get("px_chg_oi") or 0) < 0 and (r.get("oi_contracts_24h") or 0) > 0:
                a("跌势里有人在持续开新仓，不是多头在离场。")
            elif (r.get("px_chg_oi") or 0) > 0 and (r.get("oi_contracts_24h") or 0) < 0:
                a("这段上涨是平仓推动的，不是新钱进场。")
            else:
                a("价格与持仓同向，没有背离。")
            a("持仓占流通市值 %s。" % oms)
            a("只看结构，不给点位。NFA")
        a("```")
        a("")
        a("> 为什么选它：%s" % _why(r, i))
        a("")
    return "\n".join(L)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="山寨币合约仓位结构雷达")
    ap.add_argument("--top", type=int, default=0,
                    help="普通模式: 成交额前 N (默认25); --screen 模式: 涨跌榜每个方向前 N (默认30)")
    ap.add_argument("--minvol", type=float, default=1e6,
                    help="成交额下限 USD, 过滤僵尸合约 (默认 100万, 仅 --screen 生效)")
    ap.add_argument("--sort", default="auto",
                    choices=["auto", "yao", "oi_mcap", "oi_change", "turnover",
                             "mcap", "change", "score"],
                    help="排序字段: oi_change=持仓张数变化率(流量), oi_mcap=持仓占市值(存量)")
    ap.add_argument("--symbols", type=str, default="", help="指定币种, 逗号分隔, 如 PEPEUSDT,WIFUSDT")
    ap.add_argument("--workers", type=int, default=8, help="并发线程数 (默认 8)")
    ap.add_argument("--proxy", type=str, default="", help="本地代理, 如 http://127.0.0.1:7890")
    ap.add_argument("--demo", action="store_true", help="用内置样本数据离线演示")
    ap.add_argument("--screen", action="store_true",
                    help="妖币筛选模式: 取涨跌榜两个方向前 N, 加成交额下限, 剔除代币化股票, 按妖币分排序")
    ap.add_argument("--daily", action="store_true",
                    help="每日三条: 从涨跌榜挑 3 个不同角度, 各出一条可直接发的单条推文")
    ap.add_argument("--outdir", type=str, default="", help="输出目录, 默认脚本同级 output/")
    args = ap.parse_args()

    # Windows 控制台常见 GBK 编码, 打印箭头/特殊符号会崩, 统一转 UTF-8 并容错
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    outdir = args.outdir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    os.makedirs(outdir, exist_ok=True)

    init_net(args.proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"))

    print("=" * 62)
    print("  山寨币合约 · 仓位结构雷达")
    print("=" * 62)

    if args.demo:
        rows, bench = demo_rows()
    else:
        try:
            if args.screen or args.daily:
                top_n = args.top if args.top > 0 else 30
                rows, bench = fetch_rows(top_n, args.symbols, args.workers,
                                         light=True, rank_by="change", minvol=args.minvol)
            else:
                top_n = args.top if args.top > 0 else 25
                rows, bench = fetch_rows(top_n, args.symbols, args.workers)
        except Exception as e:
            print("\n[抓取失败] %s" % e)
            print("\n常见原因与对策:")
            print("  1. 访问不了 Binance → 加代理:  --proxy http://127.0.0.1:7890")
            print("  2. 地区限制 / 网络封锁 → 换网络环境, 或用 --demo 先看成品")
            print("  3. 接口临时限流 → 等 1 分钟重试, 或 --top 10 减少请求")
            return 1

    rows = score_rows(rows)
    rows = compute_yao(rows)
    if args.screen or args.daily:
        before = len(rows)
        rows = [r for r in rows if r.get("mcap")]
        if len(rows) < before:
            print("  [剔除] %d 个没有流通供给的标的(代币化股票/ETF), 不参与妖币排序"
                  % (before - len(rows)))

    sorters = {
        "yao": lambda r: r.get("yao") or 0,
        "oi_mcap": lambda r: r.get("oi_mcap") or 0,
        "oi_change": lambda r: abs(r.get("oi_contracts_24h") or 0),
        "turnover": lambda r: r.get("turnover") or 0,
        "mcap": lambda r: r.get("mcap") or 0,
        "change": lambda r: abs(r.get("px_chg_24") or 0),
        "score": lambda r: r.get("score") or 0,
    }
    sort_key = args.sort
    if sort_key == "auto":
        sort_key = "yao" if (args.screen or args.daily) else "score"
    rows.sort(key=sorters[sort_key], reverse=True)
    now = datetime.now(CST)
    stamp = now.strftime("%Y-%m-%d %H:%M")
    tag = now.strftime("%Y%m%d_%H%M")

    # 每日三条：先按涨跌榜选出三个角度，再把主动买卖比补齐（归因需要）
    if args.daily:
        picks = pick_daily(rows)
        if not args.demo and picks:
            try:
                _perp, tickers, prem = fetch_universe()
                enriched = []
                for p in picks:
                    enriched.append(fetch_one(p["symbol"], tickers, prem, light=False))
                compute_yao(enriched)
                picks = enriched
            except Exception as ex:
                print("  [提示] 补充主动买卖比失败，改用基础数据: %s" % ex)
        text = build_daily(picks, stamp)
        dpath = os.path.join(outdir, "daily_%s.md" % tag)
        with open(dpath, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("\n" + text)
        print("\n  已生成: %s" % dpath)
        return 0

    html_path = os.path.join(outdir, "radar_%s.html" % tag)
    csv_path = os.path.join(outdir, "radar_%s.csv" % tag)
    tw_path = os.path.join(outdir, "tweets_%s.md" % tag)

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(build_html(rows, bench, stamp, demo=args.demo))
    write_csv(rows, csv_path)
    with open(tw_path, "w", encoding="utf-8") as f:
        f.write(build_tweets(rows, bench, stamp))

    # 控制台速览
    print("\n  %-12s %8s %9s %9s %9s %8s %8s"
          % ("币种", "24h同窗", "OI张数", "流通市值", "OI/市值", "换手", "妖币分"))
    print("  " + "-" * 74)
    for r in rows[:15]:
        print("  %-12s %8s %9s %9s %9s %8s %8.0f"
              % (r["base"], sgn_pct(r.get("px_chg_24")),
                 sgn_pct(r.get("oi_contracts_24h")),
                 money(r.get("mcap")),
                 ("%.1f%%" % (r["oi_mcap"] * 100)) if r.get("oi_mcap") is not None else "—",
                 ("%.2fx" % r["turnover"]) if r.get("turnover") is not None else "—",
                 r.get("yao") or 0))

    print("\n  已生成:")
    print("    看板   %s" % html_path)
    print("    数据   %s" % csv_path)
    print("    推文   %s" % tw_path)
    print("\n  下一步: 用浏览器打开 HTML, 截图右下角带上免责声明的区域发推。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
