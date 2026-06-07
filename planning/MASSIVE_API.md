# Massive API (formerly Polygon.io) — Reference

Massive (massive.com) is the rebrand of Polygon.io. The REST API and Python client are backward-compatible; only the package name changed from `polygon-api-client` to `massive`.

## Authentication

All requests require an API key passed as a Bearer token.

**HTTP header:**
```
Authorization: Bearer YOUR_API_KEY
```

**Python client:**
```python
from massive import RESTClient

client = RESTClient(api_key="YOUR_API_KEY")
```

**Base URL:** `https://api.massive.com`

---

## Rate Limits

| Tier | Requests / minute | Recommended poll interval |
|------|-------------------|--------------------------|
| Free | 5 | 15 s |
| Starter / paid | Unlimited | 2–5 s |

On the free tier, one call every 15 seconds stays comfortably under the 5 req/min ceiling.

---

## Key Endpoints for This Project

### 1. Batch Snapshot (multiple tickers in one call)

This is the primary endpoint for the project's polling loop. Returns the latest trade, quote, current-day OHLC, and **previous-day close** for a comma-separated list of tickers.

```
GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,TSLA,GOOGL
```

**Python client:**
```python
snapshots = client.get_snapshot_all("stocks", tickers=["AAPL", "TSLA", "GOOGL"])
for snap in snapshots:
    ticker   = snap.ticker
    price    = snap.last_trade.price          # latest trade price
    prev_c   = snap.prev_day.close            # previous close (% change baseline)
    change_p = snap.todays_change_perc        # pre-computed % change vs prev close
    print(f"{ticker}  ${price:.2f}  {change_p:+.2f}%")
```

**Raw HTTP example:**
```python
import httpx

resp = httpx.get(
    "https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers",
    params={"tickers": "AAPL,TSLA"},
    headers={"Authorization": f"Bearer {api_key}"},
    timeout=10,
)
data = resp.json()
for t in data["tickers"]:
    print(t["ticker"], t["lastTrade"]["p"], t["prevDay"]["c"])
```

**Response structure:**
```json
{
  "status": "OK",
  "count": 2,
  "tickers": [
    {
      "ticker": "AAPL",
      "todaysChange": 0.98,
      "todaysChangePerc": 0.82,
      "updated": 1605195918306274000,
      "day": {
        "o": 119.62, "h": 120.53, "l": 118.81, "c": 120.42,
        "v": 28727868, "vw": 119.725
      },
      "lastTrade": {
        "p": 120.47,
        "s": 236,
        "t": 1605195918306274000
      },
      "lastQuote": {
        "P": 120.47,
        "p": 120.46,
        "S": 4,
        "s": 8,
        "t": 1605195918507251700
      },
      "prevDay": {
        "o": 117.19, "h": 119.63, "l": 116.44, "c": 119.49,
        "v": 110597265, "vw": 118.4998
      },
      "min": {
        "o": 120.435, "h": 120.468, "l": 120.37, "c": 120.42,
        "v": 270796, "vw": 120.4129,
        "t": 1684428720000
      }
    }
  ]
}
```

**Key fields:**
- `lastTrade.p` — latest trade price (use as current price)
- `prevDay.c` — previous session close (use as % change baseline)
- `todaysChangePerc` — pre-computed `(lastTrade.p - prevDay.c) / prevDay.c * 100`

---

### 2. Single Ticker Snapshot

```
GET /v2/snapshot/locale/us/markets/stocks/tickers/{ticker}
```

```python
snap = client.get_snapshot("stocks", "AAPL")
price  = snap.last_trade.price
prev_c = snap.prev_day.close
```

Use this to validate a ticker before adding it to the watchlist: a 404 response means the symbol is unknown.

---

### 3. Previous Day Bar (OHLC)

Returns the official previous session's open/high/low/close for a single ticker.

```
GET /v2/aggs/ticker/{ticker}/prev
```

```python
import httpx

resp = httpx.get(
    f"https://api.massive.com/v2/aggs/ticker/AAPL/prev",
    headers={"Authorization": f"Bearer {api_key}"},
    timeout=10,
)
result = resp.json()["results"][0]
prev_close = result["c"]   # closing price of previous session
```

**Response:**
```json
{
  "status": "OK",
  "ticker": "AAPL",
  "results": [
    {
      "T": "AAPL",
      "o": 115.55, "h": 117.59, "l": 114.13, "c": 115.97,
      "v": 131704427, "vw": 116.3058,
      "t": 1605042000000
    }
  ]
}
```

---

### 4. Ticker Validation

Before accepting a user-submitted ticker, verify it exists:

```python
import httpx

def validate_ticker(ticker: str, api_key: str) -> bool:
    resp = httpx.get(
        f"https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=5,
    )
    return resp.status_code == 200
```

---

## Installation

```bash
pip install -U massive
```

Requires Python 3.9+. The `massive` package is the official client; it replaces the old `polygon-api-client`.

---

## Notes

- **Market hours**: The snapshot returns the latest available data. Outside market hours, `lastTrade.p` reflects the most recent after-hours or pre-market print. For this project that is acceptable — the price feed simply goes quiet and the simulator is the recommended default.
- **Timestamp format**: All `t` fields are Unix nanoseconds (divide by 1e9 for seconds).
- **Previous close field**: `prevDay.c` is the correct baseline for computing daily % change — identical to what quote vendors and Bloomberg display.
