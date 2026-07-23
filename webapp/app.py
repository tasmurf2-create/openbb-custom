"""
Personal OpenBB frontend — a FastAPI "backend-for-frontend" that talks to your
self-hosted OpenBB Platform API and serves your own tweakable GUI.

Built on FastAPI to match OpenBB's own stack (OpenBB's API is FastAPI too).

    Browser  ->  this FastAPI app (/api/*)  ->  OpenBB Platform API  ->  data feeds

The browser never talks to OpenBB directly — this app proxies each call server
side, so there's no CORS hassle and the OpenBB API location stays configurable.

Local:   OPENBB_API_URL defaults to http://localhost:6900 (the API you already run)
Render:  set OPENBB_API_URL to your deployed OpenBB API service URL

Run:  uvicorn app:app --port 5000     (or just `python app.py`)
"""

import email.utils
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta

import requests
from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse

try:  # load webapp/.env so secrets (e.g. ANTHROPIC_API_KEY) stay out of the repo
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

app = FastAPI(title="My OpenBB Dashboard")

# Optional keys (read from webapp/.env or the environment; never hardcoded)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")  # for AI-written advice
FMP_CONFIGURED = None  # FMP key lives in OpenBB's user_settings.json, not here

HERE = os.path.dirname(os.path.abspath(__file__))

# Where the OpenBB Platform API lives. Override in production via env var.
OPENBB_API_URL = os.environ.get("OPENBB_API_URL", "http://localhost:6900").rstrip("/")
# Default data provider. yfinance is free and needs no API key.
PROVIDER = os.environ.get("OPENBB_PROVIDER", "yfinance")
# How long to wait on the OpenBB API before giving up.
TIMEOUT = int(os.environ.get("OPENBB_TIMEOUT", "30"))

# Map a friendly range label -> how far back to start, and a sensible interval.
RANGE_TO_DAYS = {"1mo": 30, "3mo": 90, "6mo": 180, "1y": 365, "5y": 365 * 5}


def obb_get(path: str, params: dict) -> JSONResponse:
    """Call the OpenBB API and return its JSON (or a friendly error) verbatim."""
    params = {"provider": PROVIDER, **params}
    url = f"{OPENBB_API_URL}/api/v1/{path.lstrip('/')}"
    try:
        resp = requests.get(url, params=params, timeout=TIMEOUT)
    except requests.exceptions.ConnectionError:
        return JSONResponse(
            {
                "error": (
                    f"Could not reach the OpenBB API at {OPENBB_API_URL}. "
                    "Is it running? "
                    "(uvicorn openbb_core.api.rest_api:app --port 6900)"
                )
            },
            status_code=502,
        )
    except requests.exceptions.RequestException as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    try:
        return JSONResponse(resp.json(), status_code=resp.status_code)
    except ValueError:
        return JSONResponse(
            {"error": resp.text or "Non-JSON response from OpenBB API"},
            status_code=resp.status_code,
        )


Sym = Query(..., min_length=1, description="Ticker symbol")


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "index.html"))


# ---- Overview tab ---------------------------------------------------------
@app.get("/api/quote")
def quote(symbol: str = Sym):
    return obb_get("equity/price/quote", {"symbol": symbol.upper()})


@app.get("/api/historical")
def historical(symbol: str = Sym, range: str = "3mo"):
    days = RANGE_TO_DAYS.get(range, 90)
    interval = "1W" if days >= 365 * 2 else "1d"
    start = (date.today() - timedelta(days=days)).isoformat()
    return obb_get(
        "equity/price/historical",
        {"symbol": symbol.upper(), "interval": interval, "start_date": start},
    )


@app.get("/api/news")
def news(symbol: str = Sym):
    return obb_get("news/company", {"symbol": symbol.upper(), "limit": 12})


# ---- Multi-ticker + market widgets -----------------------------------------
@app.get("/api/quotes")
def quotes(symbols: str = Query(..., description="Comma-separated tickers")):
    return obb_get("equity/price/quote", {"symbol": symbols.upper()})


@app.get("/api/management")
def management(symbol: str = Sym):
    return obb_get("equity/fundamental/management", {"symbol": symbol.upper()})


@app.get("/api/index")
def index_series(symbol: str = Query(..., description="Index symbol e.g. ^GSPC"), range: str = "1mo"):
    days = RANGE_TO_DAYS.get(range, 30)
    start = (date.today() - timedelta(days=days)).isoformat()
    return obb_get(
        "index/price/historical",
        {"symbol": symbol, "interval": "1d", "start_date": start},
    )


