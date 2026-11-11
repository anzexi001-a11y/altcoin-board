# -*- coding: utf-8 -*-
"""altcoin_board.py — 山寨币合约「异动榜 + 持仓占比榜」单页看板

把三条判定做成可计算的信号，不是罗列：
  1. OI 在价格横盘时先动   -> 有人提前建仓
  2. 费率转负但价格不跌     -> 空头在交钱，价格不认
  3. 成交放大而 OI 不跟     -> 只换手，没有新仓沉淀
再加五条：
  4. 价跌而持仓增           -> 新空进场 / 多头摊平
  5. 价涨而持仓减           -> 空头平仓，燃料耗尽
  6. 持仓占市值过高         -> 合约盘子相对现货过重
  7. 费率极端为正           -> 多头极度拥挤
  8. 单小时 OI 暴动         -> 急性异动

扫描范围用全市场成交额榜（不是涨跌榜），因为 1、2、3 这三类信号
出现的币恰恰是不涨不跌的那批，在涨跌榜上永远看不到。

用法:
    python altcoin_board.py                 扫成交额前 400，出两张 Top10
    python altcoin_board.py --top 150       扫少一点，快
    python altcoin_board.py --minvol 3000000
"""
import argparse
import html
import os
import sys
from datetime import datetime

import altcoin_radar as ar
import xvenue

SIGNALS = [
    # key, 名称, 触发条件描述, 含义, 权重
    ("s1", "横盘吸筹", "|价格| ≤ 3% 且 持仓张数 ≥ +10%", "有人提前建仓", 2.0),
    ("s2", "费率背离", "年化费率 ≤ -30% 且 价格 ≥ -2%", "空头在交钱，价格不认", 2.0),
    ("s3", "空换手", "换手 ≥ 1.5x 且 |持仓张数| ≤ 5%", "只换手，没有新仓沉淀", 1.5),
    ("s4", "价跌仓增", "价格 ≤ -5% 且 持仓张数 ≥ +10%", "新空进场或多头摊平", 2.0),
    ("s5", "价涨仓减", "价格 ≥ +5% 且 持仓张数 ≤ -10%", "空头平仓，燃料耗尽", 2.0),
    ("s6", "杠杆过重", "跨所持仓/流通市值 ≥ 35%", "合约盘子相对现货过重", 1.5),
    ("s7", "多头拥挤", "费率年化 ≥ 100%", "多头极度拥挤", 1.0),
    ("s8", "急性异动", "|单小时持仓张数变化| ≥ 8%", "急性异动，需立刻看", 1.5),
]


def detect(r):
    """返回命中的信号 key 列表。所有阈值都和 SIGNALS 里写的保持一致。"""
    px = r.get("px_chg_oi")
    ct = r.get("oi_contracts_24h")
    fr = r.get("funding_ann")
    tv = r.get("turnover")
    om = r.get("oi_mcap_all")
    if om is None:
        om = r.get("oi_mcap")
    h1 = r.get("oi_1h")
    hit = []

    if px is not None and ct is not None and abs(px) <= 3 and ct >= 10:
        hit.append("s1")
    if fr is not None and px is not None and fr <= -30 and px >= -2:
        hit.append("s2")
    if tv is not None and ct is not None and tv >= 1.5 and abs(ct) <= 5:
        hit.append("s3")
    if px is not None and ct is not None and px <= -5 and ct >= 10:
        hit.append("s4")
    if px is not None and ct is not None and px >= 5 and ct <= -10:
        hit.append("s5")
    if om is not None and om >= 0.35:
        hit.append("s6")
    if fr is not None and fr >= 100:
        hit.append("s7")
    if h1 is not None and abs(h1) >= 8:
        hit.append("s8")
    return hit


WEIGHTS = {s[0]: s[4] for s in SIGNALS}


def sig_score(hits):
    return round(sum(WEIGHTS.get(h, 0) for h in hits), 1)


def cell_chg(v, key=None):
    attr = ' data-k="%s"' % key if key else ""
    if v is None:
        return '<td class="mut"%s>—</td>' % attr
    cls = "up" if v > 0 else ("down" if v < 0 else "mut")
    return '<td class="%s"%s>%s</td>' % (cls, attr, ar.sgn_pct(v))


