import json, math, os, re, time
from datetime import datetime, timezone
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text())
STATE = ROOT / "state" / "latest.json"
STATE.parent.mkdir(exist_ok=True)
S = requests.Session()
S.headers.update({"User-Agent": "crypto-opportunity-scanner/2.0"})

TOKENIZED_WORDS = (
    "tokenized stock", "tokenized equity", "robinhood token", "ondo tokenized",
    "bstocks tokenized", "xstock", "tokenized etf", "tokenized shares"
)
MEME_NAME_RE = re.compile(r"\b(meme|memecoin|pepe|wojak|raccoon|dogwifhat|floki|bonk|shiba)\b", re.I)
MEME_CATEGORY_WORDS = ("meme", "memecoin", "dog-themed", "cat-themed", "animal-themed")
NARRATIVE_TERMS = (
    "artificial intelligence", "ai agent", "depin", "real world asset", "rwa",
    "zero knowledge", "zk", "modular", "interoperability", "data availability",
    "restaking", "compute", "storage", "oracle", "identity", "privacy",
    "decentralized physical infrastructure"
)
TECH_TERMS = (
    "zero knowledge", "zk", "layer 1", "layer 2", "modular", "interoperability",
    "data availability", "privacy", "depin", "oracle", "compute", "storage"
)
UTILITY_TERMS = (
    "fee", "fees", "staking", "gas", "governance", "collateral", "revenue",
    "burn", "payment", "compute", "storage", "oracle", "data availability",
    "restaking", "settlement", "security", "identity", "liquidity"
)

def clamp(x, a=0, b=100):
    return max(a, min(b, x))

def cg(path, params=None):
    base = os.getenv("COINGECKO_BASE", "https://api.coingecko.com/api/v3")
    key = os.getenv("COINGECKO_API_KEY", "").strip()
    headers = {"x-cg-demo-api-key": key} if key else {}
    last = None
    for attempt in range(5):
        r = S.get(base + path, params=params, headers=headers, timeout=30)
        last = r
        if r.status_code != 429:
            r.raise_for_status()
            time.sleep(0.8 if key else 12.0)
            return r.json()
        retry = r.headers.get("Retry-After")
        try:
            retry = int(retry) if retry else 0
        except ValueError:
            retry = 0
        wait = max(15, retry, min(15 * (attempt + 1), 60))
        print(f"CoinGecko rate limit; waiting {wait}s (attempt {attempt+1}/5)")
        time.sleep(wait)
    last.raise_for_status()

def market_params():
    return {
        "vs_currency": "usd", "order": "market_cap_desc", "per_page": 250,
        "sparkline": "false", "price_change_percentage": "24h,7d,30d,1y"
    }

def markets():
    hour = datetime.now(timezone.utc).hour
    pages = [2, 3, 4, 5, 6 + (hour % 7)]
    out, seen = [], set()
    for page in pages:
        p = market_params(); p["page"] = page
        for c in cg("/coins/markets", p):
            if c.get("id") not in seen:
                out.append(c); seen.add(c.get("id"))
    watch = [x for x in CFG.get("manual_watchlist_ids", []) if x]
    if watch:
        p = market_params(); p["ids"] = ",".join(watch)
        for c in cg("/coins/markets", p):
            if c.get("id") not in seen:
                out.append(c); seen.add(c.get("id"))
    return out, pages

def obvious_noise(c):
    name = (c.get("name") or "").lower()
    cid = (c.get("id") or "").lower()
    blob = f"{name} {cid}"
    if any(w in blob for w in TOKENIZED_WORDS): return "tokenized-stock"
    if MEME_NAME_RE.search(blob): return "obvious-meme"
    return None

def coin_detail(coin_id):
    try:
        return cg(f"/coins/{coin_id}", {
            "localization": "false", "tickers": "false", "market_data": "true",
            "community_data": "true", "developer_data": "true", "sparkline": "false"
        })
    except Exception as e:
        print(f"detail failed for {coin_id}: {e}"); return {}

def detail_noise(detail):
    cats = [str(x).lower() for x in detail.get("categories", [])]
    blob = " | ".join(cats)
    if any(w in blob for w in MEME_CATEGORY_WORDS): return "meme-category"
    if "tokenized stock" in blob or "tokenized equity" in blob: return "tokenized-stock-category"
    desc = ((detail.get("description") or {}).get("en") or "").lower()
    name = (detail.get("name") or "").lower()
    if any(w in f"{name} {desc[:800]}" for w in TOKENIZED_WORDS): return "tokenized-stock-description"
    return None

