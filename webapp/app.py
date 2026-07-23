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
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse

app = FastAPI(title="My OpenBB Dashboard")

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
