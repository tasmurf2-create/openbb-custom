# My OpenBB Dashboard

A small, self-hosted personal frontend on top of the **OpenBB Platform API**.
You own it, you tweak it, you deploy it. OpenBB supplies all the data/feeds.

```
Browser  ->  this Flask app (/api/*)  ->  OpenBB Platform API  ->  data feeds
```

The browser never talks to OpenBB directly — this app proxies each call server
side, so there's no CORS setup and the OpenBB API URL stays configurable.

## Run locally

You need the OpenBB Platform API running (that's the data engine).

1. **Start the OpenBB API** (from the repo's `.venv`):
   ```bash
   ../.venv/Scripts/python.exe -m uvicorn openbb_core.api.rest_api:app --host 127.0.0.1 --port 6900
   ```

2. **Start this app** (in another terminal):
   ```bash
   # uses the same venv, which has flask + requests installed
   ../.venv/Scripts/python.exe app.py
   # -> http://localhost:5000
   ```

The app defaults to the OpenBB API at `http://localhost:6900`. Override with the
`OPENBB_API_URL` env var.

## Configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `OPENBB_API_URL` | `http://localhost:6900` | Where the OpenBB Platform API lives |
| `OPENBB_PROVIDER` | `yfinance` | Default data provider (free, no key) |
| `PORT` | `5000` | Port this app listens on |

## Deploy to Render (personal use)

`render.yaml` (in this folder) is a Blueprint that deploys **two** services:
the OpenBB data API and this frontend. On Render's Linux the full `openbb`
package installs with prebuilt wheels — no compiler needed.

1. Push this repo to GitHub.
2. Render → **New → Blueprint** → pick the repo → apply.
3. Render wires `OPENBB_API_URL` from the frontend to the API service automatically.

## Where to tweak

- **Add endpoints / data:** new routes in `app.py` that call other OpenBB paths
  (see the full list at `http://localhost:6900/docs`). E.g. options, fundamentals,
  economy, crypto, ETFs — anything OpenBB exposes.
- **Add premium providers:** install the provider package into the API's venv
  (e.g. `pip install openbb-fmp`) and set its API key; then pass `provider=fmp`.
- **Change the UI:** it's all in `index.html` (vanilla JS + Chart.js).
