import json, math, os, time
from datetime import datetime, timezone
from pathlib import Path
import requests

ROOT=Path(__file__).resolve().parent
CFG=json.loads((ROOT/"config.json").read_text())
STATE=ROOT/"state"/"latest.json"
STATE.parent.mkdir(exist_ok=True)
S=requests.Session()
S.headers.update({"User-Agent":"crypto-opportunity-scanner/1.0"})

def clamp(x,a=0,b=100): return max(a,min(b,x))
def cg(path,params=None):
    base=os.getenv("COINGECKO_BASE","https://api.coingecko.com/api/v3")
    headers={}
    key=os.getenv("COINGECKO_API_KEY","").strip()
    if key: headers["x-cg-demo-api-key"]=key
    last=None
    for attempt in range(5):
        r=S.get(base+path,params=params,headers=headers,timeout=30)
        last=r
        if r.status_code != 429:
            r.raise_for_status()
            time.sleep(6.5)
            return r.json()
        wait=int(r.headers.get("Retry-After") or min(15*(attempt+1),60))
        print(f"CoinGecko rate limit; waiting {wait}s (attempt {attempt+1}/5)")
        time.sleep(wait)
    last.raise_for_status()

def markets():
    out=[]
    for page in range(1,9):
        data=cg("/coins/markets",{"vs_currency":"usd","order":"market_cap_desc","per_page":250,
          "page":page,"sparkline":"false","price_change_percentage":"24h,7d"})
        if not data: break
        out.extend(data)
        if min([(x.get("market_cap") or 10**18) for x in data]) < CFG["min_market_cap"]: break
    return out

def hist_max_x(coin_id):
    try:
        d=cg(f"/coins/{coin_id}/market_chart",{"vs_currency":"usd","days":"max","interval":"daily"})
        prices=[p[1] for p in d.get("prices",[]) if p[1] and p[1]>0]
        if len(prices)<14:return None
        # Corrected methodology proxy: meaningful post-launch dip then highest subsequent daily peak.
        # 3-day median-like smoothing removes isolated wick lows/highs.
        sm=[]
        for i in range(1,len(prices)-1):
            sm.append(sorted(prices[i-1:i+2])[1])
        best=1.0
        running_low=sm[0]
        for p in sm[1:]:
            running_low=min(running_low,p)
            if running_low>0: best=max(best,p/running_low)
        return round(best,2)
    except Exception:return None

def score_market(c,max_x=None):
    mc=c.get("market_cap") or 0; fdv=c.get("fully_diluted_valuation") or 0
    vol=c.get("total_volume") or 0
    ratio=(fdv/mc) if mc and fdv else None
    # Valuation/asymmetry: low MC rewarded, excessive FDV/MC and past Max-X penalized.
    if mc<=5e6:v=96
    elif mc<=15e6:v=92
    elif mc<=30e6:v=88
    elif mc<=60e6:v=83
    elif mc<=100e6:v=78
    else:v=72
    if ratio:
        if ratio>12:v-=22
        elif ratio>8:v-=16
        elif ratio>5:v-=10
        elif ratio>3:v-=5
    if max_x:
        if max_x>100:v-=35
        elif max_x>20:v-=25
        elif max_x>10:v-=15
        elif max_x>5:v-=7
        elif max_x<3:v+=3
    v=clamp(v)

    # Liquidity/distribution proxy from volume/MC. Distribution itself requires qualitative verification.
    vm=(vol/mc) if mc else 0
    liq=clamp(45 + min(45, math.log10(max(vm,1e-5)*1000+1)*20))
    if vol<CFG["min_volume_24h"]: liq=min(liq,45)

    # Automated qualitative categories are deliberately conservative placeholders.
    # They are NOT presented as verified fundamentals.
    narrative=60; tech=60; adoption=55; tokenomics=55; utility=55; reg=60
    # Market evidence nudges adoption, but cannot substitute fundamental verification.
    if vm>.20: adoption+=8
    elif vm>.08: adoption+=4
    if ratio and ratio<=2: tokenomics+=8
    elif ratio and ratio>5: tokenomics-=12

    w=CFG["weights"]
    final=(v*w["valuation_asymmetry"]+narrative*w["narrative_catalyst"]+
      tech*w["technology_moat"]+adoption*w["adoption"]+tokenomics*w["tokenomics"]+
      utility*w["utility_value_capture"]+reg*w["regulation_survival"]+
      liq*w["liquidity_distribution"])
    return {"valuation_asymmetry":round(v,1),"narrative_catalyst":narrative,
      "technology_moat":tech,"adoption":adoption,"tokenomics":tokenomics,
      "utility_value_capture":utility,"regulation_survival":reg,
      "liquidity_distribution":round(liq,1),"preliminary_score":round(final,1),
      "fdv_mc":round(ratio,2) if ratio else None}

