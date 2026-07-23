# My OpenBB Dashboard

A self-hosted, fully tweakable personal finance **dashboard** built on top of a
self-hosted **OpenBB Platform API**. It recreates the OpenBB Workspace experience
(drag/resize widget grid, multiple dashboards, live news) — but it's *yours*: your
code, your server, deployable to Render, with a personal Irish-tax wealth engine
layered in.

```
Browser ── /api/* ──►  this FastAPI app (webapp/)  ──►  OpenBB Platform API :6900  ──►  yfinance / data
                                    │
                                    ├──►  Yahoo symbol search  (autocomplete)
                                    ├──►  RSS/Atom feeds        (news ticker, RSS widget)
                                    ├──►  YouTube /live pages   (live TV resolver)
                                    └──►  any JSON API          (custom Endpoint widget)
```

The browser never calls those sources directly — the app proxies everything
server-side, so there's no CORS setup and the OpenBB API location stays configurable.

---

## Tech stack

| Layer | Tech |
|---|---|
| Backend | Python 3.12, **FastAPI** + uvicorn (`webapp/app.py`) — a backend-for-frontend |
| Data engine | Self-hosted **OpenBB Platform** REST API (`openbb-core` + provider extensions) on port 6900 |
| Market data | `yfinance` (free, no key) via OpenBB; Yahoo search for autocomplete |
| Frontend | Single `webapp/index.html` — vanilla JS, **Gridstack** (drag/resize), **Chart.js** |
| Persistence | Browser `localStorage` (dashboards, layouts, widget config) |
| Wealth engine | Ported from the KKR Tracker — Irish SFT/CET/ARF/CGT logic |

---

## Branches

| Branch | State | Contents |
|---|---|---|
| **`master`** | Stable | The OpenBB dashboard: widget grid, multiple dashboards, Company Monitor preset, market/fundamentals/options/news widgets, autocomplete, breaking-news ticker, live TV news, RSS/Iframe/YouTube/Endpoint utility widgets |
| **`wealth-integration`** | Active (Phase 1 done) | Everything in `master` **plus** the "My Money" widgets (Retirement Projector, CGT Calculator, My Portfolio), per-widget ticker override (⚙), and skeleton loaders |

- Preview the branch: you're on it when checked out; hard-refresh `http://localhost:5000`.
- Back to stable: `git checkout master`.
- Merge when happy: open a PR `master...wealth-integration` on GitHub.

### Roadmap
- **Phase 2** — Statement upload & analysis: pension statement parser + **KKR portfolio PDF parser** + fund classification/benchmarking (needs `pdfplumber`).
- **Phase 3** — A default "My Wealth" dashboard; remaining UI polish (sparklines, chart depth, light mode, mobile).

---

## Run locally

Two services share one venv at `../.venv` (Python 3.12; the repo root's full
`openbb` install isn't possible on this ARM-Windows box, so the API runs on
`openbb-core` + free provider extensions).

**1. OpenBB data API** (port 6900):
```bash
../.venv/Scripts/python.exe -m uvicorn openbb_core.api.rest_api:app --host 127.0.0.1 --port 6900
```

**2. This dashboard** (port 5000):
```bash
../.venv/Scripts/python.exe app.py
# -> http://localhost:5000
```

Both are also wired into `nestegg/.claude/launch.json` as `openbb-api` and `openbb-webapp`.

### Config (env vars)
| Var | Default | Purpose |
|---|---|---|
| `OPENBB_API_URL` | `http://localhost:6900` | Where the OpenBB API lives |
| `OPENBB_PROVIDER` | `yfinance` | Default data provider (free) |
| `PORT` | `5000` | Port this app listens on |

---

## Widgets

Added via **+ Add Widgets** (searchable library with categories + multi-select).
Each widget can be dragged (by its header), resized (any edge), removed (✕), and —
for market widgets — pinned to its own ticker via the **⚙ gear**.