CSS = """
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0b0e11;color:#eaecef;font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
     padding:26px 18px 44px;line-height:1.55}
.wrap{max-width:1300px;margin:0 auto}
.top{display:flex;flex-wrap:wrap;align-items:flex-end;gap:14px;border-bottom:1px solid #232830;padding-bottom:15px;margin-bottom:8px}
h1{font-size:23px;font-weight:700}
h1 span{color:#f0b90b}
.stamp{color:#848e9c;font-size:13px;margin-left:auto;text-align:right}
h2{font-size:16px;margin:30px 0 12px;padding-left:9px;border-left:3px solid #f0b90b}
h2 em{font-style:normal;color:#848e9c;font-size:12px;font-weight:400;margin-left:8px}
.sig{display:grid;grid-template-columns:repeat(auto-fill,minmax(258px,1fr));gap:10px}
.sc{background:#161a1e;border:1px solid #232830;border-radius:9px;padding:12px 14px}
.sc h3{font-size:14px;margin-bottom:5px;color:#f0b90b}
.sc p{font-size:12px;color:#9aa6b2;margin-bottom:4px}
.sc u{text-decoration:none;color:#eaecef;font-size:12.5px}
.sci{font-size:11px;color:#5e6673;margin-top:6px}
table{width:100%;border-collapse:collapse;background:#161a1e;border-radius:9px;overflow:hidden;font-size:12.5px}
thead th{background:#1e2329;color:#848e9c;font-weight:600;text-align:right;padding:9px 8px;white-space:nowrap;font-size:11.5px}
thead th:first-child,tbody td:first-child{text-align:left;padding-left:12px}
tbody td{padding:8px;text-align:right;border-top:1px solid #232830;white-space:nowrap;font-variant-numeric:tabular-nums}
tbody tr:hover{background:#1c2126}
.rk{color:#5e6673;font-variant-numeric:tabular-nums}
.sym{font-weight:600}
.sym small{color:#5e6673;font-weight:400;margin-left:4px;font-size:10.5px}
.up{color:#0ecb81}.down{color:#f6465d}.mut{color:#848e9c}
.bd{display:inline-block;padding:1px 6px;border-radius:4px;font-size:10.5px;margin:1px 2px 1px 0;
    background:rgba(240,185,11,.13);color:#f0b90b;white-space:nowrap}
.bd.hot{background:rgba(246,70,93,.15);color:#f6465d}
.bd.cool{background:rgba(14,203,129,.13);color:#0ecb81}
.bar{height:4px;background:#232830;border-radius:2px;width:46px;display:inline-block;vertical-align:middle;margin-right:6px}
.bar i{display:block;height:4px;border-radius:2px;background:#f0b90b}
.empty{padding:14px;color:#5e6673;font-size:12.5px;background:#161a1e;border-radius:9px}
.status{background:#161a1e;border:1px solid #232830;border-left:3px solid #0ecb81;border-radius:7px;
        padding:9px 13px;font-size:12px;color:#9aa6b2;margin:12px 0 2px;line-height:1.75}
.status b{color:#eaecef}
.status i{font-style:normal;color:#0ecb81}
.btn{display:inline-block;margin:10px 0 2px;background:#1e2329;color:#eaecef;border:1px solid #2b323b;
     border-radius:7px;padding:8px 14px;font-size:12.5px;cursor:pointer;font-family:inherit}
.btn:hover{background:#262c33;border-color:#f0b90b}
footer{margin-top:28px;padding-top:14px;border-top:1px solid #232830;color:#5e6673;font-size:11.5px;line-height:1.8}
@media(max-width:760px){body{padding:16px 8px}}
"""


