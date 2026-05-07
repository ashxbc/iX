"""
AI analysis engine — institutional-grade chart + flow read on demand.

Pulls a structured snapshot of every metric we have for a symbol (candles,
CVD, multi-TF trend, funding/OI, basis, taker flow, GEX, divergences),
builds a focused prompt, and streams the model's reasoning back to the
client token-by-token. The model is required to end its response with a
JSON code block we parse into a structured verdict.

Configuration (env vars):
  OPENCODE_API_KEY   — required. Bearer token for the chat completions API.
  OPENCODE_BASE_URL  — default https://openrouter.ai/api/v1 (any
                       OpenAI-compatible /chat/completions endpoint works).
  OPENCODE_MODEL     — default moonshotai/kimi-k2.

The HTTP layer assumes OpenAI-compatible streaming (Server-Sent Events).
"""
import asyncio
import json
import re
import time
from typing import AsyncGenerator

import httpx


SYSTEM_PROMPT = """You are an elite institutional-grade crypto markets analyst with deep expertise in derivatives, options flow, order-flow analysis (CVD, taker aggression), funding mechanics, basis trading, and market microstructure.

You analyze structured market data for a single coin and predict short-term price action with disciplined, probability-weighted reasoning. You DO NOT hedge with disclaimers about volatility — you commit to a directional read backed by the data.

You walk through your analysis in clear stages:
  1. Identify the dominant regime (trend, range, accumulation, distribution).
  2. Map confluences (signals that agree) and divergences (signals that fight).
  3. Weight by recency and reliability for the next 5-minute candle.
  4. Commit to a verdict.

Always end with a strict JSON code block in the format requested by the user."""