def telegram_chat_id(token):
    chat=os.getenv("TELEGRAM_CHAT_ID","").strip()
    if chat: return chat
    # If CHAT_ID is not configured, discover the latest private chat that messaged the bot.
    # Send /start to the bot once before the first workflow run.
    try:
        r=S.get(f"https://api.telegram.org/bot{token}/getUpdates",timeout=20)
        r.raise_for_status()
        updates=r.json().get("result",[])
        for u in reversed(updates):
            m=u.get("message") or u.get("edited_message") or {}
            ch=m.get("chat") or {}
            if ch.get("id") and ch.get("type")=="private":
                return str(ch["id"])
    except Exception:
        pass
    return ""

def send_telegram(msg):
    token=os.getenv("TELEGRAM_BOT_TOKEN","").strip()
    if not token:
        print("Telegram token not configured; alert printed only.")
        print(msg); return False
    chat=telegram_chat_id(token)
    if not chat:
        print("Telegram chat not discovered. Send /start to the bot once.")
        print(msg); return False
    r=S.post(f"https://api.telegram.org/bot{token}/sendMessage",
      json={"chat_id":chat,"text":msg,"disable_web_page_preview":True},timeout=20)
    r.raise_for_status(); return True

def main():
    excluded={x.strip().upper() for x in CFG["master_table_symbols"].split(",")}
    raw=markets()
    cand=[c for c in raw if c.get("market_cap") and
      CFG["min_market_cap"]<=c["market_cap"]<=CFG["max_market_cap"] and
      (c.get("symbol") or "").upper() not in excluded and
      (c.get("total_volume") or 0)>=CFG["min_volume_24h"]]
    # First pass market-only; fetch history only for best asymmetry/liquidity candidates.
    prelim=[]
    for c in cand:
        s=score_market(c)
        prelim.append((s["preliminary_score"],c,s))
    prelim.sort(reverse=True,key=lambda x:x[0])
    rows=[]
    for _,c,_ in prelim[:12]:
        mx=hist_max_x(c["id"])
        s=score_market(c,mx)
        rows.append({"id":c["id"],"symbol":c["symbol"].upper(),"name":c["name"],
          "price":c.get("current_price"),"market_cap":c.get("market_cap"),
          "fdv":c.get("fully_diluted_valuation"),"volume_24h":c.get("total_volume"),
          "change_24h":c.get("price_change_percentage_24h"),
          "max_x_proxy":mx,**s})
    rows.sort(key=lambda x:x["preliminary_score"],reverse=True)
    old={}
    if STATE.exists():
        try: old={x["id"]:x for x in json.loads(STATE.read_text()).get("rows",[])}
        except Exception: pass
    alerts=[]
    # Important: score >=85 should normally require qualitative verification; placeholders make that rare.
    for x in rows[:15]:
        prev=old.get(x["id"])
        if x["preliminary_score"]>=CFG["alert_score"]:
            alerts.append(("NEW HIGH SCORE",x))
        elif prev and x["preliminary_score"]-prev.get("preliminary_score",0)>=CFG["score_change_alert"]:
            alerts.append(("SCORE UP",x))
        elif prev and prev.get("market_cap") and abs(x["market_cap"]/prev["market_cap"]-1)*100>=CFG["market_cap_change_alert_pct"]:
            alerts.append(("MC MOVE",x))
    payload={"updated_at":datetime.now(timezone.utc).isoformat(),"rows":rows[:35]}
    STATE.write_text(json.dumps(payload,ensure_ascii=False,indent=2))
    top=rows[:10]
    print(json.dumps(payload,ensure_ascii=False,indent=2))
    if alerts:
        lines=["🔎 Crypto Opportunity Scanner — anlamlı değişiklik"]
        for kind,x in alerts[:8]:
            lines.append(f"{kind}: {x['symbol']} | ön skor {x['preliminary_score']} | MC ${x['market_cap']/1e6:.2f}M | MaxX~{x['max_x_proxy']}")
        lines.append("Not: Otomatik skor ön-elemedir; nitel başlıklar doğrulanmadan nihai puan değildir.")
        send_telegram("\n".join(lines))

if __name__=="__main__": main()
