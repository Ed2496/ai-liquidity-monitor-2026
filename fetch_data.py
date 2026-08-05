#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
2026 AI 流動性壓力測試指標 — 自動數據抓取與儀表板生成
數據來源：FRED API + Yahoo Finance (yfinance)
執行環境：GitHub Actions (每日自動排程) 或本地 Windows Task Scheduler / 手動執行

── 資料層修正版 (Edward 裸投檢查校準, 2026-08-05) ────────────────────────
本檔基於 Kimi 原始部署包，模型層閾值/權重 100% 保留（DeepSeek 原始計分卡），
僅修正會靜默出錯的資料層 bug，並加上資料來源健康檢查（provenance）：

  [FIX-1] 維度2 HY 利差單位：FRED BAMLH0A0HYM2 單位為「百分比」(約3~3.5)，
          非 bps。原碼直接拿去跟 400/600 bps 比 → 永遠 0 分。改：抓取後 ×100 轉 bps。
  [FIX-2] 維度4 ticker COR：CoreSite 2021 底被 American Tower 併購下市，
          COR 現為 Cencora(藥品通路商)。移除，改用 DLR + EQIX + IRM 資料中心 REIT。
  [FIX-3] REIT 殖利率：不用 info["dividendYield"](版本會×100成300%)，
          改用 TTM 實配息 ÷ 現價 這條確定性算法為主路徑。
  [FIX-4] divs.last("365D") 在 pandas 2.2+ 已移除 → 改用 index 布林遮罩。
  [FIX-5] 維度1「SOFR 跳升 >0.5%」：原碼降格成「SOFR−DGS10 水位差」。
          改：抓 SOFR 近 ~7 筆序列，算真實 delta(最新 vs 5 個交易日前)。
  [ADD-1] 補抓 ^VIX 當 context 顯示(決策樹 >35 閘門引用)，不計分、不動五維權重。
  [ADD-2] 每個維度標 provenance：LIVE / FALLBACK / PROXY / OVERRIDE，
          「這格是真數據還是預設值」直接看得見(裸投檢查焊進儀表板)。
  [FIX-6] 計分刻度：DeepSeek 宣告滿分100/閾值60-40-30，但 raw(0/5/10)×小數權重
          總分上限只有 10 → 紅綠燈永遠碰不到 60，每天都落「進場」區(全案最嚴重承筐無實)。
          改 total = Σ raw×weight×10，讓實作對齊 DeepSeek 自己宣告的 100 分刻度。
          Python weighted_total 與 HTML 內 JS calc() 同步修正。
  [FLAG]  維度4 方向性斷層(買訊號被加進賣總分)保留 DeepSeek 原始邏輯，
          僅在儀表板標紅提示，坍縮權在 Edward。