def dt(s):
    if not s: return None
    try: return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        try: return datetime.strptime(str(s)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception: return None

def age_info(detail, coin_id):
    g = dt(detail.get("genesis_date"))
    manual = coin_id in set(CFG.get("manual_watchlist_ids", []))
    if not g: return None, "manual-watchlist" if manual else "unknown"
    return (datetime.now(timezone.utc) - g).days, "verified-genesis"

def hist_max_x(coin_id, market_row=None):
    best, method, confidence = None, None, "medium"
    c = market_row or {}
    atl, ath = c.get("atl"), c.get("ath")
    atl_d, ath_d = dt(c.get("atl_date")), dt(c.get("ath_date"))
    if atl and ath and atl > 0 and atl_d and ath_d and atl_d < ath_d:
        best, method, confidence = ath / atl, "atl-before-ath", "low"
    try:
        d = cg(f"/coins/{coin_id}/market_chart", {"vs_currency":"usd","days":"365","interval":"daily"})
        prices = [p[1] for p in d.get("prices", []) if p[1] and p[1] > 0]
        if len(prices) >= 14:
            sm = [sorted(prices[i-1:i+2])[1] for i in range(1, len(prices)-1)]
            run_low, local_best = sm[0], 1.0
            for p in sm[1:]:
                run_low = min(run_low, p); local_best = max(local_best, p / run_low)
            if best is None or (local_best > best and local_best < best * 3):
                best, method, confidence = local_best, "1y-smoothed-running-low", "medium"
    except Exception as e:
        print(f"history failed for {coin_id}: {e}")
    return (round(best, 2) if best else None, method, confidence)

def valuation_score(c, max_x=None):
    mc = c.get("market_cap") or 0
    fdv = c.get("fully_diluted_valuation") or 0
    ratio = (fdv / mc) if mc and fdv else None
    if mc <= 5e6: v = 96
    elif mc <= 15e6: v = 92
    elif mc <= 30e6: v = 88
    elif mc <= 60e6: v = 84
    elif mc <= 100e6: v = 80
    else: v = 75
    if ratio:
        if ratio > 12: v -= 24
        elif ratio > 8: v -= 18
        elif ratio > 5: v -= 12
        elif ratio > 3: v -= 6
    if max_x:
        if max_x > 100: v -= 38
        elif max_x > 20: v -= 28
        elif max_x > 10: v -= 18
        elif max_x > 5: v -= 9
        elif max_x < 3: v += 3
    return clamp(v), ratio

def preliminary_market_score(c):
    v, ratio = valuation_score(c)
    mc, vol = c.get("market_cap") or 1, c.get("total_volume") or 0
    vm = vol / mc
    liq = clamp(35 + min(55, math.log10(max(vm, 1e-5) * 1000 + 1) * 22))
    circ, total = c.get("circulating_supply"), c.get("total_supply")
    tok = 50
    if circ and total and total > 0:
        cr = circ / total
        if cr >= .75: tok += 20
        elif cr >= .50: tok += 10
        elif cr < .20: tok -= 25
        elif cr < .35: tok -= 12
    if ratio:
        if ratio <= 1.5: tok += 12
        elif ratio <= 2.5: tok += 6
        elif ratio > 8: tok -= 22
        elif ratio > 5: tok -= 14
    return round(.62 * v + .23 * clamp(tok) + .15 * liq, 1)

def count_terms(text, terms):
    t = text.lower()
    return sum(1 for x in terms if x in t)

def factor_scores(c, detail, max_x=None):
    v, ratio = valuation_score(c, max_x)
    cats = " | ".join(str(x) for x in detail.get("categories", []))
    desc = ((detail.get("description") or {}).get("en") or "")
    links, dev, md = detail.get("links") or {}, detail.get("developer_data") or {}, detail.get("market_data") or {}
    coin_id = c.get("id") or ""
    age_days, age_source = age_info(detail, coin_id)

    narrative = 48 + min(30, 6 * count_terms(cats, NARRATIVE_TERMS))
    if age_days is not None:
        if age_days <= 365: narrative += 8
        elif age_days <= CFG.get("max_age_days", 730): narrative += 5
        else: narrative -= 12
    if coin_id in set(CFG.get("manual_watchlist_ids", [])): narrative += 5
    narrative = clamp(narrative, 25, 88)

    repos = ((links.get("repos_url") or {}).get("github") or [])
    commits, stars = dev.get("commit_count_4_weeks") or 0, dev.get("stars") or 0
    tech = 42 + (12 if repos else 0)
    tech += 18 if commits >= 25 else 12 if commits >= 8 else 6 if commits > 0 else 0
    tech += 8 if stars >= 1000 else 4 if stars >= 100 else 0
    tech += min(10, 3 * count_terms(cats, TECH_TERMS))
    tech = clamp(tech, 25, 92)

    mc, vol = c.get("market_cap") or 1, c.get("total_volume") or 0
    vm, watchers = vol / mc, detail.get("watchlist_portfolio_users") or 0
    adoption = 38
    adoption += 22 if vm >= .30 else 16 if vm >= .12 else 10 if vm >= .05 else 5 if vm >= .02 else 0
    adoption += 20 if watchers >= 100000 else 14 if watchers >= 25000 else 8 if watchers >= 5000 else 4 if watchers >= 1000 else 0
    tvl = md.get("total_value_locked") or 0
    if tvl and mc: adoption += 12 if tvl / mc >= .5 else 6 if tvl / mc >= .1 else 0
    adoption = clamp(adoption, 25, 92)

    circ = c.get("circulating_supply") or md.get("circulating_supply")
    total = c.get("total_supply") or md.get("total_supply")
    max_supply = c.get("max_supply") or md.get("max_supply")
    tokenomics, circ_ratio = 48, None
    denom = total or max_supply
    if circ and denom and denom > 0:
        circ_ratio = circ / denom
        tokenomics += 22 if circ_ratio >= .80 else 15 if circ_ratio >= .60 else 7 if circ_ratio >= .40 else -25 if circ_ratio < .20 else -15 if circ_ratio < .30 else 0
    if ratio:
        tokenomics += 16 if ratio <= 1.4 else 10 if ratio <= 2.0 else 4 if ratio <= 3.0 else -25 if ratio > 8 else -16 if ratio > 5 else 0
    if md.get("max_supply_infinite") is True: tokenomics -= 10
    tokenomics = clamp(tokenomics, 15, 95)

    utility = clamp(38 + min(42, count_terms(desc[:5000] + " " + cats, UTILITY_TERMS) * 6), 25, 88)

    regulation = 62
    lowcats = cats.lower()
    if "privacy" in lowcats: regulation -= 10
    if "gambling" in lowcats: regulation -= 18
    if "meme" in lowcats: regulation -= 25
    if any(x in lowcats for x in ("infrastructure", "oracle", "identity", "storage", "depin")): regulation += 6
    if age_days is not None and age_days > 365: regulation += 3
    regulation = clamp(regulation, 20, 82)

    liq = clamp(35 + min(55, math.log10(max(vm, 1e-5) * 1000 + 1) * 22))
    if vol < CFG["min_volume_24h"]: liq = min(liq, 40)

    scores = {
        "valuation_asymmetry": round(v,1), "narrative_catalyst": round(narrative,1),
        "technology_moat": round(tech,1), "adoption": round(adoption,1),
        "tokenomics": round(tokenomics,1), "utility_value_capture": round(utility,1),
        "regulation_survival": round(regulation,1), "liquidity_distribution": round(liq,1)
    }
    w = CFG["weights"]
    total_score = sum(scores[k] * w[k] for k in w)
    return scores, round(total_score,1), ratio, circ_ratio, age_days, age_source

def telegram_chat_id(token):
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if chat: return chat
    try:
        r = S.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20); r.raise_for_status()
        for u in reversed(r.json().get("result", [])):
            m = u.get("message") or u.get("edited_message") or {}
            ch = m.get("chat") or {}
            if ch.get("id") and ch.get("type") == "private": return str(ch["id"])
    except Exception: pass
    return ""

