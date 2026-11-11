# -*- coding: utf-8 -*-
"""xvenue.py — 跨所持仓快照（Bybit + OKX）

每家一次请求拿全市场。只做两件事：
  1. 提供各所的 USD 持仓名义，用于把「持仓/市值」从单所口径修正为跨所口径
  2. 提供各所报价，用于资产身份校验（同名不同币必须剔除）

⚠️ 符号碰撞是真实存在的：币安的 ONUSDT 是 $0.09 级别的币，
   而 Bybit / OKX 上的 ON 是 $72 级别的另一个资产。只按符号对齐必然出错，
   所以每次都要用「跨所价差 > 20% 即判定为不同资产」来拦截。
"""
import json
import urllib.request

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
PX_TOLERANCE = 0.20          # 跨所价差超过 20% 视为不同资产

BYBIT = "https://api.bybit.com/v5/market/tickers?category=linear"
OKX_OI = "https://www.okx.com/api/v5/public/open-interest?instType=SWAP"
OKX_PX = "https://www.okx.com/api/v5/market/tickers?instType=SWAP"
HL_INFO = "https://api.hyperliquid.xyz/info"


def _get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


def _post(url, payload):
    headers = dict(UA)
    headers["Content-Type"] = "application/json"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def base_of(binance_symbol):
    """币安符号 -> 通用基础币名。1000PEPEUSDT -> PEPE"""
    b = binance_symbol.replace("USDT", "")
    if b.startswith("1000") and len(b) > 4:
        b = b[4:]
    return b


def fetch():
    """返回 (data, info)。

    data: {基础币名: {"bybit_usd": float, "bybit_px": float,
                      "okx_usd": float, "okx_px": float}}
    info: {"bybit": n, "okx": n, "errors": [...]}
    """
    data = {}
    info = {"bybit": 0, "okx": 0, "hl": 0, "errors": []}

    # ---- Bybit：一次拿全部线性合约 ----
    try:
        rows = _get(BYBIT)["result"]["list"]
        for r in rows:
            sym = r.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            oi = _f(r.get("openInterestValue"))
            px = _f(r.get("lastPrice"))
            if oi <= 0:
                continue
            b = sym[:-4]
            d = data.setdefault(b, {})
            d["bybit_usd"] = d.get("bybit_usd", 0.0) + oi
            d["bybit_px"] = px
            info["bybit"] += 1
    except Exception as e:
        info["errors"].append("Bybit: %s" % e)

    # ---- OKX：持仓与报价是两个接口，都要 ----
    okx_px = {}
    try:
        for r in _get(OKX_PX)["data"]:
            inst = r.get("instId", "")
            if not inst.endswith("-USDT-SWAP"):
                continue
            okx_px[inst.split("-")[0]] = _f(r.get("last"))
    except Exception as e:
        info["errors"].append("OKX 报价: %s" % e)

    try:
        for r in _get(OKX_OI)["data"]:
            inst = r.get("instId", "")
            if not inst.endswith("-USDT-SWAP"):
                continue
            b = inst.split("-")[0]
            oi = _f(r.get("oiUsd"))
            if oi <= 0:
                continue
            d = data.setdefault(b, {})
            d["okx_usd"] = d.get("okx_usd", 0.0) + oi
            d["okx_px"] = okx_px.get(b, 0.0)
            info["okx"] += 1
    except Exception as e:
        info["errors"].append("OKX 持仓: %s" % e)

    # ---- Hyperliquid：一次 POST 拿全部资产（持仓按币数 × markPx 折成美元）----
    try:
        j = _post(HL_INFO, {"type": "metaAndAssetCtxs"})
        names = [a.get("name", "") for a in j[0].get("universe", [])]
        ctxs = j[1]
        for i, n in enumerate(names):
            if not n or i >= len(ctxs):
                continue
            c = ctxs[i] or {}
            oi = _f(c.get("openInterest"))
            px = _f(c.get("markPx"))
            usd = oi * px
            if usd <= 0 or px <= 0:
                continue
            d = data.setdefault(n, {})
            d["hl_usd"] = d.get("hl_usd", 0.0) + usd
            d["hl_px"] = px
            info["hl"] += 1
    except Exception as e:
        info["errors"].append("Hyperliquid: %s" % e)

    return data, info


def unit_mult(binance_symbol):
    """币安 1000XXX 合约按「千枚」计价，比价前要把它折算成单枚价。"""
    b = binance_symbol.replace("USDT", "")
    return 1000.0 if (b.startswith("1000") and len(b) > 4) else 1.0


def attach(rows, data, tol=PX_TOLERANCE):
    """把跨所持仓挂到行上，并做资产身份校验。

    给每行补充：
        oi_bybit / oi_okx / oi_total / bin_share / oi_mcap_all / collide
    """
    collisions = []
    for r in rows:
        b = base_of(r["symbol"])
        px = r.get("price") or 0
        mult = unit_mult(r["symbol"])
        px_unit = px / mult if px else 0      # 折算成单枚价再比
        d = data.get(b, {})

        bybit = d.get("bybit_usd", 0.0)
        okx = d.get("okx_usd", 0.0)
        hl = d.get("hl_usd", 0.0)

        # 资产身份校验：报价对不上就整所剔除
        drop = []
        for venue, key_oi, key_px in (("Bybit", "bybit_usd", "bybit_px"),
                                      ("OKX", "okx_usd", "okx_px"),
                                      ("Hyperliquid", "hl_usd", "hl_px")):
            vpx = d.get(key_px) or 0
            if d.get(key_oi) and px_unit and vpx:
                if abs(px_unit / vpx - 1.0) > tol:
                    drop.append((venue, vpx))
                    if key_oi == "bybit_usd":
                        bybit = 0.0
                    elif key_oi == "okx_usd":
                        okx = 0.0
                    else:
                        hl = 0.0
        if drop:
            collisions.append((r["base"], px_unit, drop))

        bin_oi = r.get("oi_usd") or 0.0
        total = bin_oi + bybit + okx + hl
        r["oi_bybit"] = bybit
        r["oi_okx"] = okx
        r["oi_hl"] = hl
        r["oi_total"] = total
        r["bin_share"] = (bin_oi / total) if total else None
        mcap = r.get("mcap")
        r["oi_mcap_all"] = (total / mcap) if (total and mcap) else None
        r["collide"] = bool(drop)
    return collisions


def reyao(rows):
    """用跨所口径重算妖币分（价格变化仍取币安同窗，因为跨所没有历史序列）。"""
    for r in rows:
        chg = r.get("px_chg_oi")
        if chg is None:
            chg = r.get("px_chg_24") or 0
        om = r.get("oi_mcap_all")
        r["yao"] = round(abs(chg) * om * 100, 1) if om else 0.0
    return rows