LIVE_JS = r"""
(function(){
  var API   = "https://fapi.binance.com";
  var BYBIT = "https://api.bybit.com/v5/market/tickers?category=linear";
  var OKXOI = "https://www.okx.com/api/v5/public/open-interest?instType=SWAP";
  var OKXPX = "https://www.okx.com/api/v5/market/tickers?instType=SWAP";
  var HLAPI = "https://api.hyperliquid.xyz/info";
  var MS = 300000, TOL = 0.20;

  function money(v){
    if(v===null||v===undefined||isNaN(v)) return "—";
    if(v>=1e9) return (v/1e9).toFixed(2)+"B";
    if(v>=1e6) return (v/1e6).toFixed(1)+"M";
    if(v>=1e3) return (v/1e3).toFixed(1)+"K";
    return v.toFixed(2);
  }
  function xmoney(v){ return v ? money(v) : "—"; }
  function pctf(v){
    if(v===null||v===undefined||isNaN(v)) return "—";
    return (v>0?"+":"")+v.toFixed(2)+"%";
  }
  function baseOf(s){
    var b = s.replace("USDT","");
    if(b.indexOf("1000")===0 && b.length>4) b = b.slice(4);
    return b;
  }
  function put(tr,k,text,numv){
    var td = tr.querySelector('[data-k="'+k+'"]');
    if(!td) return;
    td.textContent = text;
    if(td.classList.contains("up")||td.classList.contains("down")){
      td.classList.remove("up","down");
      td.classList.add(numv>0?"up":(numv<0?"down":"mut"));
    }
  }
  function putCls(tr,k,text,cls){
    var td = tr.querySelector('[data-k="'+k+'"]');
    if(!td) return;
    td.textContent = text;
    td.classList.remove("up","down","mut");
    td.classList.add(cls);
  }

  function unitMult(s){
    var b = s.replace("USDT","");
    return (b.indexOf("1000")===0 && b.length>4) ? 1000 : 1;
  }

  // ---- 纯计算函数，可在 node 里离线校验 ----
  function calc(hist, ticker, cross, mult){
    mult = mult || 1;
    var p = [], i, h;
    for(i=0;i<hist.length;i++){
      h = hist[i];
      if(+h.sumOpenInterest>0 && +h.sumOpenInterestValue>0) p.push(h);
    }
    if(p.length < 2) return null;
    var a0=p[0], b0=p[p.length-1];
    var c0=+a0.sumOpenInterest, v0=+a0.sumOpenInterestValue;
    var c1=+b0.sumOpenInterest, v1=+b0.sumOpenInterestValue;
    var sup=+b0.CMCCirculatingSupply;
    var px0=v0/c0, px1=v1/c1;
    var price=+ticker.lastPrice / mult;      // 折算成单枚价再比
    var by=0, ok=0, hl=0;
    if(cross){
      if(cross.by && (!cross.bpx || Math.abs(price/cross.bpx-1)<=TOL)) by=cross.by;
      if(cross.ok && (!cross.opx || Math.abs(price/cross.opx-1)<=TOL)) ok=cross.ok;
      if(cross.hl && (!cross.hpx || Math.abs(price/cross.hpx-1)<=TOL)) hl=cross.hl;
    }
    var total=v1+by+ok+hl;
    var mcap = sup>0 ? sup*px1 : null;
    return {
      px:(px1/px0-1)*100,
      ct:(c1/c0-1)*100,
      oi24:(v1/v0-1)*100,
      oi_bin:v1, x_bybit:by, x_okx:ok, x_hl:hl, oi_total:total,
      bin_share: total ? v1/total*100 : null,
      mcap:mcap,
      om: (mcap && total) ? total/mcap*100 : null,
      tv: (mcap && +ticker.quoteVolume) ? +ticker.quoteVolume/mcap : null
    };
  }
  globalThis.__boardCalc = calc;

  function pickGainers(all, minvol, n){
    var arr = [];
    (all || []).forEach(function(t){
      var s = t.symbol || "";
      if(s.slice(-4) !== "USDT") return;
      if(+t.quoteVolume < minvol) return;
      arr.push({ s:s, pc:+t.priceChangePercent, tk:t });
    });
    arr.sort(function(x,y){ return y.pc - x.pc; });
    return arr.slice(0, n);
  }
  globalThis.__pickGainers = pickGainers;

  function refreshGainers(cross){
    var tb = document.getElementById("gainers");
    if(!tb) return Promise.resolve(0);
    var cfg = document.getElementById("cfg");
    var minvol = (cfg && cfg.dataset.minvol) ? +cfg.dataset.minvol : 1e6;
    return fetch(API + "/fapi/v1/ticker/24hr")
      .then(function(r){ return r.json(); })
      .then(function(all){
        var picked = pickGainers(all, minvol, 15);
        return Promise.all(picked.map(function(g){
          var b = baseOf(g.s);
          return fetch(API + "/futures/data/openInterestHist?symbol=" + g.s + "&period=1h&limit=25")
            .then(function(r){ return r.json(); })
            .then(function(hist){
              return { g:g, d: calc(hist, g.tk, cross[b], unitMult(g.s)) || {} };
            }).catch(function(){ return { g:g, d:{} }; });
        }));
      })
      .then(function(items){
        var rows = [], i, it, d, g, base;
        for(i = 0; i < items.length && rows.length < 5; i++){
          it = items[i];
          d = it.d || {};
          if(!d.mcap) continue;        // 代币化股票没有流通供给，跳过并补下一位
          g = it.g;
          base = g.s.replace("USDT","");
          rows.push('<tr><td class="rk">' + (rows.length + 1) + '</td>'
            + '<td class="sym">' + base + '</td>'
            + '<td class="' + (g.pc>0?"up":(g.pc<0?"down":"mut")) + '">' + pctf(g.pc) + '</td>'
            + '<td class="' + (d.ct>0?"up":(d.ct<0?"down":"mut")) + '">' + pctf(d.ct) + '</td>'
            + '<td class="mut">' + money(d.mcap) + '</td>'
            + '<td class="' + ((d.om||0)>=35?"up":"mut") + '">'
            + (d.om==null?"—":d.om.toFixed(1)+"%") + '</td>'
            + '<td class="mut">' + (d.tv==null?"—":d.tv.toFixed(2)+"x") + '</td>'
            + '<td class="' + ((d.bin_share||0)>=85?"mut":"down") + '">'
            + (d.bin_share==null?"—":d.bin_share.toFixed(0)+"%") + '</td></tr>');
        }
        if(rows.length) tb.innerHTML = rows.join("");
        return rows.length;
      })
      .catch(function(){ return 0; });
  }
  globalThis.__refreshGainers = refreshGainers;

  function fetchCross(){
    return Promise.all([
      fetch(BYBIT).then(function(r){return r.json();}).catch(function(){return null;}),
      fetch(OKXOI).then(function(r){return r.json();}).catch(function(){return null;}),
      fetch(OKXPX).then(function(r){return r.json();}).catch(function(){return null;}),
      fetch(HLAPI, { method:"POST", headers:{ "Content-Type":"application/json" },
                     body: JSON.stringify({ type:"metaAndAssetCtxs" }) })
        .then(function(r){return r.json();}).catch(function(){return null;})
    ]).then(function(res){
      var map = {}, by = res[0], oi = res[1], px = res[2], hl = res[3], e, b, v;
      if(by && by.result && by.result.list){
        by.result.list.forEach(function(r){
          var s = r.symbol || "";
          if(s.slice(-4) !== "USDT") return;
          v = +r.openInterestValue;
          if(!(v>0)) return;
          b = s.slice(0,-4);
          e = map[b] || (map[b] = {});
          e.by = (e.by||0) + v;
          e.bpx = +r.lastPrice;
        });
      }
      var pxm = {};
      if(px && px.data){
        px.data.forEach(function(r){
          var inst = r.instId || "";
          if(inst.slice(-10) !== "-USDT-SWAP") return;
          pxm[inst.split("-")[0]] = +r.last;
        });
      }
      if(oi && oi.data){
        oi.data.forEach(function(r){
          var inst = r.instId || "";
          if(inst.slice(-10) !== "-USDT-SWAP") return;
          v = +r.oiUsd;
          if(!(v>0)) return;
          b = inst.split("-")[0];
          e = map[b] || (map[b] = {});
          e.ok = (e.ok||0) + v;
          e.opx = pxm[b] || 0;
        });
      }
      if(hl && hl[0] && hl[0].universe && hl[1]){
        var names = hl[0].universe, ctxs = hl[1], i, n, c, usd, p;
        for(i=0; i<names.length && i<ctxs.length; i++){
          n = names[i].name;
          c = ctxs[i] || {};
          p = +c.markPx;
          usd = (+c.openInterest) * p;
          if(!n || !(usd>0) || !(p>0)) continue;
          e = map[n] || (map[n] = {});
          e.hl = (e.hl||0) + usd;
          e.hpx = p;
        }
      }
      return map;
    });
  }

  function refresh(){
    var trs = Array.prototype.slice.call(document.querySelectorAll("tr[data-sym]"));
    var syms = [];
    trs.forEach(function(t){ if(syms.indexOf(t.dataset.sym)<0) syms.push(t.dataset.sym); });
    var box = document.getElementById("upd");
    if(box) box.textContent = "正在刷新…";
    fetchCross().then(function(cross){
      return Promise.all([refreshGainers(cross), Promise.all(syms.map(function(s){
        var b = baseOf(s);
        return Promise.all([
          fetch(API+"/futures/data/openInterestHist?symbol="+s+"&period=1h&limit=25")
            .then(function(r){return r.json();}),
          fetch(API+"/fapi/v1/ticker/24hr?symbol="+s)
            .then(function(r){return r.json();})
        ]).then(function(res){
          var d = calc(res[0], res[1], cross[b], unitMult(s));
          return d ? { s:s, d:d } : null;
        }).catch(function(){ return null; });
      }))]);
    }).then(function(res){
      var items = res[1], gcount = res[0];
      var map = {}, ok = 0;
      items.forEach(function(it){ if(it){ map[it.s] = it.d; ok++; } });
      trs.forEach(function(tr){
        var d = map[tr.dataset.s];
        if(!d) return;
        put(tr,"px",pctf(d.px),d.px);
        put(tr,"ct",pctf(d.ct),d.ct);
        put(tr,"oi24",pctf(d.oi24),d.oi24);
        put(tr,"oi_bin",money(d.oi_bin),null);
        put(tr,"oi_total",money(d.oi_total),null);
        putCls(tr,"bin_share", d.bin_share===null?"—":d.bin_share.toFixed(0)+"%",
               d.bin_share!==null && d.bin_share<85 ? "down" : "mut");
        put(tr,"x_bybit",xmoney(d.x_bybit),null);
        put(tr,"x_okx",xmoney(d.x_okx),null);
        put(tr,"x_hl",xmoney(d.x_hl),null);
        put(tr,"mcap",money(d.mcap),null);
        putCls(tr,"om", d.om===null?"—":d.om.toFixed(1)+"%",
               d.om!==null && d.om>=35 ? "up" : "mut");
        put(tr,"tv",d.tv===null?"—":d.tv.toFixed(2)+"x",null);
      });
      var now = new Date();
      if(box) box.textContent = "数据刷新 " + now.toTimeString().slice(0,8)
        + "（明细 " + ok + "/" + syms.length + "，涨幅榜重排 " + gcount + " 条，含跨所口径）";
    });
  }
  function tablesToMd(){
    var titles = ["涨幅榜 Top5", "异动 Top10", "持仓占市值 Top10"];
    var tabs = document.querySelectorAll("table"), parts = [], i, j, k;
    for(i=0;i<tabs.length;i++){
      parts.push("## " + (titles[i] || ("表" + (i+1))));
      parts.push("");
      var rows = tabs[i].querySelectorAll("tr");
      for(j=0;j<rows.length;j++){
        var cells = rows[j].querySelectorAll("th,td"), out = [], sep = [];
        for(k=0;k<cells.length;k++){
          out.push(cells[k].textContent.replace(/\s+/g," ").trim());
          sep.push("---");
        }
        parts.push("| " + out.join(" | ") + " |");
        if(j === 0) parts.push("| " + sep.join(" | ") + " |");
      }
      parts.push("");
    }
    var head = "山寨币合约异动榜 · " + new Date().toLocaleString("zh-CN", {hour12:false});
    return head + "\n\n" + parts.join("\n");
  }
  globalThis.__tablesToMd = tablesToMd;

  (function(){
    var btn = document.getElementById("copymd");
    if(!btn) return;
    var label = btn.textContent;
    function ok(){ btn.textContent = "已复制 ✓"; setTimeout(function(){ btn.textContent = label; }, 2000); }
    function fallback(txt){
      var ta = document.createElement("textarea");
      ta.value = txt;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); ok(); } catch(e) { alert("复制失败，请手动选中表格文字"); }
      document.body.removeChild(ta);
    }
    btn.addEventListener("click", function(){
      var txt = tablesToMd();
      if(navigator.clipboard && navigator.clipboard.writeText){
        navigator.clipboard.writeText(txt).then(ok, function(){ fallback(txt); });
      } else {
        fallback(txt);
      }
    });
  })();

  refresh();
  setInterval(refresh, MS);
})();
"""