─────────────────────────────────────────────────────────────────────
"""

import os
import json
import csv
import requests
import numpy as np
import pandas as pd
from datetime import datetime

# 資料來源健康檢查（provenance）：raw key -> "live" | "fallback" | "proxy" | "override"
PROVENANCE = {}

# yfinance 導入
try:
    import yfinance as yf
except ImportError:
    yf = None

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")

# ============ 數據抓取函數 ============

def fetch_fred(series_id, fallback=None):
    """從 FRED API 抓取最新有效觀測值。回傳 (value, is_live)。
    is_live=True 代表真的抓到即時數據；False 代表落回 fallback 預設值。"""
    if not FRED_API_KEY:
        print(f"[WARN] FRED_API_KEY 未設定，使用預設值 {fallback}")
        return fallback, False
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 30,  # 抓多筆，跳過 "." 缺值取最新有效值
    }
    try:
        r = requests.get(url, params=params, timeout=30)
        data = r.json()
        if "observations" in data and data["observations"]:
            for obs in data["observations"]:  # desc 排序，第一個有效即最新
                val = obs["value"]
                if val not in (".", "", None):
                    return float(val), True
    except Exception as e:
        print(f"[ERROR] FRED {series_id}: {e}")
    print(f"[WARN] FRED {series_id} 抓取失敗，使用預設值 {fallback}")
    return fallback, False


def fetch_fred_series(series_id, n=10):
    """抓取 FRED 序列近 n 筆有效值（新→舊）。用於計算變化量（如 SOFR 跳升）。
    回傳 list[float]（新到舊排序），失敗回傳 []。"""
    if not FRED_API_KEY:
        return []
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": max(n * 3, 30),  # 多抓以跳過缺值
    }
    try:
        r = requests.get(url, params=params, timeout=30)
        data = r.json()
        vals = []
        if "observations" in data and data["observations"]:
            for obs in data["observations"]:
                v = obs["value"]
                if v not in (".", "", None):
                    vals.append(float(v))
                if len(vals) >= n:
                    break
        return vals
    except Exception as e:
        print(f"[ERROR] FRED series {series_id}: {e}")
        return []


def fetch_vix():
    """抓取 ^VIX 最新收盤（context 用，不計分）。回傳 (value, is_live)。"""
    hist = fetch_yf_history("^VIX", period="5d")
    if hist is not None and not hist.empty:
        try:
            return float(hist["Close"].iloc[-1]), True
        except Exception as e:
            print(f"[ERROR] VIX parse: {e}")
    return None, False


def fetch_yf_history(ticker, period="1mo"):
    """從 Yahoo Finance 抓歷史價格"""
    if yf is None:
        return None
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period=period)
        if not hist.empty:
            return hist
    except Exception as e:
        print(f"[ERROR] YF history {ticker}: {e}")
    return None


def calc_volatility(ticker, period="1mo"):
    """計算年化波動率 (%)"""
    hist = fetch_yf_history(ticker, period)
    if hist is None or len(hist) < 5:
        return None
    returns = hist["Close"].pct_change().dropna()
    if len(returns) < 3:
        return None
    vol = returns.std() * np.sqrt(252) * 100
    return float(vol)


def fetch_reit_yield(ticker):
    """估算 REIT 殖利率 (%)。
    主路徑：TTM(近365日)實配息總額 ÷ 現價 — 確定性、不受 yfinance 欄位版本影響。
    次路徑：info["dividendYield"]（已知版本間單位不一，僅作 sanity 對照）。
    回傳 float 或 None。"""
    if yf is None:
        return None
    try:
        t = yf.Ticker(ticker)

        # ── 主路徑：TTM 實配息 / 現價（FIX-3 / FIX-4）──
        divs = t.dividends
        if divs is not None and len(divs) >= 1:
            # FIX-4: 不用已被移除的 .last("365D")，改 index 布林遮罩
            idx = divs.index
            try:
                cutoff = idx.max() - pd.Timedelta(days=365)
                ttm_div = float(divs[idx > cutoff].sum())
            except Exception:
                # 極端 fallback：取最後 4 筆（季配）
                ttm_div = float(divs.tail(4).sum())
            hist = t.history(period="5d")
            if not hist.empty:
                price = float(hist["Close"].iloc[-1])
                if price > 0 and ttm_div > 0:
                    y = (ttm_div / price) * 100.0
                    # sanity：REIT 殖利率合理區間 0~30%
                    if 0 < y < 30:
                        return y

        # ── 次路徑：info dividendYield（單位版本不穩，需 normalize）──
        info = t.info
        if info and info.get("dividendYield"):
            dy = float(info["dividendYield"])
            # 版本 A: 小數(0.035) → ×100；版本 B: 已是百分數(3.5) → 不乘
            y = dy * 100.0 if dy < 1 else dy
            if 0 < y < 30:
                return y
    except Exception as e:
        print(f"[ERROR] REIT {ticker}: {e}")
    return None


# ============ 分數計算 ============

def load_config():
    if os.path.exists("config.json"):
        with open("config.json", "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def score_dim1(dgs10, sofr, sofr_jump=None, override=None):
    """FIX-5: 「SOFR 跳升 >0.5%」是變化量，不是 SOFR−DGS10 水位差。
    sofr_jump = 最新 SOFR − 5 個交易日前 SOFR（由 SOFR 序列算出）。
    若無法取得 sofr_jump（None），退回僅用水位門檻判定（保守，不會誤報極度壓力）。"""
    if override is not None:
        return int(override)
    if dgs10 is not None and sofr is not None:
        jump_ok = (sofr_jump is not None and sofr_jump > 0.5)
        if dgs10 > 4.5 and jump_ok:
            return 10
        if dgs10 >= 4.0:
            return 5
    return 0


def score_dim2(hy_spread, override=None):
    if override is not None:
        return int(override)
    if hy_spread is not None:
        if hy_spread > 600:
            return 10
        if hy_spread >= 400:
            return 5
    return 0


def score_dim3(usdjpy_vol, override=None):
    if override is not None:
        return int(override)
    if usdjpy_vol is not None:
        if usdjpy_vol > 18:
            return 10
        if usdjpy_vol >= 10:
            return 5
    return 0


def score_dim4(reit_yield, dgs10, override=None):
    if override is not None:
        return int(override)
    if reit_yield is not None and dgs10 is not None:
        spread = reit_yield - dgs10
        if spread > 3.0:
            return 10
        if spread >= 1.0:
            return 5
    return 0


def score_dim5(cny_vol, override=None):
    """
    維度5: 地緣防火牆。
    因 SGE vs LBMA 價差無穩定免費 API，此處以「人民幣(離岸)30日波動率」
    作為「境內資本恐慌/管制壓力」的替代指標。
    用戶可在 config.json 中手動覆蓋為真實 SGE/LBMA 價差對應分數。
    """
    if override is not None:
        return int(override)
    if cny_vol is not None:
        if cny_vol > 8:
            return 10
        if cny_vol >= 2:
            return 5
    return 0


def weighted_total(scores):
    """FIX-6: DeepSeek 宣告「滿分100、閾值60/40/30」，但 raw 分為 0/5/10、
    權重為小數，Σ raw×weight 上限只有 10 → 60 分閾值永遠碰不到，紅綠燈全死。
    正解：total = Σ raw × weight × 10，讓實作對齊 DeepSeek 自己宣告的 100 分刻度。
    (raw=10 全滿 → Σ 10×weight×10 = 100×Σweight = 100，與各維度 /20 /25 /15 分母一致)"""
    weights = {"d1": 0.20, "d2": 0.25, "d3": 0.15, "d4": 0.25, "d5": 0.15}
    total = 0
    for k, v in scores.items():
        total += v * weights[k] * 10
    return round(total)


# ============ HTML 生成 ============

def generate_html(raw, scores, total, update_time, prov=None, history=None):
    prov = prov or {}
    history = history or []

    # 映射分數到下拉選項的 value
    def val_to_option(v):
        return {0: 0, 5: 1, 10: 2}.get(v, 0)

    sel = {k: val_to_option(v) for k, v in scores.items()}

    # provenance 徽章：把「這格是真數據還是預設值」變成可見標記（裸投檢查焊進儀表板）
    _prov_meta = {
        "live":     ("LIVE",     "#2e7d32", "#e8f5e9"),
        "partial":  ("PARTIAL",  "#f9a825", "#fffde7"),
        "proxy":    ("PROXY",    "#f9a825", "#fffde7"),
        "fallback": ("FALLBACK", "#9e9e9e", "#f0f0f0"),
        "override": ("OVERRIDE", "#1565c0", "#e3f2fd"),
    }
    def prov_badge(raw_key, dim_key=None):
        # 手動覆蓋優先顯示 OVERRIDE
        if dim_key and prov.get(f"{dim_key}_override"):
            label, fg, bg = _prov_meta["override"]
        else:
            st = prov.get(raw_key, "fallback")
            label, fg, bg = _prov_meta.get(st, _prov_meta["fallback"])
        return (f'<span style="display:inline-block;font-size:11px;font-weight:700;'
                f'padding:1px 7px;border-radius:4px;color:{fg};background:{bg};'
                f'border:1px solid {fg};margin-left:6px;vertical-align:middle;">{label}</span>')

    # 狀態判定
    d4_raw = scores["d4"]
    if total > 60:
        status_cls = "status-exit"
        status_text = "🚨 離場訊號"
        status_detail = "總分 > 60 分。維度2（高收益利差）與維度3（日圓波動）同時爆表，穿境資本正在瘋狂平倉，市場進入「無差別拋售」階段。此時不要接刀，最深的「實體資產法拍」尚未到來。"
        action_cls = "action-exit"
        action_title = "🚨 離場行動"
        action_body = "將股票/加密貨幣部位降至 20% 以下，持有短期（1-3 個月）美國國庫券（T-bills）或美元現金。等待市場恐慌進一步釋放。"
    elif total >= 40:
        status_cls = "status-watch"
        status_text = "🟡 觀察期"
        status_detail = "總分 40–60 分。開始研究「目標實體基建名單」，確認現金流覆蓋倍數（DSCR）> 1.5 倍，為進場做準備。"
        action_cls = "action-watch"
        action_title = "🟡 觀察期行動"
        action_body = "開始研究「目標實體基建名單」（特定資料中心 REITs、擁有長期電力合約的發電廠、已出租給 AWS/Microsoft 的機房開發商）。<br><br><strong>關鍵動作：</strong>確認目標公司的「現金流覆蓋倍數（DSCR）」是否大於 1.5 倍。若大於，代表即使經濟衰退，仍有現金還債，不會倒閉。"
    else:
        if d4_raw >= 10:
            status_cls = "status-enter"
            status_text = "🟢 進場訊號"
            status_detail = "總分 < 30 分，且維度 4（REITs 利差）> 3%。實體基建的租金回報率已遠遠超過借貸成本，這是 2026 年唯一具備「安全邊際」的資產類別。"
            action_cls = "action-enter"
            action_title = "🟢 進場行動（反市場時刻）"
            action_body = "大膽進場承接「維度 4」中利差最大的 REITs，或購買擁有大量資料中心抵押品的「不良債券基金（Distressed Debt Fund）」。<br><br>歷史對照：這就像 1988 年 KKR 眼中的 RJR 菸草公司——股價雖跌，但香菸（算力）仍在每天生產現金流。只要確定租約（合約）是長期的，它就是「被錯殺的現金奶牛」。"
        else:
            status_cls = "status-watch"
            status_text = "🟡 預備觀察"
            status_detail = "總分雖低（< 30），但維度 4（REITs 利差）尚未達到 >3% 的強烈買進訊號。繼續等待實體資產被錯殺的時機。"
            action_cls = "action-watch"
            action_title = "🟡 預備觀察"
            action_body = "總分雖低，但實體基建利差尚未達到 >3% 的強烈買進訊號。持續監控資料中心 REITs 報價與 10 年期公債利差，等待黃金坑出現。"

    # 原始值格式化
    fmt = lambda x, d=2: f"{x:.{d}f}" if x is not None else "N/A"
    reit_spread = (raw.get("REIT_YIELD", 0) - raw.get("DGS10", 0)) if raw.get("REIT_YIELD") and raw.get("DGS10") else None

    # 資料源健康檢查面板（把 provenance 攤開成一張表）
    _health_items = [
        ("DGS10（10Y公債）", "DGS10", "FRED"),
        ("SOFR（隔夜利率）", "SOFR", "FRED"),
        ("HY 利差（BAMLH0A0HYM2, ×100→bps）", "HY_SPREAD", "FRED"),
        ("USD/JPY 波動率", "USDJPY_VOL", "Yahoo Finance"),
        (f"資料中心 REIT 殖利率（{raw.get('REIT_TICKERS','DLR,EQIX,IRM')}）", "REIT_YIELD", "Yahoo Finance"),
        ("CNH 離岸波動率（維度5替代指標）", "CNY_VOL", "Yahoo Finance"),
        ("VIX（context, 不計分）", "VIX", "Yahoo Finance"),
    ]
    _health_rows = []
    for name, key, src in _health_items:
        st = prov.get(key, "fallback")
        label, fg, bg = _prov_meta.get(st, _prov_meta["fallback"])
        _health_rows.append(
            f'<tr><td style="padding:6px 10px;border-bottom:1px solid #eee;">{name}</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee;color:#777;">{src}</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee;text-align:right;">'
            f'<span style="font-size:11px;font-weight:700;padding:1px 7px;border-radius:4px;'
            f'color:{fg};background:{bg};border:1px solid {fg};">{label}</span></td></tr>'
        )
    n_live = sum(1 for _, k, _ in _health_items if prov.get(k) == "live")
    n_total = len(_health_items)
    n_override = sum(1 for i in range(1, 6) if prov.get(f"d{i}_override"))
    health_panel = (
        '<div class="section-title">資料源健康檢查（裸投檢查）</div>'
        '<div style="background:#fafafa;border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:8px;">'
        f'<div style="font-size:14px;margin-bottom:10px;">即時抓取 <strong style="color:#2e7d32;">{n_live}/{n_total}</strong> 項為 LIVE；'
        f'手動覆蓋 <strong style="color:#1565c0;">{n_override}</strong> 個維度。'
        'FALLBACK=落回預設值(未設 FRED_API_KEY 或抓取失敗)，該格數字不可當真數據；'
        'PROXY=替代指標(非目標本尊)；OVERRIDE=你在 config.json 手動指定。</div>'
        '<table style="width:100%;border-collapse:collapse;font-size:14px;">'
        '<tr style="color:#555;font-size:12px;"><td style="padding:6px 10px;">指標</td>'
        '<td style="padding:6px 10px;">來源</td><td style="padding:6px 10px;text-align:right;">狀態</td></tr>'
        + "".join(_health_rows) +
        '</table></div>'
    )

    # 趨勢面板（讀歷史畫 sparkline；含 fallback 讀數標記）
    if len(history) >= 2:
        pts_total = [(h["date"], h["total"]) for h in history]
        pts_spread = [(h["date"], h["reit_spread"]) for h in history]
        spark_total = sparkline_svg(pts_total, color="#1e3a5f")
        spark_spread = sparkline_svg(pts_spread, color="#2e7d32", zero_line=True)
        first_d, last_d = history[0]["date"], history[-1]["date"]
        n_fb = sum(1 for h in history if h.get("has_fallback"))
        fb_note = (f'<span style="color:#c62828;">（其中 {n_fb} 筆含 FALLBACK，趨勢判讀請剔除）</span>'
                   if n_fb else '')
        last_total = history[-1]["total"]
        last_spread = history[-1]["reit_spread"]
        prev_total = history[-2]["total"] if len(history) >= 2 else None
        delta = (f'{last_total - prev_total:+.0f}' if prev_total is not None else '—')
        trend_panel = (
            '<div class="section-title">歷史趨勢（時間序列）</div>'
            '<div style="background:#fafafa;border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:8px;">'
            f'<div style="font-size:13px;color:var(--text-secondary);margin-bottom:12px;">'
            f'共 {len(history)} 筆讀數（{first_d} → {last_d}）{fb_note}。每日一筆，git commit 逐日鎖定。</div>'
            '<div style="display:flex;gap:16px;flex-wrap:wrap;">'
            '<div style="flex:1;min-width:240px;">'
            f'<div style="font-size:13px;color:var(--text-secondary);margin-bottom:4px;">壓力總分（今日 {last_total}，日變化 {delta}）</div>'
            f'{spark_total}</div>'
            '<div style="flex:1;min-width:240px;">'
            f'<div style="font-size:13px;color:var(--text-secondary);margin-bottom:4px;">REIT 利差 %（今日 {last_spread}，紅虛線=0，翻正並過+3%=進場閘門）</div>'
            f'{spark_spread}</div>'
            '</div></div>'
        )
    else:
        trend_panel = (
            '<div class="section-title">歷史趨勢（時間序列）</div>'
            '<div style="background:#fafafa;border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:8px;'
            'font-size:14px;color:var(--text-secondary);">歷史僅 '
            f'{len(history)} 筆——趨勢圖需 ≥2 筆。每天自動累積一筆（git commit 鎖定），明後天起這裡會長出折線。</div>'
        )

    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>2026 AI 流動性壓力測試指標 — 自動監控儀表板</title>
<style>
  :root {{
    --bg:#ffffff; --text:#1a1a1a; --text-secondary:#555555; --border:#d0d0d0;
    --accent:#1e3a5f; --accent-light:#e8f0fe; --danger:#c62828; --danger-bg:#ffebee;
    --warn:#f9a825; --warn-bg:#fffde7; --safe:#2e7d32; --safe-bg:#e8f5e9;
    --stage1:#5c6bc0; --stage2:#7e57c2; --stage3:#d32f2f; --stage4:#ef6c00;
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{
    font-family:"Noto Sans TC","Microsoft JhengHei","PingFang TC",sans-serif;
    background:var(--bg); color:var(--text); line-height:1.7;
    padding:24px; max-width:1100px; margin:0 auto;
  }}
  h1 {{ font-size:28px; font-weight:700; color:var(--accent); margin-bottom:4px; letter-spacing:-0.5px; }}
  .subtitle {{ font-size:15px; color:var(--text-secondary); margin-bottom:24px; }}
  .update-time {{ font-size:13px; color:var(--text-secondary); margin-bottom:16px; text-align:right; }}
  .score-board {{
    background:var(--accent-light); border:2px solid var(--accent); border-radius:12px;
    padding:24px; margin-bottom:28px; display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:16px;
  }}
  .score-circle-wrap {{ text-align:center; min-width:140px; }}
  .score-circle {{
    width:120px; height:120px; border-radius:50%; border:6px solid var(--border);
    display:flex; flex-direction:column; align-items:center; justify-content:center;
    margin:0 auto 8px; transition:all 0.4s ease; background:#fff;
  }}
  .score-num {{ font-size:36px; font-weight:800; line-height:1; }}
  .score-label {{ font-size:13px; color:var(--text-secondary); margin-top:4px; }}
  .status-badge {{
    display:inline-block; padding:10px 24px; border-radius:8px;
    font-size:20px; font-weight:700; letter-spacing:1px; transition:all 0.3s ease;
  }}
  .status-exit {{ background:var(--danger-bg); color:var(--danger); border:2px solid var(--danger); }}
  .status-watch {{ background:var(--warn-bg); color:#5d4037; border:2px solid var(--warn); }}
  .status-enter {{ background:var(--safe-bg); color:var(--safe); border:2px solid var(--safe); }}
  .status-detail {{ font-size:15px; color:var(--text-secondary); margin-top:8px; max-width:500px; }}
  .section-title {{
    font-size:20px; font-weight:700; color:var(--accent);
    margin:32px 0 16px; padding-bottom:8px; border-bottom:2px solid var(--accent-light);
  }}
  .dim-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:16px; margin-bottom:24px; }}
  .dim-card {{
    border:1px solid var(--border); border-radius:10px; padding:16px; background:#fafafa; transition:box-shadow 0.2s;
  }}
  .dim-card:hover {{ box-shadow:0 2px 8px rgba(0,0,0,0.06); }}
  .dim-header {{ display:flex; justify-content:space-between; align-items:baseline; margin-bottom:10px; }}
  .dim-name {{ font-size:16px; font-weight:700; color:var(--accent); }}
  .dim-weight {{ font-size:13px; color:var(--text-secondary); background:#e0e0e0; padding:2px 8px; border-radius:4px; }}
  .dim-indicator {{ font-size:14px; color:var(--text-secondary); margin-bottom:10px; }}
  .dim-raw {{ font-size:13px; color:var(--danger); font-weight:600; margin-bottom:8px; }}
  .dim-select {{
    width:100%; padding:10px 12px; font-size:15px;
    border:1px solid var(--border); border-radius:6px; background:#fff; color:var(--text); cursor:pointer;
  }}
  .dim-score {{ text-align:right; font-size:14px; font-weight:700; margin-top:8px; color:var(--text-secondary); }}
  .action-box {{ margin-top:24px; padding:20px; border-radius:10px; border:2px solid; font-size:15px; }}
  .action-exit {{ background:var(--danger-bg); border-color:var(--danger); }}
  .action-watch {{ background:var(--warn-bg); border-color:var(--warn); }}
  .action-enter {{ background:var(--safe-bg); border-color:var(--safe); }}
  .footer-note {{
    margin-top:32px; padding-top:16px; border-top:1px solid var(--border);
    font-size:13px; color:var(--text-secondary); text-align:center;
  }}
  .highlight {{ background:#fff9c4; padding:0 4px; font-weight:600; }}
  .note-box {{
    background:#fffde7; border:1px solid var(--warn); border-radius:8px;
    padding:12px 16px; font-size:14px; color:#5d4037; margin-bottom:20px;
  }}
  @media (max-width:600px) {{
    body {{ padding:16px; }}
    h1 {{ font-size:22px; }}
    .score-board {{ flex-direction:column; text-align:center; }}
  }}
</style>
</head>
<body>

<h1>2026 AI 流動性壓力測試指標</h1>
<p class="subtitle">穿境者監控儀表板（Channel Fault Line Monitor）— 基於五維度加權計分</p>
<div class="update-time">數據自動更新時間：{update_time}（UTC+8）　·　<a href="history.html" style="color:var(--accent);">📈 離線歷史檢視器</a></div>

<div class="note-box">
  <strong>⚠️ 自動化聲明（資料層已校準 2026-08-05）：</strong>維度 1–4 來自 FRED / Yahoo Finance 自動抓取。
  已修正：HY利差單位(百分比×100→bps)、REIT ticker(移除已成藥廠的 COR，改 DLR/EQIX/IRM)、
  殖利率改 TTM實配息/現價、SOFR跳升改真實變化量。
  <strong>維度 5（地緣防火牆）</strong>因 SGE/LBMA 無穩定免費 API，以「CNH離岸人民幣波動率」為替代指標(標 PROXY)。
  請每日手動核對真實 SGE/LBMA 價差，並在 <code>config.json</code> 的 <code>dim5_override</code> 覆蓋分數。
  <strong>每格右側徽章顯示該數字是 LIVE 真數據還是 FALLBACK 預設值。</strong>
</div>

<div class="score-board">
  <div class="score-circle-wrap">
    <div class="score-circle" id="scoreCircle" style="border-color:#{'c62828' if total>60 else ('f9a825' if total>=40 else '2e7d32')}">
      <div class="score-num" id="totalScore">{total}</div>
      <div class="score-label">壓力總分</div>
    </div>
  </div>
  <div style="flex:1; min-width:260px;">
    <div class="status-badge {status_cls}" id="statusBadge">{status_text}</div>
    <div class="status-detail" id="statusDetail">{status_detail}</div>
  </div>
</div>

<div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px;">
  <div style="flex:1;min-width:200px;border:1px solid var(--border);border-radius:8px;padding:12px 16px;background:#fafafa;">
    <div style="font-size:13px;color:var(--text-secondary);">VIX 恐慌指數（context，不計分）{prov_badge('VIX')}</div>
    <div style="font-size:24px;font-weight:800;color:{'#c62828' if (raw.get('VIX') or 0) > 35 else ('#f9a825' if (raw.get('VIX') or 0) >= 20 else '#2e7d32')};">{fmt(raw.get('VIX'))}</div>
    <div style="font-size:12px;color:var(--text-secondary);">決策樹閘門：突破 35 後回落 → 觀察維度4進場時機</div>
  </div>
  <div style="flex:1;min-width:200px;border:1px solid var(--border);border-radius:8px;padding:12px 16px;background:#fafafa;">
    <div style="font-size:13px;color:var(--text-secondary);">實體基建利差（REIT殖利率 − 10Y公債）</div>
    <div style="font-size:24px;font-weight:800;color:{'#2e7d32' if (reit_spread or 0) > 3 else 'var(--accent)'};">{fmt(reit_spread)}%</div>
    <div style="font-size:12px;color:var(--text-secondary);">&gt;3% = 強烈買進訊號（維度4 進場閘門）</div>
  </div>
</div>

{trend_panel}

<div class="section-title">一、五維度壓力評分（滿分 100）</div>
<div class="dim-grid">

  <div class="dim-card">
    <div class="dim-header">
      <span class="dim-name">1. 資金成本</span>
      <span class="dim-weight">權重 20%</span>    </div>
    <div class="dim-indicator">美國 10 年期國債收益率 + SOFR</div>
    <div class="dim-raw">自動抓取：DGS10={fmt(raw.get('DGS10'))}% / SOFR={fmt(raw.get('SOFR'))}% / SOFR跳升={fmt(raw.get('SOFR_JUMP')) if raw.get('SOFR_JUMP') is not None else 'N/A'}%{prov_badge('DGS10','d1')}</div>
    <select class="dim-select" id="d1" onchange="calc()">
      <option value="0" {'selected' if sel['d1']==0 else ''}>正常：&lt; 4.0%（0 分）</option>
      <option value="5" {'selected' if sel['d1']==1 else ''}>警戒：4.0% - 4.5%（+5 分）</option>
      <option value="10" {'selected' if sel['d1']==2 else ''}>極度壓力：&gt; 4.5% 且 SOFR 跳升 &gt; 0.5%（+10 分）</option>
    </select>
    <div class="dim-score">加權得分：<span id="s1">{round(scores['d1']*0.20*10)}</span> / 20</div>
  </div>

  <div class="dim-card">
    <div class="dim-header">
      <span class="dim-name">2. 利差崩潰預警</span>
      <span class="dim-weight">權重 25%</span>
    </div>
    <div class="dim-indicator">高收益債 (HY) vs 公債利差；AI 公司債 CDS</div>
    <div class="dim-raw">自動抓取：HY Spread={fmt(raw.get('HY_SPREAD'),1)} bps（FRED 百分比×100 已修正）{prov_badge('HY_SPREAD','d2')}</div>
    <select class="dim-select" id="d2" onchange="calc()">
      <option value="0" {'selected' if sel['d2']==0 else ''}>正常：&lt; 400 bps（0 分）</option>
      <option value="5" {'selected' if sel['d2']==1 else ''}>警戒：400 - 600 bps（+5 分）</option>
      <option value="10" {'selected' if sel['d2']==2 else ''}>極度壓力：&gt; 600 bps（+10 分）</option>
    </select>
    <div class="dim-score">加權得分：<span id="s2">{round(scores['d2']*0.25*10)}</span> / 25</div>
  </div>

  <div class="dim-card">
    <div class="dim-header">
      <span class="dim-name">3. 日圓套利解除壓力</span>
      <span class="dim-weight">權重 15%</span>
    </div>
    <div class="dim-indicator">美元/日圓 (USD/JPY) 30 日波動率</div>
    <div class="dim-raw">自動抓取：30日已實現波動率(年化)={fmt(raw.get('USDJPY_VOL'))}%{prov_badge('USDJPY_VOL','d3')}</div>
    <select class="dim-select" id="d3" onchange="calc()">
      <option value="0" {'selected' if sel['d3']==0 else ''}>正常：&lt; 10%（0 分）</option>
      <option value="5" {'selected' if sel['d3']==1 else ''}>警戒：10% - 18%（+5 分）</option>
      <option value="10" {'selected' if sel['d3']==2 else ''}>極度壓力：&gt; 18%（+10 分）</option>
    </select>
    <div class="dim-score">加權得分：<span id="s3">{round(scores['d3']*0.15*10)}</span> / 15</div>
  </div>

  <div class="dim-card">
    <div class="dim-header">
      <span class="dim-name">4. 實體基建「錯殺」機會</span>
      <span class="dim-weight">權重 25%</span>
    </div>
    <div class="dim-indicator">資料中心 REITs 殖利率 vs 10 年期公債利差</div>
    <div class="dim-raw">自動抓取：REITs平均殖利率={fmt(raw.get('REIT_YIELD'))}%（{raw.get('REIT_TICKERS','DLR,EQIX,IRM')}）/ 利差={fmt(reit_spread)}%{prov_badge('REIT_YIELD','d4')}</div>
    <div style="font-size:12px;color:#c62828;background:#ffebee;border:1px solid #c62828;border-radius:6px;padding:6px 10px;margin-bottom:8px;">⚠️ 方向性斷層：本維度是「買點越強分數越高」，卻被加進「分數越高越該賣」的總分。買訊號(+10)會把總分推向離場，與它要觸發的「總分&lt;30進場」閘門相衝。此為 DeepSeek 原始計分卡的結構，已保留未改——是否把本維度拆成獨立「機會軸」不進總分，坍縮權在你。</div>
    <select class="dim-select" id="d4" onchange="calc()">
      <option value="0" {'selected' if sel['d4']==0 else ''}>正常：&lt; 1%（0 分）</option>
      <option value="5" {'selected' if sel['d4']==1 else ''}>警戒：1% - 2.5%（+5 分）</option>
      <option value="10" {'selected' if sel['d4']==2 else ''}>強烈買進訊號：&gt; 3%（+10 分）</option>
    </select>
    <div class="dim-score">加權得分：<span id="s4">{round(scores['d4']*0.25*10)}</span> / 25</div>
  </div>

  <div class="dim-card">
    <div class="dim-header">
      <span class="dim-name">5. 地緣防火牆（中國變數）</span>
      <span class="dim-weight">權重 15%</span>
    </div>
    <div class="dim-indicator">上海金 (SGE) vs 倫敦金 (LBMA) 價差</div>
    <div class="dim-raw">自動抓取：CNH離岸30日波動率(年化)={fmt(raw.get('CNY_VOL'))}%（非真 SGE/LBMA 價差，替代指標）{prov_badge('CNY_VOL','d5')}</div>
    <select class="dim-select" id="d5" onchange="calc()">
      <option value="0" {'selected' if sel['d5']==0 else ''}>正常：&lt; 2%（0 分）</option>
      <option value="5" {'selected' if sel['d5']==1 else ''}>警戒：2% - 8%（+5 分）</option>
      <option value="10" {'selected' if sel['d5']==2 else ''}>極度壓力：&gt; 8%（+10 分）</option>
    </select>
    <div class="dim-score">加權得分：<span id="s5">{round(scores['d5']*0.15*10)}</span> / 15</div>
  </div>

</div>

{health_panel}

<div class="action-box {action_cls}" id="actionBox">
  <strong style="font-size:17px; display:block; margin-bottom:8px;">{action_title}</strong>
  <div id="actionText">{action_body}</div>
</div>

<div class="section-title">二、可存檔決策樹（離場 / 觀察 / 進場）</div>
<div style="background:#fafafa; border:1px solid var(--border); border-radius:10px; padding:20px;">
  <div style="display:flex; align-items:flex-start; gap:12px; margin-bottom:14px; padding:12px; border-radius:8px; background:#fff; border:1px solid #e0e0e0;">
    <div style="min-width:32px; height:32px; border-radius:50%; background:var(--accent); color:#fff; display:flex; align-items:center; justify-content:center; font-weight:700; font-size:15px; flex-shrink:0;">1</div>
    <div style="font-size:15px;">
      <strong style="color:var(--accent);">先看「日圓（維度3）」與「高收益債（維度2）」：</strong><br>
      若雙雙破表（總分 &gt; 60），<span class="highlight">立刻離場</span>，別想抄底。將股票/加密貨幣部位降至 20% 以下，持有短期（1-3 個月）美國國庫券（T-bills）或美元現金。
    </div>
  </div>
  <div style="display:flex; align-items:flex-start; gap:12px; margin-bottom:14px; padding:12px; border-radius:8px; background:#fff; border:1px solid #e0e0e0;">
    <div style="min-width:32px; height:32px; border-radius:50%; background:var(--accent); color:#fff; display:flex; align-items:center; justify-content:center; font-weight:700; font-size:15px; flex-shrink:0;">2</div>
    <div style="font-size:15px;">
      <strong style="color:var(--accent);">等待 VIX 恐慌指數突破 35 後回落：</strong><br>
      此時觀察「資料中心 REITs 殖利率（維度4）」。總分 40–60 分為觀察期，開始研究目標實體基建名單，確認 DSCR &gt; 1.5 倍。
    </div>
  </div>
  <div style="display:flex; align-items:flex-start; gap:12px; padding:12px; border-radius:8px; background:#fff; border:1px solid #e0e0e0;">
    <div style="min-width:32px; height:32px; border-radius:50%; background:var(--accent); color:#fff; display:flex; align-items:center; justify-content:center; font-weight:700; font-size:15px; flex-shrink:0;">3</div>
    <div style="font-size:15px;">
      <strong style="color:var(--accent);">當 REITs 殖利率比 10 年期公債高出 3% 以上時：</strong><br>
      總分 &lt; 30 分且維度 4 &gt; 3%，這是 2026 年唯一具備「安全邊際（Margin of Safety）」的資產類別。大膽進場承接利差最大的 REITs，或購買不良債券基金。
    </div>
  </div>
</div>

<div class="footer-note">
  終局判斷：這不是 2008 年「銀行倒閉」的瞬間凍結，而是一場「慢動作的資產所有權轉移」。<br>
  流動性緊縮消滅的是「無現金流的槓桿投機客」，而不是「有長期租約的實體基建」。<br>
  你的任務：當穿境資本砸出黃金坑時，替換掉他們手中的槓桿，拿走他們的實體抵押品。<br><br>
  <strong>數據來源：</strong>FRED (DGS10, SOFR, BAMLH0A0HYM2) | Yahoo Finance (USDJPY, DLR/EQIX/IRM, CNH, ^VIX)<br>
  <strong>自動執行：</strong>GitHub Actions 每日排程 或 Windows Task Scheduler | <strong>倉庫：</strong>請填入你的 GitHub 倉庫位址
</div>

<script>
function calc() {{
  const w = [0.20, 0.25, 0.15, 0.25, 0.15];
  const ids = ['d1','d2','d3','d4','d5'];
  const sIds = ['s1','s2','s3','s4','s5'];
  let total = 0;
  for (let i = 0; i < 5; i++) {{
    const v = parseInt(document.getElementById(ids[i]).value);
    const weighted = Math.round(v * w[i] * 10);  // FIX-6: ×10 對齊 100 分刻度
    document.getElementById(sIds[i]).textContent = weighted;
    total += weighted;
  }}
  document.getElementById('totalScore').textContent = total;
  const circle = document.getElementById('scoreCircle');
  const badge = document.getElementById('statusBadge');
  const detail = document.getElementById('statusDetail');
  const box = document.getElementById('actionBox');
  const actText = document.getElementById('actionText');
  const d4Val = parseInt(document.getElementById('d4').value);
  let borderColor = '#d0d0d0';
  if (total > 60) borderColor = '#c62828';
  else if (total >= 40) borderColor = '#f9a825';
  else borderColor = '#2e7d32';
  circle.style.borderColor = borderColor;
  if (total > 60) {{
    badge.className = 'status-badge status-exit'; badge.textContent = '🚨 離場訊號';
    detail.textContent = '總分 > 60 分。維度2（高收益利差）與維度3（日圓波動）同時爆表，穿境資本正在瘋狂平倉，市場進入「無差別拋售」階段。此時不要接刀，最深的「實體資產法拍」尚未到來。';
    box.className = 'action-box action-exit';
    actText.innerHTML = '<strong style="font-size:17px; display:block; margin-bottom:8px;">🚨 離場行動</strong>將股票/加密貨幣部位降至 20% 以下，持有短期（1-3 個月）美國國庫券（T-bills）或美元現金。等待市場恐慌進一步釋放。';
  }} else if (total >= 40) {{
    badge.className = 'status-badge status-watch'; badge.textContent = '🟡 觀察期';
    detail.textContent = '總分 40–60 分。開始研究「目標實體基建名單」，確認現金流覆蓋倍數（DSCR）> 1.5 倍，為進場做準備。';
    box.className = 'action-box action-watch';
    actText.innerHTML = '<strong style="font-size:17px; display:block; margin-bottom:8px;">🟡 觀察期行動</strong>開始研究「目標實體基建名單」（特定資料中心 REITs、擁有長期電力合約的發電廠、已出租給 AWS/Microsoft 的機房開發商）。<br><br><strong>關鍵動作：</strong>確認目標公司的「現金流覆蓋倍數（DSCR）」是否大於 1.5 倍。若大於，代表即使經濟衰退，仍有現金還債，不會倒閉。';
  }} else {{
    if (d4Val >= 10) {{
      badge.className = 'status-badge status-enter'; badge.textContent = '🟢 進場訊號';
      detail.textContent = '總分 < 30 分，且維度 4（REITs 利差）> 3%。實體基建的租金回報率已遠遠超過借貸成本，這是 2026 年唯一具備「安全邊際」的資產類別。';
      box.className = 'action-box action-enter';
      actText.innerHTML = '<strong style="font-size:17px; display:block; margin-bottom:8px;">🟢 進場行動（反市場時刻）</strong>大膽進場承接「維度 4」中利差最大的 REITs，或購買擁有大量資料中心抵押品的「不良債券基金（Distressed Debt Fund）」。<br><br>歷史對照：這就像 1988 年 KKR 眼中的 RJR 菸草公司——股價雖跌，但香菸（算力）仍在每天生產現金流。只要確定租約（合約）是長期的，它就是「被錯殺的現金奶牛」。';
    }} else {{
      badge.className = 'status-badge status-watch'; badge.textContent = '🟡 預備觀察';
      detail.textContent = '總分雖低（< 30），但維度 4（REITs 利差）尚未達到 >3% 的強烈買進訊號。繼續等待實體資產被錯殺的時機。';
      box.className = 'action-box action-watch';
      actText.innerHTML = '<strong style="font-size:17px; display:block; margin-bottom:8px;">🟡 預備觀察</strong>總分雖低，但實體基建利差尚未達到 >3% 的強烈買進訊號。持續監控資料中心 REITs 報價與 10 年期公債利差，等待黃金坑出現。';
    }}
  }}
}}
</script>

</body>
</html>"""
    return html


