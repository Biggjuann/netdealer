# Net Dealer Target — the **"C"** Calculator

A web app that computes the **net dealer target "C"** for *any* ticker and *any*
expiry — the price where the option book is delta-balanced and dealers (the
counterparty to that book) are directionally flat. That price is the magnet a
pinning market gets drawn toward: **"follow the dealer."**

It reproduces the annotated *Wave* framework from the `$MU` example — pick a
ticker + expiry, and the app returns the **C** target plus the supporting
structure (max pain, call/put walls, long/short OI averages) and a full,
netDIR-colored option chain.

Live option data comes from **Schwab**, read through the **shared token**
already used by the [`0DTE`](../0DTE) / MM services — this app holds **no Schwab
credentials of its own**.

---

## What "C" is, and how it's computed

For a candidate underlying price `P`, the **net dealer directional exposure** of
the open-interest book is

```
netDelta(P) = 100 · Σ_strikes [ Δcall(P,K)·OI_call(K) + Δput(P,K)·OI_put(K) ]
```

where `Δcall ∈ [0,1]` and `Δput = Δcall − 1 ∈ [−1,0]` are Black–Scholes deltas
evaluated *at the candidate price* using each strike's implied vol.

* As `P` rises, call deltas → 1 and put deltas → 0, so `netDelta → +ΣOI_call`
  (book net-long upside ⇒ **dealers net-short ⇒ they want price down**).
  This is the screenshot's `netDIR = +46%` up at the 990 strike.
* As `P` falls, call deltas → 0 and put deltas → −1, so `netDelta → −ΣOI_put`
  (**dealers net-long ⇒ they want price up**). The `-45%` down at 760.

`netDelta(P)` is strictly increasing in `P`, so it has a single zero crossing:

> **C** = the price `P*` where `netDelta(P*) = 0` — the delta-neutral,
> net-dealer-flat price.

In the `$MU` screenshot `netDIR` crosses from `+0.4%` (845 strike) to `-0.5%`
(840 strike); interpolating the crossing lands on **840.72** — exactly the
printed "C" target. The app solves for that crossing directly (bisection).

### Supporting levels (same chain)

| Level | Meaning |
|---|---|
| **Max Pain** | strike minimising total intrinsic payout to holders (corroborates C) |
| **Call Wall ▲** | strike with the most call OI — above it dealers are heavily short → **downward** pull |
| **Put Wall ▼** | strike with the most put OI — below it dealers are heavily long → **upward** pull |
| **Long Avg** | call-OI-weighted average strike (the "LongAvg" line) |
| **Short Avg** | put-OI-weighted average strike (the "ShortAvg" line) |

A bigger call wall above pulls **C down** (price must stay low to keep those
calls OTM / the book balanced); a bigger put wall below pulls **C up**. C sits
between the two walls — the "crush" zone in the framework.

> ⚠️ **Not financial advice.** This is a transparent re-implementation of the
> *concept* using standard dealer-positioning math (BS deltas over open
> interest). It is not the proprietary Wave engine and makes the usual
> simplifying assumption that OI is customer-long / dealer-short.

---

## Using it

* **Ticker** — choose from the dropdown or type any symbol.
* **Expiry** — auto-populates from the live chain (mock mode lists the next 5 Fridays).
* **Calculate C** — returns the target and renders the profile + chain.

The **Net Dealer Directional Profile** plots `netDIR(P)` across strikes with the
zero-crossing (C, pink) and spot (blue) marked. The chain table shows calls
(left) / strike / netDIR / puts (right), with the **C row** highlighted and the
walls outlined.

---

## Running locally

```bash
pip install -r requirements.txt
python run.py            # http://localhost:8080  (mock mode, no token needed)
```

Go **live** (real Schwab chains via the shared token):

```bash
cp .env.example .env
# set DATA_MODE=live and SCHWAB_TOKEN_SHARE_KEY=... (== MM's share key)
python run.py
```

### The shared Schwab token

Identical mechanism to the `0DTE` app — no Schwab OAuth here:

```
GET  <SCHWAB_TOKEN_URL>   Authorization: Bearer <SCHWAB_TOKEN_SHARE_KEY>
->   { "access_token": "...", "expires_in": 1800 }
```

The access token is then used as the bearer for Schwab market-data calls
(`/marketdata/v1/chains`). `SCHWAB_TOKEN_URL` defaults to `MM_BASE_URL +
/auth/token`; `SCHWAB_TOKEN_SHARE_KEY` falls back to `MM_API_KEY`. A `401`
transparently refreshes the token and retries (it rotates ~every 30 min).

Key env vars (full list in [`.env.example`](.env.example)):

| Var | Purpose |
|---|---|
| `DATA_MODE` | `mock` (default) or `live` |
| `SCHWAB_TOKEN_URL` | shared-token endpoint (default `MM_BASE_URL/auth/token`) |
| `SCHWAB_TOKEN_SHARE_KEY` | bearer for the token endpoint (**required for live**) |
| `TICKERS` | symbols in the dropdown |
| `RISK_FREE_RATE`, `FALLBACK_IV`, `STRIKE_COUNT` | model knobs |
| `CORS_ORIGINS` | origins allowed to call the API (set to your Pages URL) |

---

## Hosting on GitHub

Two GitHub-native pieces:

1. **Front-end → GitHub Pages.** The [`docs/`](docs) folder is a self-contained
   static build of the dashboard. Enable **Settings → Pages → Deploy from
   GitHub Actions**; the [`Deploy Pages`](.github/workflows/pages.yml) workflow
   publishes it on every push to `main`.

2. **Back-end API → a host that can hold a secret.** GitHub Pages is static and
   cannot hold the shared-token key, so deploy this FastAPI app to Railway
   (config included: [`Procfile`](Procfile), [`railway.json`](railway.json)) —
   the same way `0DTE` deploys. Set `DATA_MODE=live` and
   `SCHWAB_TOKEN_SHARE_KEY` there.

Then open the Pages site, click **⚙** (top-right), and point it at your deployed
backend URL. Set `CORS_ORIGINS` on the backend to your Pages origin.

> Prefer a single service? The FastAPI app *also* serves the dashboard at `/`,
> so deploying just the backend gives you the full app at one URL — no Pages
> needed. The Pages copy exists for a purely GitHub-hosted front-end.

[`CI`](.github/workflows/ci.yml) runs the engine tests and a mock-mode boot
smoke-test on every push.

---

## Layout

```
app/
  net_dealer.py         # the "C" engine — pure, unit-tested math
  config.py             # env-driven settings (shared-token vars)
  server.py             # FastAPI: /api/config, /api/expiries, /api/net-dealer
  providers/
    token.py            # SharedTokenProvider (shared Schwab access token)
    schwab.py           # SchwabChainClient — chains + expirations (read-only)
    mock.py             # synthetic chain (default; zero network)
    factory.py          # mock vs live wiring
  static/dashboard.html # the UI (ticker + expiry selectors)
docs/index.html         # same UI, for GitHub Pages
tests/test_net_dealer.py
```

## API

```
GET /api/config                        -> mode, tickers, model params
GET /api/expiries?ticker=MU            -> { "expiries": ["2026-08-07", ...] }
GET /api/net-dealer?ticker=MU&expiry=2026-08-07
    -> { c_target, c_netdir, max_pain, call_wall, put_wall,
         long_avg, short_avg, spot, dte, rows: [{strike, call_oi, put_oi,
         netdir, ...}], ... }
```
