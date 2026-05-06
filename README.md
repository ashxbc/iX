# iX

Custom trading terminal with **true** Cumulative Volume Delta from Binance Spot.
Uses real tick-level aggressor classification (`aggTrade.m`) — no candle approximation.

## Run

```powershell
cd backend
pip install -r requirements.txt

# Generate a 24-char access token and save it locally (never commit this file).
python -c "import secrets; print('IX_ACCESS_TOKEN=' + secrets.token_urlsafe(18))" > .env

uvicorn main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000 and paste the token from `backend/.env`.

## Access control

Every API and WebSocket request requires the token from `IX_ACCESS_TOKEN`.
The frontend stores it in `localStorage` after one successful login. Click
**logout** in the header to forget it.

The check uses `secrets.compare_digest`, so token comparison is constant-time.
The `.env` file is in `.gitignore` and must never leave your private machines.

## Stack

- **Backend** — FastAPI + Binance Futures `aggTrade` websocket
- **CVD** — true delta per trade: `m=true` → sell aggression, `m=false` → buy aggression
- **Divergence** — pivot-based detector (bear: price HH + CVD LH, bull: price LL + CVD HL)
- **Frontend** — TradingView Lightweight Charts (open source), vanilla JS

## Why this is leading, not lagging

Most retail oscillators (RSI, MACD) derive from price itself, so they always lag.
CVD divergence reads **order flow**: it shows when aggressive buyers stop showing up
*before* price reverses. The reversal is often visible in CVD one or more candles
before price confirms.

## 24/7 Operation

All `(symbol, timeframe)` engines start at server boot and run continuously,
regardless of whether a frontend is connected. Every observed candle is
persisted to `backend/data.db` (SQLite, WAL mode):

- The in-progress candle is flushed to disk every ~2 seconds, so a crash loses
  at most a couple seconds of delta.
- On restart, each engine restores its candle history and resumes CVD from the
  last persisted value. No reset to zero.
- Restarts produce a small CVD discontinuity equal to whatever trades happened
  during the downtime — keep the server running for true continuous CVD.

## Endpoints

- `GET  /api/symbols` — supported symbols + timeframes
- `GET  /api/status`  — live state of every engine (candles, CVD, last bar)
- `WS   /ws/{symbol}/{timeframe}` — snapshot + live ticks + divergence updates

## Notes

- CVD line is drawn **only on observed candles**. Older padding bars (from
  klines) show price but no fake CVD line.
- Divergence pivots use `left=5, right=2`, so signals appear 2 candles after a
  pivot forms. Tune in `backend/divergence.py` if you want earlier/later
  confirmation.
- Persistence is in `backend/data.db`. Delete it to wipe history.