def classify_status(total, d4_raw):
    """回傳 (status_key, status_text)。與 generate_html / JS 的門檻邏輯一致。"""
    if total > 60:
        return "exit", "離場訊號"
    if total >= 40:
        return "watch", "觀察期"
    if d4_raw >= 10:
        return "enter", "進場訊號"
    return "watch_pre", "預備觀察"


# ============ 歷史紀錄存儲 ============

HISTORY_DIR = "data"
HISTORY_CSV = os.path.join(HISTORY_DIR, "history.csv")
HISTORY_JSONL = os.path.join(HISTORY_DIR, "history.jsonl")

# CSV / JSONL 欄位順序（固定，方便 Excel / pandas / canon-collider 讀取）
HISTORY_FIELDS = [
    "date", "timestamp", "total", "status",
    "d1", "d2", "d3", "d4", "d5",
    "DGS10", "SOFR", "SOFR_JUMP", "HY_SPREAD", "USDJPY_VOL",
    "REIT_YIELD", "REIT_SPREAD", "CNY_VOL", "VIX",
    "live_count", "data_quality", "has_fallback", "provenance",
]


def build_history_row(raw, scores, total, status_text, prov, update_time):
    """把一次 run 攤平成一筆歷史紀錄（含 provenance / 資料品質，裸投檢查寫進時間序列）。"""
    reit_spread = None
    if raw.get("REIT_YIELD") is not None and raw.get("DGS10") is not None:
        reit_spread = round(raw["REIT_YIELD"] - raw["DGS10"], 4)

    # 資料品質：7 個可抓來源中有幾個 LIVE（PROXY/FALLBACK 不計 LIVE）
    src_keys = ["DGS10", "SOFR", "HY_SPREAD", "USDJPY_VOL", "REIT_YIELD", "CNY_VOL", "VIX"]
    live_count = sum(1 for k in src_keys if prov.get(k) == "live")
    has_fallback = any(prov.get(k) == "fallback" for k in src_keys)

    def r(x, d=4):
        return round(x, d) if isinstance(x, (int, float)) else None

    return {
        "date": update_time.split(" ")[0],   # YYYY-MM-DD（去重主鍵）
        "timestamp": update_time,
        "total": total,
        "status": status_text,
        "d1": scores["d1"], "d2": scores["d2"], "d3": scores["d3"],
        "d4": scores["d4"], "d5": scores["d5"],
        "DGS10": r(raw.get("DGS10")), "SOFR": r(raw.get("SOFR")),
        "SOFR_JUMP": r(raw.get("SOFR_JUMP")), "HY_SPREAD": r(raw.get("HY_SPREAD"), 1),
        "USDJPY_VOL": r(raw.get("USDJPY_VOL")), "REIT_YIELD": r(raw.get("REIT_YIELD")),
        "REIT_SPREAD": reit_spread, "CNY_VOL": r(raw.get("CNY_VOL")),
        "VIX": r(raw.get("VIX"), 2),
        "live_count": live_count,
        "data_quality": round(live_count / len(src_keys), 3),
        "has_fallback": has_fallback,
        # 完整 provenance 也存進去（含 override 標記），canon-collider 可據此篩掉不可信讀數
        "provenance": json.dumps(prov, ensure_ascii=False, sort_keys=True),
    }