| Category | Widgets |
|---|---|
| **Price** | Quote · Historical Stock Price (chart) · Market Overview (indices) · Watchlist |
| **Analyst Estimates** | Price Target (consensus) |
| **Asset Overview** | Ticker Profile · Management Team |
| **Comparison** | Comparison Analysis (multi-ticker) |
| **Fundamental Analysis** | Key Metrics · Valuation Multiples · Financial Statements · Dividend Payment |
| **Options** | Options Chain |
| **My Money** *(wealth-integration)* | Retirement Projector (Irish) · CGT Calculator · My Portfolio (P&L) |
| **News** | News (per-ticker) |
| **Utils** | RSS / Atom Feed (ticker-aware via `{ticker}`) · Iframe · YouTube · Custom Endpoint (JSON) · Live News (TV) |

**Global features:** breaking-news ticker (multi-source, top of app) · ticker
autocomplete · multiple saved dashboards · one-click **Company Monitor** template.

---

## API endpoints (`webapp/app.py`)

### Market (proxied to the OpenBB API)
| Route | Purpose |
|---|---|
| `GET /api/quote` `/api/historical` `/api/news` | Quote, OHLCV history, company news |
| `GET /api/quotes?symbols=` | Multi-ticker quotes (watchlist) |
| `GET /api/profile /api/metrics /api/income /api/dividends /api/consensus /api/options` | Fundamentals, analyst consensus, options chain |
| `GET /api/management` `/api/index?symbol=` | Management team, index history (Market Overview) |
| `GET /api/search?q=` | Symbol autocomplete (Yahoo) |

### News / media / utility
| Route | Purpose |
|---|---|
| `GET /api/breaking` | Multi-source breaking-news aggregator (CNBC/Reuters/MarketWatch/BBC/markets) |
| `GET /api/rss?url=` | Fetch & parse any RSS/Atom feed (`{ticker}` supported by the widget) |
| `GET /api/livestream?channel=` | Resolves a channel's **current** YouTube live video id (self-healing) |
| `GET /api/proxy?url=` | Generic JSON fetcher for the Custom Endpoint widget |

### My Money (wealth-integration)
| Route | Purpose |
|---|---|
| `POST /api/wealth/projector` | Irish retirement projector — SFT/CET, health score, risks & actions |
| `GET /api/wealth/cgt` | Irish CGT (33%, €1,270 exemption) |

`GET /api/health` — checks the app can reach the OpenBB API.

---

## Deploy to Render

`render.yaml` is a Blueprint deploying **both** services (API + dashboard). On
Render's Linux x86-64 the full `openbb` package installs with prebuilt wheels
(no compiler needed — unlike local ARM-Windows). Push to GitHub → Render **New →
Blueprint** → pick the repo. `OPENBB_API_URL` is wired between the two services
automatically.

> Note: if you make the deployment public, lock down `/api/proxy` (it fetches
> arbitrary URLs) and consider rate limits.

---

## Data notes & caveats

- **Free tier:** all widgets run on `yfinance`. Richer widgets (Insider Trading,
  Financial Ratios, Earnings History) need a keyed provider like FMP:
  `pip install openbb-fmp` into the venv, set the key, pass `provider=fmp`.
- **Live TV:** resolves each channel's current live stream, so it stays live and
  self-heals; falls back to an "Open Live ↗" link if a channel disables embedding.
- **Iframe:** many big sites block embedding (`X-Frame-Options`).
- **Wealth engine:** ported from the KKR Tracker; it encodes Irish tax rules
  (SFT schedule to 2029, CET 40%, CGT 33%, earnings cap €115k) and personal
  assumptions (monthly contributions). Review constants each January.

---

## Provenance

- **Market UI + data**: this app, on the open-source OpenBB Platform (the OpenBB
  Workspace GUI itself is closed-source and not used here).
- **My Money engine**: ported from `kkr-tracker/server.py`
  (`_generate_financial_advice`, CGT logic); related to the `nestegg` app.