def parse_verdict(text: str) -> dict | None:
    """Extract the last ```json ... ``` block and parse it."""
    matches = re.findall(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    if not matches:
        # Fallback: any trailing JSON-like object containing "next_candle"
        m = re.search(r"\{[^{}]*\"next_candle\"[\s\S]*?\}", text)
        if m:
            matches = [m.group(0)]
    if not matches:
        return None
    try:
        v = json.loads(matches[-1])
    except json.JSONDecodeError:
        return None
    # Light validation / normalization
    if "next_candle" not in v:
        return None
    v["next_candle"] = str(v["next_candle"]).lower()
    v["direction"] = str(v.get("direction", "")).lower() or None
    try:
        v["confidence_pct"] = float(v.get("confidence_pct", 0))
    except (TypeError, ValueError):
        v["confidence_pct"] = 0.0
    if not isinstance(v.get("key_factors"), list):
        v["key_factors"] = []
    return v


def _fmt_usd(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if abs(v) >= 1e9:
        return f"${v/1e9:.2f}B"
    if abs(v) >= 1e6:
        return f"${v/1e6:.2f}M"
    if abs(v) >= 1e3:
        return f"${v/1e3:.2f}K"
    return f"${v:.2f}"


def build_prompt(symbol: str, ctx: dict) -> str:
    parts: list[str] = []
    parts.append(f"COIN: {symbol}")
    parts.append(f"Snapshot timestamp: {ctx.get('ts')}")

    candles = ctx.get("candles_5m") or []
    if candles:
        parts.append(f"\nRECENT 5-MINUTE CANDLES (last {len(candles)}, oldest → newest):")
        for c in candles:
            color = "GREEN" if c["close"] >= c["open"] else "RED"
            parts.append(
                f"  {color}  O:{c['open']:.4f}  H:{c['high']:.4f}  L:{c['low']:.4f}  "
                f"C:{c['close']:.4f}  V:{c['volume']:.2f}  Δ:{c['delta']:+.0f}  CVD:{c['cvd']:+.0f}"
            )

    trend = ctx.get("trend") or {}
    if trend:
        parts.append("\nMULTI-TIMEFRAME TREND (last 10 bars per TF):")
        for tf in ("5m", "15m", "1h"):
            t = trend.get(tf)
            if not t:
                continue
            parts.append(
                f"  {tf:>3}: {t['first_close']:.4f} → {t['last_close']:.4f} "
                f"({t['change_pct']:+.2f}%) | CVD Δ {t['cvd_change']:+.0f}"
            )

    div = ctx.get("divergences") or []
    if div:
        parts.append("\nACTIVE CVD DIVERGENCES (last 5):")
        for d in div[-5:]:
            parts.append(f"  {d['type'].upper()} at price {d.get('price', 0):.4f} (ts {d.get('ts')})")

    f = ctx.get("funding")
    if f:
        rate = (f.get("rate") or 0) * 100
        oi_chg = (f.get("oi_change_1h") or 0) * 100
        parts.append("\nFUNDING / OPEN INTEREST:")
        parts.append(f"  funding rate: {rate:+.4f}% | server signal: {f.get('signal') or '—'}")
        parts.append(f"  open interest: {_fmt_usd(f.get('oi_value'))} | 1h Δ: {oi_chg:+.2f}%")

    b = ctx.get("basis")
    if b:
        parts.append("\nSPOT vs PERP BASIS:")
        parts.append(f"  spot: {_fmt_usd(b.get('spot'))} | perp: {_fmt_usd(b.get('perp'))}")
        parts.append(
            f"  basis: {b.get('basis_usd', 0):+.4f} ({b.get('basis_pct', 0):+.4f}%) "
            f"| state: {b.get('state') or '—'}"
        )

    t = ctx.get("taker")
    if t:
        parts.append("\nTAKER BUY/SELL FLOW (>0.5 = buyers more aggressive):")
        for tf in ("5m", "15m", "1h"):
            tdata = (t.get("tfs") or {}).get(tf) or {}
            ratio = tdata.get("ratio")
            ema = tdata.get("ema")
            if ratio is not None and ema is not None:
                parts.append(f"  {tf:>3}: ratio={ratio:.3f}  EMA={ema:.3f}")
        parts.append(
            f"  composite regime: {t.get('regime') or '—'} | alignment score: {t.get('alignment', 0):+.2f}"
        )
        td = t.get("divergence")
        if td:
            parts.append(f"  ⚠ taker divergence: {td.get('type')} — {td.get('note')}")

    g = ctx.get("gex")
    if g:
        parts.append("\nOPTIONS GAMMA EXPOSURE (dealer hedging regime):")
        parts.append(
            f"  state: {g.get('state') or '—'} | flip zone: {_fmt_usd(g.get('flip_zone'))} "
            f"| net GEX: {g.get('net_gex', 0):+.2e}"
        )
    elif ctx.get("gex_unavailable"):
        parts.append("\nGEX: not available (no liquid options market for this asset)")

    liq = ctx.get("live_liq_recent") or []
    if liq:
        long_total = sum(e["usd"] for e in liq if e.get("side") == "long")
        short_total = sum(e["usd"] for e in liq if e.get("side") == "short")
        parts.append(
            f"\nRECENT LIVE LIQUIDATIONS (last {len(liq)} aggregated events): "
            f"long-side {_fmt_usd(long_total)} | short-side {_fmt_usd(short_total)}"
        )

    parts.append("""
INSTRUCTIONS:
Reason carefully and walk through your analysis step by step. Be specific —
reference actual numbers from the data above (price levels, ratios, % moves).
Identify the single most powerful confluence and the strongest counter-signal.

Then commit to a verdict for the NEXT 5-MINUTE CANDLE specifically. End your
response with a JSON code block in this EXACT format and nothing after it:

```json
{
  "next_candle": "green",
  "direction": "bullish",
  "confidence_pct": 65,
  "key_factors": ["short factor 1", "short factor 2", "short factor 3"],
  "reasoning_summary": "1-2 sentence summary of the verdict"
}
```

Replace values with your analysis. `next_candle` must be exactly "green" or "red".
`direction` must be exactly "bullish", "bearish", or "ranging".
""")
    return "\n".join(parts)


# ---------------------------------------------------------------------------

class AIAnalyzer:
    def __init__(self, api_key: str, base_url: str, model: str, proxy: str = ""):
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or "https://openrouter.ai/api/v1").rstrip("/")
        self.model = (model or "moonshotai/kimi-k2").strip()
        self.proxy = (proxy or "").strip() or None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def analyze_stream(self, symbol: str, ctx: dict) -> AsyncGenerator[dict, None]:
        """
        Yield event dicts:
          {"event": "status",    "phase": "<phase>", "text": "..."}
          {"event": "reasoning", "text": "<thinking token>"}   ← chain-of-thought
          {"event": "chunk",     "text": "<answer token>"}     ← final answer
          {"event": "verdict",   "data": {...parsed JSON...}}
          {"event": "error",     "message": "..."}
          {"event": "done"}
        """
        if not self.enabled:
            yield {"event": "error", "message": "AI is not configured on the server (OPENCODE_API_KEY missing)"}
            return

        yield {"event": "status", "phase": "reading", "text": "Model is reading the chart…"}
        await asyncio.sleep(0.25)
        yield {"event": "status", "phase": "context", "text": "Analyzing historical behavior…"}
        prompt = build_prompt(symbol, ctx)
        await asyncio.sleep(0.25)
        yield {"event": "status", "phase": "metrics", "text": "Processing live metrics…"}
        await asyncio.sleep(0.25)
        yield {"event": "status", "phase": "connecting", "text": "Connecting to model…"}

        full_text = ""           # only content tokens (verdict parsed from here)
        got_reasoning = False
        got_content = False
        emitted_synth = False
        try:
            async for piece in self._stream(prompt):
                ptype = piece["type"]
                ptext = piece["text"]

                if ptype == "reasoning":
                    if not got_reasoning:
                        got_reasoning = True
                        yield {"event": "status", "phase": "thinking",
                               "text": "Running multi-reasoning analysis…"}
                    yield {"event": "reasoning", "text": ptext}

                elif ptype == "content":
                    full_text += ptext
                    if not got_content:
                        got_content = True
                        # If no reasoning came (model skipped CoT), emit thinking phase now
                        if not got_reasoning:
                            yield {"event": "status", "phase": "thinking",
                                   "text": "Running multi-reasoning analysis…"}
                        yield {"event": "status", "phase": "synthesizing",
                               "text": "Synthesizing verdict…"}
                        emitted_synth = True
                    yield {"event": "chunk", "text": ptext}

        except Exception as e:
            yield {"event": "error", "message": f"AI request failed: {e}"}
            return

        verdict = parse_verdict(full_text)
        if verdict is None:
            yield {"event": "error",
                   "message": "Could not parse a structured verdict from the model output."}
            return

        yield {"event": "status", "phase": "complete", "text": "Analysis complete"}
        yield {"event": "verdict", "data": verdict}
        yield {"event": "done"}

    async def _stream(self, user_prompt: str) -> AsyncGenerator[str, None]:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            # Mimic a legitimate browser client to avoid datacenter IP blocks.
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }
        body = {
            "model": self.model,
            "stream": True,
            "temperature": 0.3,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        }
        proxy = self.proxy or None
        async with httpx.AsyncClient(timeout=180, proxy=proxy) as client:
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                if resp.status_code != 200:
                    body_text = (await resp.aread()).decode(errors="replace")[:400]
                    raise RuntimeError(f"HTTP {resp.status_code}: {body_text}")
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    # Chain-of-thought reasoning tokens arrive in delta.reasoning
                    # Final answer tokens arrive in delta.content (standard OpenAI field)
                    reasoning = delta.get("reasoning")
                    content = delta.get("content")
                    if reasoning:
                        yield {"type": "reasoning", "text": reasoning}
                    if content:
                        yield {"type": "content", "text": content}