def append_history(row):
    """append/upsert 一筆歷史紀錄。以 date 為主鍵去重（同日重跑會覆蓋當日，不會產生兩筆）。
    同時寫 CSV（Excel/pandas 用）與 JSONL（canon-collider / RAG 用）。"""
    os.makedirs(HISTORY_DIR, exist_ok=True)

    # ── CSV：讀既有 → 以 date upsert → 全寫回（排序）──
    rows = {}
    if os.path.exists(HISTORY_CSV):
        try:
            with open(HISTORY_CSV, "r", encoding="utf-8-sig", newline="") as f:
                for old in csv.DictReader(f):
                    rows[old.get("date")] = old
        except Exception as e:
            print(f"[WARN] 讀取既有 CSV 失敗，將重建：{e}")
    rows[row["date"]] = {k: row.get(k) for k in HISTORY_FIELDS}  # upsert

    ordered = [rows[d] for d in sorted(rows.keys())]
    with open(HISTORY_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        w.writeheader()
        w.writerows(ordered)

    # ── JSONL：同樣以 date upsert → 全寫回 ──
    jrows = {}
    if os.path.exists(HISTORY_JSONL):
        try:
            with open(HISTORY_JSONL, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        obj = json.loads(line)
                        jrows[obj.get("date")] = obj
        except Exception as e:
            print(f"[WARN] 讀取既有 JSONL 失敗，將重建：{e}")
    jrows[row["date"]] = row  # upsert（保留原生型別）
    with open(HISTORY_JSONL, "w", encoding="utf-8") as f:
        for d in sorted(jrows.keys()):
            f.write(json.dumps(jrows[d], ensure_ascii=False) + "\n")

    print(f"[HISTORY] 已寫入 {row['date']}（共 {len(ordered)} 筆）→ {HISTORY_CSV} / {HISTORY_JSONL}")
    return len(ordered)


def load_history(limit=60):
    """讀最近 limit 筆歷史（舊→新），供儀表板畫趨勢用。無檔回傳 []。"""
    if not os.path.exists(HISTORY_CSV):
        return []
    out = []
    try:
        with open(HISTORY_CSV, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                def fnum(k):
                    v = row.get(k)
                    try:
                        return float(v) if v not in (None, "", "None") else None
                    except (ValueError, TypeError):
                        return None
                out.append({
                    "date": row.get("date"),
                    "status": row.get("status"),
                    "total": fnum("total"),
                    "reit_spread": fnum("REIT_SPREAD"),
                    "hy": fnum("HY_SPREAD"),
                    "vix": fnum("VIX"),
                    "dgs10": fnum("DGS10"),
                    "usdjpy_vol": fnum("USDJPY_VOL"),
                    "cny_vol": fnum("CNY_VOL"),
                    "data_quality": fnum("data_quality"),
                    "has_fallback": str(row.get("has_fallback")).lower() == "true",
                })
    except Exception as e:
        print(f"[WARN] 讀取歷史失敗：{e}")
        return []
    return out[-limit:]


def sparkline_svg(points, width=260, height=44, color="#1e3a5f", zero_line=False):
    """純 Python 生成 inline SVG 折線（零外部依賴）。points=[(x_label, y_value)]。
    None 值斷線。zero_line=True 時畫 y=0 基準虛線（利差用）。"""
    vals = [p[1] for p in points if p[1] is not None]
    if len(vals) < 2:
        return '<div style="font-size:12px;color:#999;">歷史資料不足（需 ≥2 筆），明天起累積</div>'
    vmin, vmax = min(vals), max(vals)
    if zero_line:
        vmin, vmax = min(vmin, 0.0), max(vmax, 0.0)
    span = (vmax - vmin) or 1.0
    n = len(points)
    pad = 4

    def sx(i):
        return pad + (width - 2 * pad) * (i / (n - 1 if n > 1 else 1))

    def sy(v):
        return height - pad - (height - 2 * pad) * ((v - vmin) / span)

    # 折線（None 斷開成多段）
    segs, cur = [], []
    for i, (_, v) in enumerate(points):
        if v is None:
            if len(cur) >= 2:
                segs.append(cur)
            cur = []
        else:
            cur.append(f"{sx(i):.1f},{sy(v):.1f}")
    if len(cur) >= 2:
        segs.append(cur)
    polylines = "".join(
        f'<polyline points="{" ".join(s)}" fill="none" stroke="{color}" stroke-width="2"/>'
        for s in segs
    )
    zero = ""
    if zero_line and vmin <= 0 <= vmax:
        zy = sy(0.0)
        zero = (f'<line x1="{pad}" y1="{zy:.1f}" x2="{width-pad}" y2="{zy:.1f}" '
                f'stroke="#c62828" stroke-width="1" stroke-dasharray="3,3"/>')
    # 末點標記
    last_i = max(i for i, (_, v) in enumerate(points) if v is not None)
    last_v = points[last_i][1]
    dot = f'<circle cx="{sx(last_i):.1f}" cy="{sy(last_v):.1f}" r="3" fill="{color}"/>'
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
            f'preserveAspectRatio="none" style="display:block;">{zero}{polylines}{dot}</svg>')


def line_chart_svg(history, key, color="#1e3a5f", zero_line=False, height=180, fmt_val="{:.2f}"):
    """全寬折線圖（含 y 軸 min/max/末值標籤、x 軸首尾日期、fallback 點畫空心）。
    純伺服器端 SVG，零 JS 零外部依賴，file:// 離線可渲染。"""
    W, H = 900, height
    padL, padR, padT, padB = 56, 16, 16, 28
    pts = [(h["date"], h.get(key), h.get("has_fallback")) for h in history]
    vals = [v for _, v, _ in pts if v is not None]
    if len(vals) < 2:
        return ('<div style="font-size:13px;color:#999;padding:20px;text-align:center;">'
                '歷史資料不足（需 ≥2 筆），每天累積一筆，明後天起長出折線。</div>')
    vmin, vmax = min(vals), max(vals)
    if zero_line:
        vmin, vmax = min(vmin, 0.0), max(vmax, 0.0)
    span = (vmax - vmin) or 1.0
    vmin -= span * 0.08
    vmax += span * 0.08
    span = (vmax - vmin) or 1.0
    n = len(pts)

    def sx(i):
        return padL + (W - padL - padR) * (i / (n - 1 if n > 1 else 1))

    def sy(v):
        return H - padB - (H - padT - padB) * ((v - vmin) / span)

    # 折線（None 斷線）
    segs, cur = [], []
    for i, (_, v, _) in enumerate(pts):
        if v is None:
            if len(cur) >= 2:
                segs.append(cur)
            cur = []
        else:
            cur.append(f"{sx(i):.1f},{sy(v):.1f}")
    if len(cur) >= 2:
        segs.append(cur)
    polylines = "".join(
        f'<polyline points="{" ".join(s)}" fill="none" stroke="{color}" stroke-width="2"/>'
        for s in segs
    )
    # 資料點（fallback 空心紅）
    dots = ""
    for i, (_, v, fb) in enumerate(pts):
        if v is None:
            continue
        if fb:
            dots += f'<circle cx="{sx(i):.1f}" cy="{sy(v):.1f}" r="3.2" fill="#fff" stroke="#c62828" stroke-width="1.5"/>'
        else:
            dots += f'<circle cx="{sx(i):.1f}" cy="{sy(v):.1f}" r="2.4" fill="{color}"/>'
    # 軸線 + 標籤
    axis = (f'<line x1="{padL}" y1="{padT}" x2="{padL}" y2="{H-padB}" stroke="#ccc" stroke-width="1"/>'
            f'<line x1="{padL}" y1="{H-padB}" x2="{W-padR}" y2="{H-padB}" stroke="#ccc" stroke-width="1"/>')
    ylabels = ""
    for frac in (0.0, 0.5, 1.0):
        val = vmin + span * frac
        y = sy(val)
        ylabels += (f'<line x1="{padL-4}" y1="{y:.1f}" x2="{W-padR}" y2="{y:.1f}" stroke="#f0f0f0" stroke-width="1"/>'
                    f'<text x="{padL-8}" y="{y+4:.1f}" text-anchor="end" font-size="11" fill="#888">{fmt_val.format(val)}</text>')
    zero = ""
    if zero_line and vmin <= 0 <= vmax:
        zy = sy(0.0)
        zero = (f'<line x1="{padL}" y1="{zy:.1f}" x2="{W-padR}" y2="{zy:.1f}" '
                f'stroke="#c62828" stroke-width="1.2" stroke-dasharray="4,3"/>'
                f'<text x="{W-padR}" y="{zy-4:.1f}" text-anchor="end" font-size="10" fill="#c62828">0</text>')
    # x 軸首尾 + 末值
    xlabels = (f'<text x="{sx(0):.1f}" y="{H-8}" text-anchor="start" font-size="11" fill="#888">{pts[0][0]}</text>'
               f'<text x="{sx(n-1):.1f}" y="{H-8}" text-anchor="end" font-size="11" fill="#888">{pts[-1][0]}</text>')
    last_i = max(i for i, (_, v, _) in enumerate(pts) if v is not None)
    last_v = pts[last_i][1]
    last_lbl = (f'<text x="{sx(last_i)-6:.1f}" y="{sy(last_v)-8:.1f}" text-anchor="end" '
                f'font-size="12" font-weight="700" fill="{color}">{fmt_val.format(last_v)}</text>')
    return (f'<svg viewBox="0 0 {W} {H}" width="100%" style="display:block;">'
            f'{ylabels}{zero}{axis}{polylines}{dots}{xlabels}{last_lbl}</svg>')


def generate_history_viewer(history):
    """獨立離線歷史檢視器 history.html。歷史資料伺服器端直接渲染成 SVG 圖 + 表格。
    零 JS、零外部依賴、file:// 雙擊即開、離線可展示全部歷史。"""
    n = len(history)
    charts = ""
    chart_specs = [
        ("total", "壓力總分（0–100）", "#1e3a5f", False, "{:.0f}"),
        ("reit_spread", "REIT 利差 %（紅虛線=0；翻正並過 +3% = 進場閘門）", "#2e7d32", True, "{:.2f}"),
        ("hy", "高收益債利差 HY（bps）", "#c62828", False, "{:.0f}"),
        ("vix", "VIX 恐慌指數", "#f9a825", False, "{:.1f}"),
    ]
    for key, title, color, zl, ff in chart_specs:
        charts += (f'<div class="chart-card"><div class="chart-title">{title}</div>'
                   f'{line_chart_svg(history, key, color=color, zero_line=zl, fmt_val=ff)}</div>')

    # 資料表（最新在上）
    rows = ""
    for h in reversed(history):
        fb = h.get("has_fallback")
        tr_style = ' style="background:#fff5f5;color:#a33;"' if fb else ''
        flag = '<span style="font-size:11px;color:#c62828;font-weight:700;">⚠FALLBACK</span>' if fb else ''
        def c(v, f="{:.2f}"):
            return f.format(v) if isinstance(v, (int, float)) else "—"
        rows += (f'<tr{tr_style}>'
                 f'<td>{h.get("date","")}</td>'
                 f'<td style="text-align:right;">{c(h.get("total"),"{:.0f}")}</td>'
                 f'<td>{h.get("status","") or ""} {flag}</td>'
                 f'<td style="text-align:right;">{c(h.get("reit_spread"))}</td>'
                 f'<td style="text-align:right;">{c(h.get("hy"),"{:.0f}")}</td>'
                 f'<td style="text-align:right;">{c(h.get("vix"),"{:.1f}")}</td>'
                 f'<td style="text-align:right;">{c(h.get("dgs10"))}</td>'
                 f'<td style="text-align:right;">{c(h.get("data_quality"))}</td>'
                 f'</tr>')

    date_range = f'{history[0]["date"]} → {history[-1]["date"]}' if n else '—'
    n_fb = sum(1 for h in history if h.get("has_fallback"))
    fb_line = (f'<span style="color:#c62828;">其中 {n_fb} 筆含 FALLBACK（表中標紅，趨勢判讀請剔除）</span>'
               if n_fb else '<span style="color:#2e7d32;">全部 LIVE / PROXY，無 FALLBACK 污染</span>')
    gen_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    empty_note = ''
    if n < 2:
        empty_note = ('<div style="background:#fffde7;border:1px solid #f9a825;border-radius:8px;'
                      'padding:14px;margin-bottom:16px;font-size:14px;color:#5d4037;">'
                      f'目前歷史 {n} 筆——圖表需 ≥2 筆。每天自動累積一筆，明後天起這裡會長出完整折線。</div>')

    return f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI 流動性壓力指標 — 離線歷史檢視器</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:"Noto Sans TC","Microsoft JhengHei","PingFang TC",sans-serif;
    background:#fff; color:#1a1a1a; line-height:1.7; padding:24px; max-width:1000px; margin:0 auto; }}
  h1 {{ font-size:26px; color:#1e3a5f; margin-bottom:4px; }}
  .sub {{ font-size:14px; color:#666; margin-bottom:6px; }}
  .meta {{ font-size:13px; color:#888; margin-bottom:20px; }}
  .chart-card {{ border:1px solid #e0e0e0; border-radius:10px; padding:16px; margin-bottom:16px; background:#fafafa; }}
  .chart-title {{ font-size:15px; font-weight:700; color:#1e3a5f; margin-bottom:8px; }}
  h2 {{ font-size:18px; color:#1e3a5f; margin:28px 0 12px; padding-bottom:6px; border-bottom:2px solid #e8f0fe; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th,td {{ padding:7px 10px; border-bottom:1px solid #eee; }}
  th {{ background:#f5f7fa; color:#555; text-align:left; font-weight:700; position:sticky; top:0; }}
  th:not(:first-child):not(:nth-child(3)) {{ text-align:right; }}
  .footer {{ margin-top:28px; padding-top:14px; border-top:1px solid #e0e0e0; font-size:12px; color:#999; }}
  code {{ background:#f0f0f0; padding:1px 5px; border-radius:3px; }}
</style>
</head>
<body>
<h1>AI 流動性壓力指標 — 離線歷史檢視器</h1>
<p class="sub">此檔為獨立離線版：所有歷史資料已嵌入本檔，<strong>不需網路、不需伺服器，雙擊即可展示全部歷史</strong>。</p>
<p class="meta">生成時間：{gen_time}｜歷史 {n} 筆（{date_range}）｜{fb_line}</p>
{empty_note}
<h2>趨勢圖</h2>
{charts}
<h2>完整資料表（最新在上）</h2>
<table>
<tr><th>日期</th><th>總分</th><th>狀態</th><th>REIT利差%</th><th>HY(bps)</th><th>VIX</th><th>10Y%</th><th>資料品質</th></tr>
{rows}
</table>
<div class="footer">
  資料同步自 <code>data/history.csv</code>／<code>data/history.jsonl</code>。空心紅點 = 該日含 FALLBACK 預設值，不可當真數據。<br>
  離線版由 fetch_data.py 每次執行時重新生成並嵌入最新歷史。
</div>
</body>
</html>"""


def sync_outputs(sync_dirs, files):
    """把產出檔複製到每個同步資料夾（如 Google Drive Desktop 同步夾、本地備份夾）。
    Drive Desktop 會自動把該夾同步到雲端並保留離線快取——零 API、零 token。"""
    import shutil
    if not sync_dirs:
        return
    for d in sync_dirs:
        try:
            target = os.path.join(d, "data")
            os.makedirs(target, exist_ok=True)
            for f in files:
                if os.path.exists(f):
                    # data/ 下的檔複製到 target/；根目錄檔（index.html/history.html）複製到 d/
                    if f.startswith(HISTORY_DIR + os.sep) or f.startswith(HISTORY_DIR + "/"):
                        shutil.copy2(f, os.path.join(target, os.path.basename(f)))
                    else:
                        shutil.copy2(f, os.path.join(d, os.path.basename(f)))
            print(f"[SYNC] 已同步到：{d}")
        except Exception as e:
            print(f"[SYNC][ERROR] 同步到 {d} 失敗：{e}")


def main():
    print("=" * 60)
    print("2026 AI 流動性壓力測試指標 — 自動數據抓取")
    print("=" * 60)

    config = load_config()
    raw = {}
    PROVENANCE.clear()

    # ── 1. FRED 數據 ──
    print("\n[1/6] 抓取 FRED 數據...")
    raw["DGS10"], live_dgs10 = fetch_fred("DGS10", fallback=4.2)
    PROVENANCE["DGS10"] = "live" if live_dgs10 else "fallback"

    raw["SOFR"], live_sofr = fetch_fred("SOFR", fallback=5.33)
    PROVENANCE["SOFR"] = "live" if live_sofr else "fallback"

    # FIX-1: BAMLH0A0HYM2 單位為百分比，×100 轉 bps 才能跟 400/600 bps 閾值比
    hy_pct, live_hy = fetch_fred("BAMLH0A0HYM2", fallback=3.5)
    raw["HY_SPREAD"] = round(hy_pct * 100.0, 1) if hy_pct is not None else None  # → bps
    PROVENANCE["HY_SPREAD"] = "live" if live_hy else "fallback"

    # FIX-5: SOFR 跳升 = 最新 − 5 個交易日前（真實變化量，非水位差）
    sofr_hist = fetch_fred_series("SOFR", n=8)
    sofr_jump = None
    if len(sofr_hist) >= 6:
        sofr_jump = round(sofr_hist[0] - sofr_hist[5], 3)  # 序列為新→舊
        print(f"    SOFR 跳升(最新−5交易日前) = {sofr_jump:+.3f}%")
    raw["SOFR_JUMP"] = sofr_jump

    # ── 2. USD/JPY 波動率 ──
    print("[2/6] 抓取 USD/JPY 30日已實現波動率(年化)...")
    v = calc_volatility("USDJPY=X")
    if v is None:
        raw["USDJPY_VOL"] = 12.0
        PROVENANCE["USDJPY_VOL"] = "fallback"
    else:
        raw["USDJPY_VOL"] = v
        PROVENANCE["USDJPY_VOL"] = "live"

    # ── 3. 資料中心 REITs 殖利率 ──
    # FIX-2: 移除 COR(已成藥品通路商 Cencora)，改用純資料中心/機房 REIT
    print("[3/6] 抓取資料中心 REITs 殖利率...")
    reit_tickers = ["DLR", "EQIX", "IRM"]  # Digital Realty / Equinix / Iron Mountain
    reit_yields = []
    for tk in reit_tickers:
        y = fetch_reit_yield(tk)
        if y is not None:
            reit_yields.append(y)
            print(f"    {tk}: {y:.2f}%")
        else:
            print(f"    {tk}: 抓取失敗")
    if reit_yields:
        raw["REIT_YIELD"] = sum(reit_yields) / len(reit_yields)
        # 全數抓到才算 live；部分抓到標 partial(視為 live 但記錄)
        PROVENANCE["REIT_YIELD"] = "live" if len(reit_yields) == len(reit_tickers) else "partial"
    else:
        raw["REIT_YIELD"] = 5.5
        PROVENANCE["REIT_YIELD"] = "fallback"
        print("    使用預設值: 5.5%")
    raw["REIT_TICKERS"] = ",".join(reit_tickers)

    # ── 4. 維度5 替代指標：CNH(離岸人民幣)波動率 ──
    # FIX: 原碼用 CNY=X(在岸)，但文件寫「離岸」，改用 CNH=X 對齊
    print("[4/6] 抓取 CNH 離岸人民幣30日波動率 (維度5替代指標)...")
    cv = calc_volatility("CNH=X")
    if cv is None:
        cv = calc_volatility("CNY=X")  # 離岸抓不到退回在岸
    if cv is None:
        raw["CNY_VOL"] = 4.0
        PROVENANCE["CNY_VOL"] = "fallback"
    else:
        raw["CNY_VOL"] = cv
        PROVENANCE["CNY_VOL"] = "proxy"  # 本質是替代指標，不是真 SGE/LBMA 價差

    # ── 5. VIX（ADD-1：context，不計分）──
    print("[5/6] 抓取 ^VIX (決策樹 context, 不計分)...")
    vix, live_vix = fetch_vix()
    raw["VIX"] = vix
    PROVENANCE["VIX"] = "live" if live_vix else "fallback"
    if vix is not None:
        print(f"    VIX = {vix:.2f}")

    # ── 6. 計算分數（模型層閾值/權重 100% 保留 DeepSeek 原始計分卡）──
    print("[6/6] 計算五維度分數...")
    ov = {f"dim{i}_override": config.get(f"dim{i}_override") for i in range(1, 6)}
    for i in range(1, 6):
        if ov[f"dim{i}_override"] is not None:
            PROVENANCE[f"d{i}_override"] = True

    scores = {
        "d1": score_dim1(raw["DGS10"], raw["SOFR"], sofr_jump, ov["dim1_override"]),
        "d2": score_dim2(raw["HY_SPREAD"], ov["dim2_override"]),
        "d3": score_dim3(raw["USDJPY_VOL"], ov["dim3_override"]),
        "d4": score_dim4(raw["REIT_YIELD"], raw["DGS10"], ov["dim4_override"]),
        "d5": score_dim5(raw["CNY_VOL"], ov["dim5_override"]),
    }
    total = weighted_total(scores)
    status_key, status_text = classify_status(total, scores["d4"])

    # ── 寫入歷史紀錄（append/upsert by date）──
    update_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    hist_row = build_history_row(raw, scores, total, status_text, dict(PROVENANCE), update_time)
    n_hist = append_history(hist_row)
    history = load_history(limit=60)

    # ── 生成 HTML（帶趨勢）──
    html = generate_html(raw, scores, total, update_time, dict(PROVENANCE), history)

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    # ── 生成獨立離線歷史檢視器 history.html（歷史嵌入，file:// 可離線展示）──
    viewer = generate_history_viewer(history)
    with open("history.html", "w", encoding="utf-8") as f:
        f.write(viewer)

    # ── 同步到 Google Drive Desktop 同步夾 / 本地備份夾（config.json 的 sync_dirs）──
    sync_dirs = config.get("sync_dirs") or []
    sync_files = [HISTORY_CSV, HISTORY_JSONL, "index.html", "history.html"]
    sync_outputs(sync_dirs, sync_files)

    print("\n" + "=" * 60)
    print(f"✅ 完成！更新時間: {update_time}")
    print(f"📊 壓力總分: {total}/100  狀態: {status_text}")
    print(f"🗂️  歷史紀錄: {n_hist} 筆 → {HISTORY_CSV} / {HISTORY_JSONL}")
    print(f"📁 輸出檔案: index.html（即時儀表板）+ history.html（離線歷史檢視器）")
    if sync_dirs:
        print(f"🔄 已同步到 {len(sync_dirs)} 個資料夾: {', '.join(sync_dirs)}")
    print("=" * 60)
    print("\n原始數據:")
    for k, v in raw.items():
        print(f"  {k}: {v}")
    print("\n維度分數:")
    for k, v in scores.items():
        print(f"  {k}: {v}")
    print("\n資料來源健康檢查(provenance):")
    for k, v in PROVENANCE.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