# ---- RSS / Atom feeds: generic reader + multi-source breaking-news ---------
def _tag(el):
    return el.tag.split("}")[-1].lower()


def _parse_date(s):
    if not s:
        return None
    try:
        return email.utils.parsedate_to_datetime(s)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _ts(dt):
    try:
        return dt.timestamp() if dt else 0.0
    except Exception:
        return 0.0


def _parse_feed(content, source_fallback=None):
    """Parse RSS or Atom bytes -> (source_title, [items])."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return None, []
    source = source_fallback
    if source is None:
        for el in root.iter():
            if _tag(el) in ("channel", "feed"):
                for c in el:
                    if _tag(c) == "title" and c.text:
                        source = c.text.strip()
                        break
                break
    items = []
    for el in root.iter():
        if _tag(el) in ("item", "entry"):
            title = link = published = None
            for c in el:
                ct = _tag(c)
                if ct == "title" and c.text and not title:
                    title = c.text.strip()
                elif ct == "link":
                    href = c.get("href")
                    link = href if href else (c.text.strip() if c.text else link)
                elif ct in ("pubdate", "published", "updated", "date") and c.text and not published:
                    published = c.text.strip()
            if title:
                items.append({"title": title, "link": link, "date": published, "source": source})
        if len(items) >= 40:
            break
    return source, items


@app.get("/api/rss")
def rss(url: str = Query(..., description="RSS or Atom feed URL")):
    if not url.lower().startswith(("http://", "https://")):
        return JSONResponse({"error": "URL must start with http:// or https://"}, status_code=400)
    try:
        resp = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "Mozilla/5.0 (OpenBB personal dashboard)"})
    except requests.exceptions.RequestException as exc:
        return JSONResponse({"error": f"Could not fetch feed: {exc}"}, status_code=502)
    if resp.status_code != 200:
        return JSONResponse({"error": f"Feed returned HTTP {resp.status_code}"}, status_code=502)
    source, items = _parse_feed(resp.content)
    if source is None and not items:
        return JSONResponse({"error": "Not a valid RSS/Atom feed"}, status_code=422)
    return JSONResponse({"results": items}, status_code=200)


# Multi-source markets breaking-news (à la a Bloomberg / Sky live ticker)
BREAKING_FEEDS = [
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("Reuters", "https://news.google.com/rss/search?q=site:reuters.com+markets+when:1d&hl=en-US"),
    ("MarketWatch", "http://feeds.marketwatch.com/marketwatch/topstories/"),
    ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("Sky News", "https://feeds.skynews.com/feeds/rss/business.xml"),
    ("Markets", "https://news.google.com/rss/search?q=stock+market+when:1d&hl=en-US"),
]


@app.get("/api/breaking")
def breaking():
    out, seen = [], set()
    for name, url in BREAKING_FEEDS:
        try:
            r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0 (OpenBB personal dashboard)"})
            if r.status_code != 200:
                continue
            _, items = _parse_feed(r.content, source_fallback=name)
        except Exception:
            continue
        for it in items:
            key = (it.get("title") or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            it["_dt"] = _parse_date(it.get("date"))
            out.append(it)
    out.sort(key=lambda x: _ts(x.get("_dt")), reverse=True)
    for it in out:
        it.pop("_dt", None)
    return JSONResponse({"results": out[:40]}, status_code=200)


@app.get("/api/proxy")
def proxy(url: str = Query(..., description="Any URL returning JSON")):
    """Generic JSON fetcher for the custom-Endpoint widget (avoids browser CORS)."""
    if not url.lower().startswith(("http://", "https://")):
        return JSONResponse({"error": "URL must start with http:// or https://"}, status_code=400)
    try:
        resp = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "OpenBB personal dashboard"})
    except requests.exceptions.RequestException as exc:
        return JSONResponse({"error": f"Could not fetch: {exc}"}, status_code=502)
    try:
        return JSONResponse(resp.json(), status_code=resp.status_code)
    except ValueError:
        return JSONResponse({"error": "Endpoint did not return JSON"}, status_code=422)


# ---- Fundamentals tab -----------------------------------------------------
@app.get("/api/profile")
def profile(symbol: str = Sym):
    return obb_get("equity/profile", {"symbol": symbol.upper()})


@app.get("/api/metrics")
def metrics(symbol: str = Sym):
    return obb_get("equity/fundamental/metrics", {"symbol": symbol.upper()})


@app.get("/api/income")
def income(symbol: str = Sym):
    return obb_get(
        "equity/fundamental/income", {"symbol": symbol.upper(), "limit": 5}
    )


@app.get("/api/dividends")
def dividends(symbol: str = Sym):
    return obb_get("equity/fundamental/dividends", {"symbol": symbol.upper()})


# ---- Analysts tab ---------------------------------------------------------
@app.get("/api/consensus")
def consensus(symbol: str = Sym):
    return obb_get("equity/estimates/consensus", {"symbol": symbol.upper()})


# ---- Options tab ----------------------------------------------------------
@app.get("/api/options")
def options(symbol: str = Sym):
    return obb_get("derivatives/options/chains", {"symbol": symbol.upper()})


# ---- Health ---------------------------------------------------------------
# ---- Live TV news: resolve each channel's CURRENT live video (self-healing) --
LIVE_CHANNELS = {
    "bloomberg": "https://www.youtube.com/@markets/live",
    "skynews": "https://www.youtube.com/@SkyNews/live",
    "cnbc": "https://www.youtube.com/@CNBC/live",
    "cnn": "https://www.youtube.com/@CNN/live",
    "aljazeera": "https://www.youtube.com/@aljazeeraenglish/live",
    "france24": "https://www.youtube.com/@FRANCE24English/live",
    "dwnews": "https://www.youtube.com/@dwnews/live",
}


@app.get("/api/livestream")
def livestream(channel: str = Query(..., description="Channel key, e.g. skynews")):
    url = LIVE_CHANNELS.get(channel.lower().replace(" ", ""))
    if not url:
        return JSONResponse({"error": f"Unknown channel '{channel}'"}, status_code=400)
    # urllib (not requests) — requests gets hard-redirected to YouTube's EU consent
    # wall; urllib returns the real page with the current live video embedded.
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Cookie": "CONSENT=YES+1",
            "Accept-Language": "en-US",
        })
        html = urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "ignore")
    except Exception as exc:
        return JSONResponse({"error": f"Could not reach YouTube: {exc}"}, status_code=502)
    m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})">', html)
    if not m:
        m = re.search(r'"videoId":"([\w-]{11})"', html)
    if not m:
        return JSONResponse({"error": "No live stream found (channel may be offline)"}, status_code=404)
    return JSONResponse({"video_id": m.group(1), "live": '"isLiveNow":true' in html}, status_code=200)


@app.get("/api/search")
def search(q: str = Query(..., min_length=1, description="Ticker or company name")):
    """Symbol autocomplete via Yahoo Finance search (free, no key)."""
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v1/finance/search",
            params={"q": q, "quotesCount": 8, "newsCount": 0},
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        data = r.json()
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    out = []
    for x in data.get("quotes", []):
        sym = x.get("symbol")
        if not sym:
            continue
        out.append({
            "symbol": sym,
            "name": x.get("shortname") or x.get("longname") or "",
            "type": x.get("quoteType"),
            "exchange": x.get("exchDisp"),
        })
    return JSONResponse({"results": out}, status_code=200)


# ---- My Money: Irish tax-aware wealth engine (ported from the KKR Tracker) ---
@app.post("/api/wealth/projector")
async def wealth_projector(request: Request):
    """Irish retirement/SFT/CET projector + financial health score.
    Ported from the KKR Tracker's _generate_financial_advice."""
    p = await request.json()

    def f(key, default=0.0):
        try:
            return float(p.get(key) or default)
        except (TypeError, ValueError):
            return float(default)

    fx = f("fx_rate", 0.92)
    kkr_eur = f("kkr_total_usd") * fx
    pension = f("pension_pot")
    age = int(f("age", 43))
    income = f("annual_income")
    monthly = f("monthly_contrib")      # employee €/month payslip deduction
    lump = f("annual_lump")
    employer_pension = f("employer_pension")
    cash_bonus = f("cash_bonus")
    unvested_eq_usd = f("unvested_equity_usd")
    ret_age = int(f("ret_age", 65))

    employee_contrib = monthly * 12 + lump
    annual_contrib = employee_contrib + employer_pension
    net_bonus = cash_bonus * 0.52
    unvested_eq_eur = unvested_eq_usd * fx
    kkr_concentration = kkr_eur + unvested_eq_eur
    combined = pension + kkr_eur
    years_to_ret = max(0, ret_age - age)
    years_to_65 = max(0, 65 - age)
    years_to_60 = max(0, 60 - age)

    PENSION_GROWTH, KKR_GROWTH = 0.07, 0.12

    def fv(pv, contrib_pa, r, n):
        return pv * (1 + r) ** n + contrib_pa * ((1 + r) ** n - 1) / r if r else pv + contrib_pa * n

    proj_pension_ret = fv(pension, annual_contrib, PENSION_GROWTH, years_to_ret)
    proj_pension_65 = fv(pension, annual_contrib, PENSION_GROWTH, years_to_65)
    proj_pension_60 = fv(pension, annual_contrib, PENSION_GROWTH, years_to_60)
    proj_kkr_ret = kkr_eur * (1 + KKR_GROWTH) ** years_to_ret
    proj_kkr_65 = kkr_eur * (1 + KKR_GROWTH) ** years_to_65
    proj_total_ret = proj_pension_ret + proj_kkr_ret
    proj_total_65 = proj_pension_65 + proj_kkr_65

    SFT_SCHEDULE = {2024: 2_000_000, 2025: 2_150_000, 2026: 2_200_000, 2027: 2_400_000, 2028: 2_600_000}
    SFT_MAX = 2_800_000

    def sft_at_age(target_age):
        return SFT_SCHEDULE.get(2024 + max(0, target_age - age), SFT_MAX)

    sft_ret, sft_65 = sft_at_age(ret_age), sft_at_age(65)
    sft_excess_ret = max(0, proj_pension_ret - sft_ret)
    sft_excess_65 = max(0, proj_pension_65 - sft_65)
    cet_liability_ret = sft_excess_ret * 0.40
    cet_liability_65 = sft_excess_65 * 0.40
    sft_headroom_ret = sft_ret - proj_pension_ret
    sft_near_miss = sft_excess_ret == 0 and sft_headroom_ret < sft_ret * 0.10

    STATE_PENSION = 14_412
    gross_income_ret = proj_total_ret * 0.04 + STATE_PENSION
    gross_income_65 = proj_total_65 * 0.04 + STATE_PENSION

    MEDIAN_PENSION, TOP10_PENSION, TOP5_PENSION, TOP1_PENSION = 55_000, 180_000, 280_000, 500_000
    MEDIAN_WEALTH, TOP10_WEALTH, TOP5_WEALTH = 230_000, 650_000, 950_000

    def peer_pct(val, p50, p90, p95):
        if val >= p95:
            return "top 5%"
        if val >= p90:
            return "top 10%"
        if val >= p50 * 2:
            return "top 20%"
        if val >= p50:
            return "above median"
        return "below median"

    pension_peer = peer_pct(pension, MEDIAN_PENSION, TOP10_PENSION, TOP5_PENSION)
    wealth_peer = peer_pct(combined, MEDIAN_WEALTH, TOP10_WEALTH, TOP5_WEALTH)
    savings_rate = (annual_contrib / income * 100) if income else 0

    score = 50
    score += 20 if pension >= TOP1_PENSION else 15 if pension >= TOP5_PENSION else 10 if pension >= TOP10_PENSION else 0
    score += 10 if kkr_eur >= 200_000 else 5 if kkr_eur >= 100_000 else 0
    score += 10 if savings_rate >= 20 else 5 if savings_rate >= 10 else 0
    if sft_excess_65 > 0:
        score -= 5
    if annual_contrib == 0:
        score -= 10
    score = max(0, min(100, score))
    grade = "A+" if score >= 90 else "A" if score >= 80 else "B+" if score >= 70 else "B" if score >= 60 else "C"

    def fmt(n):
        return f"€{n:,.0f}"

    EARNINGS_CAP = 115_000
    relief_pct = 15 if age < 30 else 20 if age < 40 else 25 if age < 50 else 30 if age < 55 else 35 if age < 60 else 40
    max_ee_relief = EARNINGS_CAP * relief_pct / 100
    unused_relief = max(0, max_ee_relief - employee_contrib)
    unused_relief_tax_saving = unused_relief * 0.40

    sft_breach_age = None
    if pension > 0:
        for yr in range(1, years_to_65 + 1):
            if fv(pension, annual_contrib, PENSION_GROWTH, yr) > sft_at_age(age + yr):
                sft_breach_age = age + yr
                break

    def sft_safe_contrib(rage):
        n = max(1, rage - age)
        target = sft_at_age(rage)
        organic = pension * (1 + PENSION_GROWTH) ** n
        if organic >= target:
            return 0
        return (target - organic) / (((1 + PENSION_GROWTH) ** n - 1) / PENSION_GROWTH)

    safe_contrib_ret = sft_safe_contrib(ret_age)
    surplus_ret = max(0, annual_contrib - safe_contrib_ret)
    ee_safe = max(0, safe_contrib_ret - employer_pension)

    risks = []
    if sft_excess_ret > 0:
        risks.append(f"SFT breach at age {ret_age} — pension projected at {fmt(proj_pension_ret)} vs SFT of {fmt(sft_ret)}. Potential CET liability of {fmt(cet_liability_ret)} at 40%.")
    elif sft_near_miss:
        risks.append(f"SFT near-miss at age {ret_age} — projected {fmt(proj_pension_ret)} vs SFT {fmt(sft_ret)} (headroom {fmt(sft_headroom_ret)}). Higher growth or employer top-ups tip this into a breach.")
    if kkr_concentration > 200_000:
        risks.append(f"KKR concentration: career income + {fmt(unvested_eq_eur)} unvested equity + {fmt(kkr_eur)} personal portfolio all tied to KKR — a firm-level event hits salary, equity and investments together.")
    if kkr_eur > 0:
        risks.append(f"Personal KKR portfolio is USD-denominated. A 10% EUR/USD move = {fmt(kkr_eur * 0.10)} swing in EUR value.")
    if unvested_eq_usd > 0:
        risks.append(f"${unvested_eq_usd:,.0f} unvested KKR equity is taxed as income (PAYE+USC+PRSI ~52%) when it vests — plan cash for the tax bill.")

    actions = []
    if sft_breach_age:
        actions.append(f"Pension on course to breach SFT at age {sft_breach_age}. To stay SFT-safe at {ret_age}, cap total contributions at {fmt(safe_contrib_ret)}/yr (your own at {fmt(ee_safe)}/yr).")
    if unused_relief > 0:
        actions.append(f"{fmt(unused_relief)}/yr of unused Revenue relief at {relief_pct}% — worth {fmt(unused_relief_tax_saving)} in tax, but only top up if it doesn't accelerate an SFT breach.")
    if net_bonus > 0:
        actions.append(f"Net bonus ~{fmt(net_bonus)}/yr: diversify away from KKR (non-Irish-domiciled index ETFs → 33% CGT vs 41% exit tax).")
    actions.append("Hedge USD/EUR — KKR portfolio and unvested equity are USD-denominated; add EUR assets as a counterweight.")

    return JSONResponse({
        "score": score, "grade": grade, "combined_eur": round(combined),
        "pension_eur": round(pension), "kkr_eur": round(kkr_eur),
        "pension_peer": pension_peer, "wealth_peer": wealth_peer,
        "savings_rate": round(savings_rate, 1), "ret_age": ret_age, "age": age,
        "proj_pension_ret": round(proj_pension_ret), "proj_kkr_ret": round(proj_kkr_ret),
        "proj_total_ret": round(proj_total_ret), "proj_income_ret": round(gross_income_ret),
        "proj_pension_65": round(proj_pension_65), "proj_total_65": round(proj_total_65),
        "sft_ret": round(sft_ret), "sft_excess_ret": round(sft_excess_ret),
        "cet_liability_ret": round(cet_liability_ret), "cet_liability_65": round(cet_liability_65),
        "annual_contrib": round(annual_contrib), "employee_contrib": round(employee_contrib),
        "unused_relief": round(unused_relief), "sft_breach_age": sft_breach_age,
        "safe_contrib_ret": round(safe_contrib_ret), "ee_safe_contrib": round(ee_safe),
        "surplus_ret": round(surplus_ret), "net_bonus": round(net_bonus),
        "state_pension": STATE_PENSION, "risks": risks, "actions": actions,
    }, status_code=200)


@app.get("/api/wealth/cgt")
def wealth_cgt(proceeds: float = Query(...), cost: float = Query(...), prior_gains: float = Query(0)):
    """Irish CGT: 33% on net gains above the €1,270 annual exemption."""
    gain = proceeds - cost
    exemption_left = max(0, 1270 - prior_gains)
    taxable = max(0, gain - exemption_left)
    tax = taxable * 0.33
    return JSONResponse({
        "gain": round(gain, 2), "exemption_used": round(min(max(0, gain), exemption_left), 2),
        "taxable": round(taxable, 2), "cgt": round(tax, 2),
        "net_proceeds": round(proceeds - cost - tax, 2), "rate": 0.33,
    }, status_code=200)


@app.get("/api/health")
def health():
    resp = obb_get("equity/price/quote", {"symbol": "AAPL"})
    return JSONResponse(
        {"app": "ok", "openbb_api": OPENBB_API_URL, "reachable": resp.status_code == 200},
        status_code=200 if resp.status_code == 200 else 502,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