# ---------------------------------------------------------------------------

def gather_context(app_state, symbol: str) -> dict:
    """Pull a snapshot of every metric we have for `symbol`."""
    sym = symbol.upper()
    ctx: dict = {"symbol": sym, "ts": int(time.time() * 1000)}

    # 5m candles for the per-bar grain we predict on
    eng_5m = app_state.engines.get((sym.lower(), "5m"))
    if eng_5m and eng_5m.candles:
        recent = [c for c in list(eng_5m.candles) if c.observed][-30:]
        ctx["candles_5m"] = [
            {
                "ts": c.ts, "open": c.open, "high": c.high, "low": c.low,
                "close": c.close, "volume": c.volume,
                "delta": c.delta, "cvd": c.cvd,
            }
            for c in recent
        ]

    # Multi-timeframe trend snapshot
    trend = {}
    for tf in ("5m", "15m", "1h"):
        e = app_state.engines.get((sym.lower(), tf))
        if not (e and e.candles):
            continue
        recent = [c for c in list(e.candles) if c.observed][-10:]
        if len(recent) < 2:
            continue
        first, last = recent[0], recent[-1]
        trend[tf] = {
            "first_close": first.close,
            "last_close": last.close,
            "change_pct": (last.close - first.close) / first.close * 100 if first.close else 0,
            "cvd_change": last.cvd - first.cvd,
        }
    if trend:
        ctx["trend"] = trend

    # CVD divergences (already computed for snapshots)
    try:
        from divergence import detect_divergences
        if eng_5m:
            ctx["divergences"] = detect_divergences(eng_5m.snapshot()) or []
    except Exception:
        pass

    # Funding / OI
    fund = app_state.funding.get(sym)
    if fund:
        snap = fund.snapshot_history()
        cur = snap.get("current") or {}
        ctx["funding"] = {
            "rate": cur.get("rate"),
            "next_funding_time": cur.get("next_funding_time"),
            "signal": cur.get("signal"),
            "oi_value": cur.get("oi_value"),
            "oi_change_1h": cur.get("oi_change_1h"),
        }

    # Basis
    basis = app_state.basis.get(sym)
    if basis:
        ctx["basis"] = {
            "spot": basis.spot,
            "perp": basis.perp,
            "basis_usd": basis.basis,
            "basis_pct": basis.basis_pct,
            "state": basis.state,
        }

    # Taker
    taker = app_state.taker.get(sym)
    if taker:
        snap = taker.snapshot_current()
        ctx["taker"] = {
            "regime": snap.get("regime"),
            "alignment": snap.get("alignment"),
            "tfs": snap.get("tfs"),
            "divergence": snap.get("divergence"),
        }

    # GEX
    gex = app_state.gex.get(sym)
    if gex:
        snap = gex.snapshot()
        if snap:
            ctx["gex"] = {
                "state": snap.get("state"),
                "flip_zone": snap.get("flip_zone"),
                "net_gex": snap.get("net_gex"),
            }
    else:
        ctx["gex_unavailable"] = True

    return ctx