def send_telegram(msg):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("Telegram token not configured; alert printed only."); print(msg); return False
    chat = telegram_chat_id(token)
    if not chat:
        print("Telegram chat not discovered. Send /start to the bot once."); print(msg); return False
    r = S.post(f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id":chat,"text":msg,"disable_web_page_preview":True}, timeout=20)
    r.raise_for_status(); return True

def main():
    excluded = {x.strip().upper() for x in CFG["master_table_symbols"].split(",")}
    raw, pages = markets()
    rejected = {"master":0,"market":0,"noise":0,"detail_noise":0}
    cand = []
    for c in raw:
        mc, sym = c.get("market_cap") or 0, (c.get("symbol") or "").upper()
        if sym in excluded: rejected["master"] += 1; continue
        if not (CFG["min_market_cap"] <= mc <= CFG["max_market_cap"]) or (c.get("total_volume") or 0) < CFG["min_volume_24h"]:
            rejected["market"] += 1; continue
        if obvious_noise(c): rejected["noise"] += 1; continue
        cand.append(c)

    ranked = sorted(cand, key=preliminary_market_score, reverse=True)
    accepted, detail_calls = [], 0
    for c in ranked:
        if detail_calls >= CFG.get("max_detail_calls_per_run", 5): break
        detail = coin_detail(c["id"]); detail_calls += 1
        if not detail: continue
        reason = detail_noise(detail)
        if reason:
            rejected["detail_noise"] += 1; print(f"Rejected {c['id']}: {reason}"); continue
        accepted.append((c, detail))
        if len(accepted) >= CFG.get("max_scored_candidates", 3): break

    rows = []
    history_budget = CFG.get("max_history_calls_per_run", 2)
    for idx, (c, detail) in enumerate(accepted):
        if idx < history_budget: mx, mx_method, mx_conf = hist_max_x(c["id"], c)
        else: mx, mx_method, mx_conf = None, None, "not-fetched"
        scores, total, fdv_mc, circ_ratio, age_days, age_source = factor_scores(c, detail, mx)
        age_ok = None if age_days is None else age_days <= CFG.get("max_age_days", 730)
        quality_gate = total >= CFG["watch_score"] and (mx is None or mx <= CFG.get("heavy_penalty_max_x",20.0)) and age_ok is not False
        rows.append({
            "id":c["id"],"symbol":(c.get("symbol") or "").upper(),"name":c.get("name"),
            "price":c.get("current_price"),"market_cap":c.get("market_cap"),
            "fdv":c.get("fully_diluted_valuation"),"volume_24h":c.get("total_volume"),
            "change_24h":c.get("price_change_percentage_24h"),
            "change_7d":c.get("price_change_percentage_7d_in_currency"),
            "change_30d":c.get("price_change_percentage_30d_in_currency"),
            "max_x_proxy":mx,"max_x_method":mx_method,"max_x_confidence":mx_conf,
            "age_days":age_days,"age_source":age_source,"age_ok_2y":age_ok,
            "fdv_mc":round(fdv_mc,2) if fdv_mc else None,
            "circulating_ratio":round(circ_ratio,3) if circ_ratio is not None else None,
            "categories":(detail.get("categories") or [])[:8],
            "score_type":"automated_8factor_prescore","score":total,"quality_gate":quality_gate, **scores
        })
    rows.sort(key=lambda x:x["score"], reverse=True)

    old = {}
    if STATE.exists():
        try: old = {x["id"]:x for x in json.loads(STATE.read_text()).get("rows",[])}
        except Exception: pass
    alerts = []
    for x in rows:
        prev = old.get(x["id"])
        oldscore = prev.get("score", prev.get("preliminary_score",0)) if prev else 0
        if x["score"] >= CFG["alert_score"] and x["quality_gate"]: alerts.append(("85+ ADAY",x))
        elif prev and x["score"] - oldscore >= CFG["score_change_alert"]: alerts.append(("SKOR ARTTI",x))
        elif prev and prev.get("market_cap") and abs(x["market_cap"]/prev["market_cap"]-1)*100 >= CFG["market_cap_change_alert_pct"]: alerts.append(("MC HAREKETİ",x))

    payload = {
        "updated_at":datetime.now(timezone.utc).isoformat(),"scanner_version":"2.0",
        "pages_scanned":pages,"api_budget_design":"5 market + 1 watchlist + <=5 detail + <=2 history calls/run",
        "raw_assets":len(raw),"eligible_after_basic_filters":len(cand),"rejected":rejected,"rows":rows
    }
    STATE.write_text(json.dumps(payload,ensure_ascii=False,indent=2))
    print(json.dumps(payload,ensure_ascii=False,indent=2))
    if alerts:
        lines = ["🔎 Crypto Opportunity Scanner — yeni sinyal"]
        for kind,x in alerts[:6]:
            age = f"{x['age_days']}g" if x["age_days"] is not None else "?"
            mx = x["max_x_proxy"] if x["max_x_proxy"] is not None else "?"
            lines.append(f"{kind}: {x['symbol']} | 8F ön skor {x['score']} | MC {x['market_cap']/1e6:.2f}M USD | MaxX~{mx} | yaş {age}")
        lines.append("Filtre: Master Table + tokenized stock + meme kategorileri dışarıda.")
        lines.append("Not: Narrative/katalizör ve regülasyon başlıkları veri-proxy'sidir; nihai puan için doğrulama gerekir.")
        send_telegram("\n".join(lines))

if __name__ == "__main__":
    main()