def xmoney(v):
    return ar.money(v) if v else "—"


def om_key_of(r):
    v = r.get("oi_mcap_all")
    return v if v is not None else (r.get("oi_mcap") or 0)


def pick_lists(rows):
    """三个榜单的唯一取法，HTML 和文字版都调它，避免两边不一致。"""
    gain = sorted([r for r in rows if r.get("px_chg_24") is not None],
                  key=lambda r: r["px_chg_24"], reverse=True)[:5]
    sig = sorted(rows, key=lambda r: (r.get("sig_score") or 0,
                                      abs(r.get("oi_contracts_24h") or 0)),
                 reverse=True)[:10]
    om = sorted([r for r in rows if om_key_of(r) > 0],
                key=om_key_of, reverse=True)[:10]
    return gain, sig, om


def build_md(stamp, scanned, minvol, gain, sig, om, collisions, cross):
    """纯文字版：可以直接粘贴给别的 AI，不需要托管、不需要发文件。"""
    L = []
    a = L.append
    a("# 山寨币合约 · 异动榜")
    a("")
    a("生成时间：%s（北京时间）｜扫描 %d 个合约，成交额 ≥ %s"
      % (stamp, scanned, ar.money(minvol)))
    if cross and (cross.get("bybit") or cross.get("okx") or cross.get("hl")):
        a("持仓口径：币安 + Bybit + OKX + Hyperliquid 四家跨所合计"
          "（仍不含 Bitget / Gate / MEXC 等，属下限）")
    else:
        a("持仓口径：仅币安单所")
    a("价格与持仓取自同一时间窗口；涨幅榜用交易所滚动 24 小时涨跌幅。")
    a("持仓张数是净额，净额不变不代表没人止损，需配合主动买卖比判断。")
    a("")

    a("## 涨幅榜 Top5（交易所滚动 24h）")
    a("")
    a("| # | 币种 | 24h涨跌 | 持仓张数 | 流通市值 | OI/市值 | 换手 | 币安占比 |")
    a("|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(gain, 1):
        omv = r.get("oi_mcap_all")
        tv = r.get("turnover")
        sh = r.get("bin_share")
        a("| %d | %s | %s | %s | %s | %s | %s | %s |"
          % (i, r["base"], ar.sgn_pct(r.get("px_chg_24")),
             ar.sgn_pct(r.get("oi_contracts_24h")), ar.money(r.get("mcap")),
             "%.1f%%" % (omv * 100) if omv is not None else "—",
             "%.2fx" % tv if tv is not None else "—",
             "%.0f%%" % (sh * 100) if sh is not None else "—"))
    a("")

    a("## 异动 Top10（命中信号权重和）")
    a("")
    a("| # | 币种 | 价·同窗 | 持仓张数 | 名义变化 | 持仓·跨所 | 币安占比 | 流通市值 | OI/市值 | 换手 | 命中信号 | 信号分 |")
    a("|---|---|---|---|---|---|---|---|---|---|---|---|")
    names = {s[0]: s[1] for s in SIGNALS}
    for i, r in enumerate(sig, 1):
        omv = r.get("oi_mcap_all")
        tv = r.get("turnover")
        sh = r.get("bin_share")
        a("| %d | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %.1f |"
          % (i, r["base"], ar.sgn_pct(r.get("px_chg_oi")),
             ar.sgn_pct(r.get("oi_contracts_24h")), ar.sgn_pct(r.get("oi_24h")),
             ar.money(r.get("oi_total")),
             "%.0f%%" % (sh * 100) if sh is not None else "—",
             ar.money(r.get("mcap")),
             "%.1f%%" % (omv * 100) if omv is not None else "—",
             "%.2fx" % tv if tv is not None else "—",
             "/".join(names[h] for h in r["hits"]) or "—",
             r["sig_score"]))
    a("")

    a("## 持仓占市值 Top10（跨所口径）")
    a("")
    a("| # | 币种 | 价·同窗 | 持仓张数 | 持仓·跨所 | 币安占比 | Bybit | OKX | Hype | 流通市值 | OI/市值 | 换手 | 费率年化 | 命中信号 |")
    a("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(om, 1):
        omv = om_key_of(r)
        tv = r.get("turnover")
        sh = r.get("bin_share")
        a("| %d | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |"
          % (i, r["base"], ar.sgn_pct(r.get("px_chg_oi")),
             ar.sgn_pct(r.get("oi_contracts_24h")),
             ar.money(r.get("oi_total")),
             "%.0f%%" % (sh * 100) if sh is not None else "—",
             ar.money(r.get("oi_bybit")) if r.get("oi_bybit") else "—",
             ar.money(r.get("oi_okx")) if r.get("oi_okx") else "—",
             ar.money(r.get("oi_hl")) if r.get("oi_hl") else "—",
             ar.money(r.get("mcap")),
             "%.1f%%" % (omv * 100) if omv else "—",
             "%.2fx" % tv if tv is not None else "—",
             ar.sgn_pct(r.get("funding_ann"), 0),
             "/".join(names[h] for h in r["hits"]) or "—"))
    a("")

    if collisions:
        a("## 已剔除的同名不同币（各所报价对不上，判定为不同资产）")
        a("")
        for base, bpx, drop in collisions:
            a("- %s：币安 %.6g；%s" % (base, bpx,
                                       " / ".join("%s %.6g" % (v, p) for v, p in drop)))
        a("")

    a("## 口径提醒")
    a("")
    a("- 「币安占比」是币安 + Bybit + OKX 三家内部的占比，不是全球占比。")
    a("- 无流通供给的标的是代币化股票/ETF，已从各榜剔除。")
    a("- 数据来源各交易所公开接口，存在延迟与口径差异，不构成任何投资建议。")
    return "\n".join(L)


def build(rows, stamp, scanned, minvol, live=True, cross=None):
    gain, top10_sig, top10_om = pick_lists(rows)

    # 各信号命中数
    counts = {s[0]: 0 for s in SIGNALS}
    for r in rows:
        for h in r["hits"]:
            counts[h] = counts.get(h, 0) + 1

    o = []
    a = o.append
    a('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">')
    a('<meta name="viewport" content="width=device-width,initial-scale=1">')
    a('<title>山寨币合约 异动榜 %s</title><style>%s</style></head><body>' % (html.escape(stamp), CSS))
    a('<div class="wrap">')
    a('<div class="top"><div><h1>山寨币合约 · <span>异动榜</span></h1>')
    a('<div style="color:#848e9c;font-size:12.5px;margin-top:3px">'
      '八个判定信号 · 异动 Top10 · 持仓占市值 Top10</div></div>')
    a('<div class="stamp">%s<br>扫描 %d 个合约（成交额 ≥ %s）<br>Binance USDT-M 永续 · 全市场成交额榜</div>'
      % (html.escape(stamp), scanned, ar.money(minvol)))
    a('</div>')

    cov = ""
    if cross and (cross.get("bybit") or cross.get("okx") or cross.get("hl")):
        cov = ("　｜　跨所覆盖：币安 + Bybit %d + OKX %d + Hyperliquid %d 个合约（同名不同币已校验剔除）"
               % (cross.get("bybit", 0), cross.get("okx", 0), cross.get("hl", 0)))
    a('<div class="status">榜单由全市场扫描生成于 <b>%s</b>%s ｜ <i id="upd">正在获取实时数据…</i><br>'
      '页面每 5 分钟就地刷新。<b>涨幅榜会按全市场实时重算并重排</b>（这是唯一会自动重排的表）；'
      '另外两张表的排名来自生成时那次扫描，要重排请重跑脚本。</div>'
      % (html.escape(stamp), html.escape(cov)))
    a('<span id="cfg" data-minvol="%d" hidden></span>' % int(minvol))
    a('<button id="copymd" class="btn">复制榜单为文字（可直接粘贴给其他 AI）</button>')

    a('<h2>判定框架<em>八条，全部可计算；命中即在下方表格打标签</em></h2><div class="sig">')
    for key, name, cond, mean, w in SIGNALS:
        a('<div class="sc"><h3>%s <span style="color:#5e6673;font-size:11px">%s</span></h3>'
          % (html.escape(name), key))
        a('<p>条件：%s</p><u>%s</u>' % (html.escape(cond), html.escape(mean)))
        a('<div class="sci">今日命中 %d 个 · 权重 %.1f</div></div>' % (counts.get(key, 0), w))
    a('</div>')

    # 异动 Top10
    # 涨幅榜 Top5（页面会自动重算并重排）
    a('<h2>涨幅榜 Top5<em>按交易所滚动 24h 涨幅 · 唯一会自动重排的表</em></h2>')
    if not gain:
        a('<div class="empty">没有涨幅数据</div>')
    else:
        a('<table><thead><tr>')
        for h in ["#", "币种", "24h涨跌", "持仓张数", "流通市值",
                  "OI/市值·跨所", "换手", "币安占比"]:
            a('<th>%s</th>' % h)
        a('</tr></thead><tbody id="gainers">')
        for i, r in enumerate(gain, 1):
            oma = r.get("oi_mcap_all")
            tv = r.get("turnover")
            sh = r.get("bin_share")
            a('<tr><td class="rk">%d</td>' % i)
            a('<td class="sym">%s</td>' % html.escape(r["base"]))
            a(cell_chg(r.get("px_chg_24")))
            a(cell_chg(r.get("oi_contracts_24h")))
            a('<td class="mut">%s</td>' % ar.money(r.get("mcap")))
            a('<td class="%s">%s</td>'
              % ("up" if (oma or 0) >= 0.35 else "mut",
                 "%.1f%%" % (oma * 100) if oma is not None else "—"))
            a('<td class="mut">%s</td>' % ("%.2fx" % tv if tv is not None else "—"))
            a('<td class="%s">%s</td>'
              % ("mut" if (sh or 0) >= 0.85 else "down",
                 "%.0f%%" % (sh * 100) if sh is not None else "—"))
            a('</tr>')
        a('</tbody></table>')

    a('<h2>异动 Top10<em>按命中信号的权重和排序，同时命中的条目越多越靠前</em></h2>')
    if not top10_sig:
        a('<div class="empty">本次没有命中任何信号</div>')
    else:
        a('<table><thead><tr>')
        for h in ["#", "币种", "24h价·同窗", "持仓张数", "名义OI变化", "币安持仓",
                  "持仓·跨所", "币安占比", "流通市值", "OI/市值·跨所", "换手",
                  "命中信号", "信号分"]:
            a('<th>%s</th>' % h)
        a('</tr></thead><tbody>')
        for i, r in enumerate(top10_sig, 1):
            a('<tr data-sym="%s">' % html.escape(r["symbol"]))
            a('<td class="rk">%d</td>' % i)
            a('<td class="sym">%s</td>' % html.escape(r["base"]))
            a(cell_chg(r.get("px_chg_oi"), "px"))
            a(cell_chg(r.get("oi_contracts_24h"), "ct"))
            a(cell_chg(r.get("oi_24h"), "oi24"))
            a('<td class="mut" data-k="oi_bin">%s</td>' % ar.money(r.get("oi_usd")))
            a('<td class="mut" data-k="oi_total">%s</td>' % ar.money(r.get("oi_total")))
            sh = r.get("bin_share")
            a('<td class="%s" data-k="bin_share">%s</td>'
              % ("mut" if (sh or 0) < 0.85 else "down",
                 "%.0f%%" % (sh * 100) if sh is not None else "—"))
            a('<td class="mut" data-k="mcap">%s</td>' % ar.money(r.get("mcap")))
            oma = r.get("oi_mcap_all")
            a('<td class="%s" data-k="om">%s</td>'
              % ("up" if (oma or 0) >= 0.35 else "mut",
                 "%.1f%%" % (oma * 100) if oma is not None else "—"))
            tv = r.get("turnover")
            a('<td class="mut" data-k="tv">%s</td>'
              % ("%.2fx" % tv if tv is not None else "—"))
            names = {s[0]: s[1] for s in SIGNALS}
            hot = {"s4", "s5", "s8"}
            cool = {"s2"}
            badges = "".join(
                '<span class="bd%s">%s</span>'
                % (" hot" if h in hot else (" cool" if h in cool else ""), html.escape(names[h]))
                for h in r["hits"])
            a('<td style="text-align:left">%s</td>' % (badges or '<span class="mut">—</span>'))
            a('<td><span class="bar"><i style="width:%d%%"></i></span>%.1f</td>'
              % (min(100, int(r["sig_score"] / 13.5 * 100)), r["sig_score"]))
            a('</tr>')
        a('</tbody></table>')

    # 持仓占市值 Top10
    a('<h2>持仓占市值 Top10<em>持仓名义 ÷ 流通市值，越高说明合约盘子相对现货越重</em></h2>')
    if not top10_om:
        a('<div class="empty">没有可用的流通市值数据</div>')
    else:
        a('<table><thead><tr>')
        for h in ["#", "币种", "24h价·同窗", "持仓张数", "持仓·跨所", "币安占比",
                  "Bybit", "OKX", "Hype", "流通市值", "OI/市值·跨所", "换手", "费率和年化", "命中信号"]:
            a('<th>%s</th>' % h)
        a('</tr></thead><tbody>')
        for i, r in enumerate(top10_om, 1):
            a('<tr data-sym="%s">' % html.escape(r["symbol"]))
            a('<td class="rk">%d</td>' % i)
            a('<td class="sym">%s</td>' % html.escape(r["base"]))
            a(cell_chg(r.get("px_chg_oi"), "px"))
            a(cell_chg(r.get("oi_contracts_24h"), "ct"))
            a('<td class="mut" data-k="oi_total">%s</td>' % ar.money(r.get("oi_total")))
            sh = r.get("bin_share")
            a('<td class="%s" data-k="bin_share">%s</td>'
              % ("mut" if (sh or 0) < 0.85 else "down",
                 "%.0f%%" % (sh * 100) if sh is not None else "—"))
            a('<td class="mut" data-k="x_bybit">%s</td>' % xmoney(r.get("oi_bybit")))
            a('<td class="mut" data-k="x_okx">%s</td>' % xmoney(r.get("oi_okx")))
            a('<td class="mut" data-k="x_hl">%s</td>' % xmoney(r.get("oi_hl")))
            a('<td class="mut" data-k="mcap">%s</td>' % ar.money(r.get("mcap")))
            oma = r.get("oi_mcap_all")
            a('<td class="up" data-k="om">%.1f%%</td>' % (oma * 100) if oma is not None
              else '<td class="mut" data-k="om">—</td>')
            tv = r.get("turnover")
            a('<td class="mut" data-k="tv">%s</td>'
              % ("%.2fx" % tv if tv is not None else "—"))
            fr = r.get("funding_rate")
            frs = "%.4f%%" % (fr * 100) if fr is not None else "—"
            a('<td class="%s">%s / %s</td>'
              % ("up" if (fr or 0) > 0 else "down", frs, ar.sgn_pct(r.get("funding_ann"), 0)))
            names = {s[0]: s[1] for s in SIGNALS}
            badges = "".join('<span class="bd">%s</span>' % html.escape(names[h]) for h in r["hits"])
            a('<td style="text-align:left">%s</td>' % (badges or '<span class="mut">—</span>'))
            a('</tr>')
        a('</tbody></table>')

    a('<footer>'
      '<b>跨所口径</b>：持仓名义 = <b>币安 + Bybit + OKX + Hyperliquid</b> 四家合计（USD）。'
      '「币安占比」是这四家内部的占比，不是全球占比——'
      '暂未接入 Bitget / Gate / MEXC / KuCoin，所以合计只是下限，真实比例只会更高。'
      '占比低于 85% 会被标红，说明单看币安会低估这个币的真实持仓。<br>'
      '<b>Hyperliquid 不可省</b>：对 HYPE、BTC 这类合约，Hyperliquid 的持仓常常超过币安，'
      '漏掉它会把这个币的杠杆水平算错一大截。<br>'
      '<b>资产身份校验</b>：同名不同币真实存在（例如币安的 ON 是 0.09 美元级、'
      'Bybit / OKX 的 ON 是 70 美元级的另一个项目）。程序对每家报价做交叉校验，'
      '跨所价差超过 20% 即判定为不同资产并剔除该所。<br>'
      '<b>历史与快照的分工</b>：24 小时持仓变化只有币安能算（只有它提供 OI 历史序列），'
      '跨所只能算「当前有多大」。所以本页的「怎么变」是币安口径，'
      '「总共多大」是跨所口径。<br>'
      '<b>价格变化口径</b>：由持仓序列的隐含价（名义 ÷ 张数）计算，与持仓变化完全同窗口，'
      '因此与交易软件里的滚动 24 小时涨跌幅会有差异。<br>'
      '<b>流通市值</b> = 流通供给 × 合约价，供给取 CoinMarketCap 口径（币安接口内置字段），'
      '与 CoinGecko 等其他来源可能相差数倍。<br>'
      '<b>信号是筛选器，不是研究方向</b>：命中只说明结构值得看，不说明会涨会跌。'
      '持仓张数是净额，净额不变不代表没人止损，需配合主动买卖比判断。<br>'
      '数据来源各交易所公开接口，存在延迟与口径差异，<b>不构成任何投资建议</b>。'
      '</footer>')
    a('</div>')
    if live:
        a('<script>')
        a(LIVE_JS)
        a('</script>')
    a('</body></html>')
    return "\n".join(o)


def main():
    ap = argparse.ArgumentParser(description="山寨币合约异动榜")
    ap.add_argument("--top", type=int, default=400, help="扫描成交额前 N (默认 400)")
    ap.add_argument("--minvol", type=float, default=1e6, help="成交额下限 USD (默认 100万)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--proxy", type=str, default="")
    ap.add_argument("--no-cross", action="store_true",
                    help="不接 Bybit / OKX，只用币安单所口径")
    ap.add_argument("--no-live", action="store_true",
                    help="关闭页面内自动刷新（生成纯静态快照）")
    ap.add_argument("--outdir", type=str, default="")
    args = ap.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    outdir = args.outdir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    os.makedirs(outdir, exist_ok=True)
    ar.init_net(args.proxy or os.environ.get("HTTPS_PROXY"))

    print("=" * 68)
    print("  山寨币合约 · 异动榜")
    print("=" * 68)

    try:
        rows, _bench = ar.fetch_rows(args.top, "", args.workers,
                                     light=True, rank_by="volume", minvol=args.minvol)
    except Exception as e:
        print("\n[抓取失败] %s" % e)
        print("  试: --proxy http://127.0.0.1:7890  或  --top 100")
        return 1

    ar.compute_yao(rows)
    rows = [r for r in rows if r.get("mcap")]

    # ---- 跨所：把「持仓/市值」从单所口径修正为跨所口径 ----
    cross_info = None
    collisions = []
    if not args.no_cross:
        try:
            print("  取 Bybit / OKX 全市场持仓 ...")
            data, cross_info = xvenue.fetch()
            collisions = xvenue.attach(rows, data)
            for base, bpx, drop in collisions:
                for venue, vpx in drop:
                    print("    [同名不同币] %s: 币安 %.6g vs %-5s %.6g → 已剔除该所"
                          % (base, bpx, venue, vpx))
            for err in cross_info.get("errors", []):
                print("    [跨所警告] %s" % err)
            xvenue.reyao(rows)
        except Exception as e:
            print("  [提示] 跨所取数失败，退回币安单所口径: %s" % e)
            cross_info = None
    if not cross_info:
        for r in rows:
            r["oi_bybit"] = 0.0
            r["oi_okx"] = 0.0
            r["oi_hl"] = 0.0
            r["oi_total"] = r.get("oi_usd") or 0.0
            r["bin_share"] = 1.0
            r["oi_mcap_all"] = r.get("oi_mcap")

    for r in rows:
        r["hits"] = detect(r)
        r["sig_score"] = sig_score(r["hits"])

    now = datetime.now(ar.CST)
    stamp = now.strftime("%Y-%m-%d %H:%M")
    tag = now.strftime("%Y%m%d_%H%M")
    path = os.path.join(outdir, "board_%s.html" % tag)
    with open(path, "w", encoding="utf-8") as f:
        f.write(build(rows, stamp, len(rows), args.minvol,
                      live=not args.no_live, cross=cross_info))

    # 纯文字版：直接粘贴给别的 AI，不需要托管也不需要发文件
    _g, _s, _o = pick_lists(rows)
    md_path = os.path.join(outdir, "board_%s.md" % tag)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(build_md(stamp, len(rows), args.minvol, _g, _s, _o,
                         collisions, cross_info))

    print("\n  扫描 %d 个合约，命中信号分布：" % len(rows))
    for key, name, _c, _m, _w in SIGNALS:
        n = sum(1 for r in rows if key in r["hits"])
        print("    %-10s %3d 个" % (name, n))

    top = sorted(rows, key=lambda r: r["sig_score"], reverse=True)[:10]
    print("\n  异动 Top10:")
    for i, r in enumerate(top, 1):
        names = {s[0]: s[1] for s in SIGNALS}
        print("    %2d. %-10s 信号分 %4.1f  %s"
              % (i, r["base"], r["sig_score"],
                 "/".join(names[h] for h in r["hits"]) or "—"))

    print("\n  已生成:")
    print("    看板   %s" % path)
    print("    文字版 %s" % md_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
