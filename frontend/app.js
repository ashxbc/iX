const $ = (id) => document.getElementById(id);

// Backend host (without protocol). Falls back to whatever served this page —
// useful when the frontend is also served by FastAPI. Override via the
// <meta name="ix-backend"> tag in index.html when deploying to Vercel etc.
const BACKEND = (document.querySelector('meta[name="ix-backend"]')?.content || "").trim() || location.host;
const HTTP = location.protocol === "https:" ? "https" : "http";
const WS_PROTO = location.protocol === "https:" ? "wss" : "ws";

const state = {
  symbol: "BTCUSDT",
  timeframe: "5m",
  ws: null,
  priceChart: null,
  cvdChart: null,
  takerChart: null,
  candleSeries: null,
  cvdSeries: null,
  takerSeries: { "5m": null, "15m": null, "1h": null },
  takerBaseline: null,
  takerWs: null,
  lastDivergences: [],
  takerDivMarker: null,
  token: "",
};

async function verifyToken(token) {
  if (!token) return false;
  try {
    const r = await fetch(`${HTTP}://${BACKEND}/api/auth/check`, {
      headers: { "X-Auth-Token": token },
      cache: "no-store",
      credentials: "omit",
    });
    return r.ok;
  } catch {
    return false;
  }
}

function showError(msg) {
  $("auth-error").textContent = msg || "";
}

async function ensureAuth() {
  const overlay = $("auth-overlay");
  const hideOverlay = () => { if (overlay) overlay.style.display = "none"; };

  // If a token is already stored, trust it immediately and hide the overlay.
  // No network call on refresh. WS connections will close with 4401 if the
  // token is bad, and reconnect logic re-tries — never auto-logging out.
  const stored = localStorage.getItem("ix_token") || "";
  if (stored) {
    hideOverlay();
    return stored;
  }

  return new Promise((resolve) => {
    const form = $("auth-form");
    const input = $("auth-input");
    overlay.style.display = "flex";
    setTimeout(() => input.focus(), 50);
    form.onsubmit = async (e) => {
      e.preventDefault();
      const t = input.value.trim();
      showError("");
      if (!t) return;
      if (await verifyToken(t)) {
        localStorage.setItem("ix_token", t);
        hideOverlay();
        resolve(t);
      } else {
        showError("invalid token");
        input.value = "";
        input.focus();
      }
    };
  });
}

function logout() {
  localStorage.removeItem("ix_token");
  if (state.ws) { state.ws.onclose = null; state.ws.close(); }
  if (state.fundingWs) { state.fundingWs.onclose = null; state.fundingWs.close(); }
  if (state.liqWs) { state.liqWs.onclose = null; state.liqWs.close(); }
  if (state.basisWs) { state.basisWs.onclose = null; state.basisWs.close(); }
  if (state.liveLiqWs) { state.liveLiqWs.onclose = null; state.liveLiqWs.close(); }
  location.reload();
}

/* ---------- Funding / OI panel ---------- */

function fmtFunding(rate) {
  if (rate === null || rate === undefined || Number.isNaN(rate)) return "—";
  // Funding rate comes as a fraction (e.g. -0.0001 = -0.01%). Show as %.
  const pct = rate * 100;
  const sign = pct >= 0 ? "+" : "";
  return `${sign}${pct.toFixed(4)}%`;
}

function fmtOI(usd) {
  if (!usd) return "—";
  if (usd >= 1e9) return `$${(usd / 1e9).toFixed(2)}B`;
  if (usd >= 1e6) return `$${(usd / 1e6).toFixed(1)}M`;
  return `$${Math.round(usd).toLocaleString()}`;
}

function fmtCountdown(nextTs) {
  if (!nextTs) return "next in —";
  const diff = nextTs - Date.now();
  if (diff <= 0) return "settling…";
  const h = Math.floor(diff / 3_600_000);
  const m = Math.floor((diff % 3_600_000) / 60_000);
  return `next in ${h}h ${m.toString().padStart(2, "0")}m`;
}

function applyFunding(c) {
  if (!c) return;
  state.lastFunding = c;
  const fundEl = $("fund-val");
  fundEl.textContent = fmtFunding(c.funding_rate);
  fundEl.style.color =
    c.funding_rate > 0 ? "var(--up)" :
    c.funding_rate < 0 ? "var(--down)" : "var(--fg)";

  $("fund-countdown").textContent = fmtCountdown(c.next_funding_time);

  $("oi-val").textContent = fmtOI(c.oi_value);
  const chg = c.oi_change_1h || 0;
  const chgPct = (chg * 100).toFixed(2);
  const oiSub = $("oi-change");
  oiSub.textContent = `1h ${chg >= 0 ? "+" : ""}${chgPct}%`;
  oiSub.style.color = chg > 0 ? "var(--up)" : chg < 0 ? "var(--down)" : "var(--mute)";

  state.lastFundingSig = c.signal || "neutral";
  recomputeSignal();
}

/* ---------- Composite signal: funding + taker + GEX ---------- */

const SIG_LABEL = {
  explosive_long:  { text: "EXPLOSIVE LONG",   sub: "vol expansion + bull flow + shorts trapped" },
  explosive_short: { text: "EXPLOSIVE SHORT",  sub: "vol expansion + bear flow + longs trapped" },
  breakout_long:   { text: "BREAKOUT ↑",  sub: "buyers pressing pinned range — break imminent" },
  breakout_short:  { text: "BREAKOUT ↓",  sub: "sellers pressing pinned range — break imminent" },
  pinned:          { text: "PINNED",           sub: "vol suppressed in dealer zone" },
  explosive:       { text: "EXPLOSIVE VOL",    sub: "no directional confirmation yet" },
  squeeze:         { text: "SQUEEZE",          sub: "shorts trapped — bullish" },
  flush:           { text: "FLUSH",            sub: "longs trapped — bearish" },
  neutral:         { text: "NEUTRAL",          sub: "no setup" },
};

function recomputeSignal() {
  const f  = state.lastFundingSig || "neutral";
  const t  = state.lastTakerRegime || "neutral";
  const gs = state.lastGex?.state || "neutral";

  const bullStrong = t === "bull_strong";
  const bearStrong = t === "bear_strong";

  let sig;
  // Highest conviction first: vol regime + flow + funding all aligned.
  if (gs === "explosive" && bullStrong && f === "squeeze") sig = "explosive_long";
  else if (gs === "explosive" && bearStrong && f === "flush") sig = "explosive_short";
  // Pinning + strong directional taker = pressure building against the pin.
  else if (gs === "pinning" && bullStrong) sig = "breakout_long";
  else if (gs === "pinning" && bearStrong) sig = "breakout_short";
  // GEX regime alone (no taker / funding confirmation).
  else if (gs === "pinning")   sig = "pinned";
  else if (gs === "explosive") sig = "explosive";
  // Fall back to funding-only signals when GEX is neutral.
  else if (f === "squeeze") sig = "squeeze";
  else if (f === "flush")   sig = "flush";
  else                       sig = "neutral";

  const lbl = SIG_LABEL[sig];
  const sigBox = $("funding-signal");
  if (sigBox && sigBox.dataset.state !== sig) sigBox.dataset.state = sig;
  const txtEl = $("signal-text");
  const subEl = $("signal-sub");
  if (txtEl) txtEl.textContent = lbl.text;
  if (subEl) subEl.textContent = lbl.sub;
}

/* ---------- Options Gamma Exposure (GEX) ---------- */

function applyGex(snap) {
  if (!snap) return;
  state.lastGex = snap;
  const stat = $("gex-stat");
  if (stat) stat.dataset.state = snap.state || "neutral";
  const valEl = $("gex-val");
  const subEl = $("gex-sub");
  if (valEl) {
    valEl.textContent =
      snap.state === "pinning"   ? "PINNING"   :
      snap.state === "explosive" ? "EXPLOSIVE" : "NEUTRAL";
  }
  if (subEl) {
    subEl.textContent = snap.flip_zone
      ? `flip $${(snap.flip_zone / 1000).toFixed(1)}k`
      : "flip —";
  }
  recomputeSignal();
}

function connectGex() {
  if (state.gexWs) {
    state.gexWs.onclose = null;
    state.gexWs.close();
  }
  const url = `${WS_PROTO}://${BACKEND}/ws/gex?token=${encodeURIComponent(state.token)}`;
  const ws = new WebSocket(url);
  state.gexWs = ws;
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.event === "snapshot" || msg.event === "tick") applyGex(msg.data);
  };
  ws.onclose = () => setTimeout(connectGex, 5000);
}

let fundingCountdownTimer = null;

function connectFunding() {
  if (state.fundingWs) {
    state.fundingWs.onclose = null;
    state.fundingWs.close();
  }
  const url = `${WS_PROTO}://${BACKEND}/ws/funding?token=${encodeURIComponent(state.token)}`;
  const ws = new WebSocket(url);
  state.fundingWs = ws;
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.event === "snapshot") applyFunding(msg.data.current);
    else if (msg.event === "tick") applyFunding(msg.data);
  };
  ws.onclose = () => setTimeout(connectFunding, 2000);

  // Refresh the countdown text once a minute (the rate itself updates every 30s via WS).
  if (fundingCountdownTimer) clearInterval(fundingCountdownTimer);
  fundingCountdownTimer = setInterval(() => {
    const txt = $("fund-countdown").textContent;
    if (!txt.startsWith("next in")) return;
    // we don't have the timestamp here; re-apply via cached state
    if (state.lastFunding) $("fund-countdown").textContent = fmtCountdown(state.lastFunding.next_funding_time);
  }, 30_000);
}

const chartOpts = {
  layout: { background: { color: "#0a0a0a" }, textColor: "#999" },
  grid: { vertLines: { color: "#141414" }, horzLines: { color: "#141414" } },
  // Fixed minimum width on the right scale so the time-axis pixel grid is
  // identical across all panes — without this, panes drift sideways when
  // axis labels differ in width.
  rightPriceScale: { borderColor: "#1c1c1c", minimumWidth: 64 },
  timeScale: {
    borderColor: "#1c1c1c",
    timeVisible: true,
    secondsVisible: false,
    shiftVisibleRangeOnNewBar: false, // don't yank the user when a new bar arrives
    rightOffset: 4,
  },
  crosshair: { mode: 1 },
};

function buildCharts() {
  if (state.priceChart) state.priceChart.remove();
  if (state.cvdChart) state.cvdChart.remove();
  if (state.takerChart) state.takerChart.remove();

  state.priceChart = LightweightCharts.createChart($("price"), chartOpts);
  state.candleSeries = state.priceChart.addCandlestickSeries({
    upColor: "#26a69a", downColor: "#ef5350",
    borderUpColor: "#26a69a", borderDownColor: "#ef5350",
    wickUpColor: "#26a69a", wickDownColor: "#ef5350",
  });

  state.cvdChart = LightweightCharts.createChart($("cvd-pane"), {
    ...chartOpts,
    timeScale: { ...chartOpts.timeScale, visible: false },
  });
  state.cvdSeries = state.cvdChart.addLineSeries({
    color: "#d4d4d4", lineWidth: 2, priceLineVisible: false,
  });

  // Basis histogram in the CVD pane — same time axis, separate overlay
  // scale anchored to the bottom 28% so it never crowds the CVD line.
  state.basisSeries = state.cvdChart.addHistogramSeries({
    priceScaleId: "basis",
    priceFormat: { type: "price", precision: 4, minMove: 0.0001 },
    base: 0,
    lastValueVisible: false,
    priceLineVisible: false,
  });
  state.cvdChart.priceScale("basis").applyOptions({
    scaleMargins: { top: 0.75, bottom: 0 },
    visible: false,
  });
  // Keep CVD line in the top 75% so it never overlaps the basis bars.
  state.cvdChart.priceScale("right").applyOptions({
    scaleMargins: { top: 0.05, bottom: 0.28 },
  });

  // Taker pane — 3 lines (5m / 15m / 1h ratio) + 0.5 baseline
  state.takerChart = LightweightCharts.createChart($("taker-pane"), {
    ...chartOpts,
    timeScale: { ...chartOpts.timeScale, visible: false },
  });
  state.takerSeries["5m"]  = state.takerChart.addLineSeries({
    color: "#26a69a", lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
  });
  state.takerSeries["15m"] = state.takerChart.addLineSeries({
    color: "#f5b942", lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
  });
  state.takerSeries["1h"]  = state.takerChart.addLineSeries({
    color: "#c97cf4", lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
  });
  // 0.5 = neutral baseline. Above = buyers aggressive, below = sellers aggressive.
  state.takerSeries["5m"].createPriceLine({
    price: 0.5, color: "#3a3a3a", lineStyle: 0, lineWidth: 1, axisLabelVisible: true, title: "0.5",
  });
  state.takerChart.priceScale("right").applyOptions({
    scaleMargins: { top: 0.15, bottom: 0.15 },
    autoScale: true,
  });

  // Hidden anchor series on the taker pane: an invisible line populated with
  // every price-candle timestamp. This forces the taker chart's time grid to
  // match the price chart's logical bar index exactly — without it, sparse
  // 1h bars would make logical-range sync blow up the scale.
  state.takerAnchorSeries = state.takerChart.addLineSeries({
    color: "rgba(0,0,0,0)",
    priceScaleId: "anchor",
    lastValueVisible: false,
    priceLineVisible: false,
    crosshairMarkerVisible: false,
  });
  state.takerChart.priceScale("anchor").applyOptions({ visible: false });

  // Sync time scales between panes by LOGICAL bar index. Works because every
  // pane shares the same time grid (price candles → cvd whitespace fill,
  // taker anchor series). Guarded to prevent feedback loops.
  const charts = [state.priceChart, state.cvdChart, state.takerChart];
  let syncing = false;
  const broadcast = (src) => src.timeScale().subscribeVisibleLogicalRangeChange((r) => {
    if (!r || syncing) return;
    syncing = true;
    try {
      for (const c of charts) {
        if (c !== src) c.timeScale().setVisibleLogicalRange(r);
      }
    } finally { syncing = false; }
  });
  charts.forEach(broadcast);

  const onResize = () => {
    state.priceChart.applyOptions({ width: $("price").clientWidth, height: $("price").clientHeight });
    state.cvdChart.applyOptions({ width: $("cvd-pane").clientWidth, height: $("cvd-pane").clientHeight });
    state.takerChart.applyOptions({ width: $("taker-pane").clientWidth, height: $("taker-pane").clientHeight });
    resizeLiqCanvas();
    drawHeatmap();
  };
  ensureLiqCanvas();
  onResize();
  window.addEventListener("resize", onResize);

  // Repaint the heatmap whenever the price chart's visible price range changes
  // (zoom, pan, autoscale). Lightweight Charts doesn't expose a single event
  // for price-range changes, so we hook the time scale + a rAF loop guard.
  state.priceChart.timeScale().subscribeVisibleTimeRangeChange(() => drawHeatmap());
  state.candleSeries.subscribeDataChanged?.(() => drawHeatmap());
}

function setStatus(s, cls) {
  const el = $("status");
  el.textContent = s;
  el.className = "status " + (cls || "");
}

function fmt(n, d = 2) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return Number(n).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
}

function applySnapshot(candles, divergences) {
  if (candles.length) state.lastPrice = candles[candles.length - 1].close;
  const cs = candles.map((c) => ({
    time: Math.floor(c.ts / 1000),
    open: c.open, high: c.high, low: c.low, close: c.close,
  }));
  // Same time index as price for perfect alignment. Unobserved bars become
  // WhitespaceData ({ time }) so the CVD line breaks across gaps but the time
  // scale stays in sync with the price pane.
  const ds = candles.map((c) => {
    const time = Math.floor(c.ts / 1000);
    return c.observed ? { time, value: c.cvd } : { time };
  });
  state.candleSeries.setData(cs);
  state.cvdSeries.setData(ds);
  // Anchor the taker pane to the same time grid so logical-range sync works.
  if (state.takerAnchorSeries) {
    state.takerAnchorSeries.setData(cs.map((c) => ({ time: c.time })));
  }
  applyDivergences(divergences || []);
}

function applyDivergences(divs) {
  const markers = divs.map((d) => ({
    time: Math.floor(d.ts / 1000),
    position: d.type === "bearish" ? "aboveBar" : "belowBar",
    color: d.type === "bearish" ? "#ef5350" : "#26a69a",
    shape: d.type === "bearish" ? "arrowDown" : "arrowUp",
    text: d.type === "bearish" ? "BEAR DIV" : "BULL DIV",
  }));
  state.candleSeries.setMarkers(markers);
}

function applyTick(c) {
  const t = Math.floor(c.ts / 1000);
  state.candleSeries.update({ time: t, open: c.open, high: c.high, low: c.low, close: c.close });
  // Keep CVD time index aligned with price: emit whitespace when not observed,
  // a real value once a trade has been seen.
  state.cvdSeries.update(c.observed ? { time: t, value: c.cvd } : { time: t });
  // Extend the taker pane's time grid in lockstep with price.
  if (state.takerAnchorSeries) state.takerAnchorSeries.update({ time: t });
  state.lastPrice = c.close;
  $("px").textContent = fmt(c.close);
  $("cvd").textContent = fmt(c.cvd, 0);
  const d = c.delta;
  const dEl = $("delta");
  dEl.textContent = (d >= 0 ? "+" : "") + fmt(d, 0);
  dEl.style.color = d >= 0 ? "var(--up)" : "var(--down)";
}

function connect() {
  if (state.ws) {
    state.ws.onclose = null;
    state.ws.close();
  }
  setStatus("connecting");
  const url = `${WS_PROTO}://${BACKEND}/ws/${state.symbol}/${state.timeframe}?token=${encodeURIComponent(state.token)}`;
  const ws = new WebSocket(url);
  state.ws = ws;
  ws.onopen = () => setStatus("live", "live");
  ws.onclose = () => {
    setStatus("disconnected", "dead");
    setTimeout(connect, 1500);
  };
  ws.onerror = () => setStatus("error", "dead");
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.event === "snapshot") applySnapshot(msg.data.candles, msg.data.divergences);
    else if (msg.event === "tick" || msg.event === "candle") applyTick(msg.data);
    else if (msg.event === "divergences") applyDivergences(msg.data);
  };
}

/* ---------- Spot vs Perp basis ---------- */

function fmtBasisUsd(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const sign = v >= 0 ? "+" : "−";
  const abs = Math.abs(v);
  return `${sign}$${abs.toFixed(2)}`;
}

function fmtBasisPct(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const sign = v >= 0 ? "+" : "−";
  return `${sign}${Math.abs(v).toFixed(4)}%`;
}

function applyBasis(c) {
  if (!c) return;
  state.lastBasis = c;
  const valEl = $("basis-val");
  valEl.textContent = `${fmtBasisUsd(c.basis)} (${fmtBasisPct(c.basis_pct)})`;
  valEl.style.color =
    c.basis > 0 ? "var(--up)" :
    c.basis < 0 ? "var(--down)" : "var(--fg)";
  const sub = $("basis-sub");
  const leader =
    c.state === "spot_led" ? "spot leading — real demand" :
    c.state === "perp_led" ? "perp leading — leverage driven" :
                              "balanced";
  sub.textContent = leader;
  sub.style.color =
    c.state === "spot_led" ? "var(--up)" :
    c.state === "perp_led" ? "var(--down)" : "var(--mute)";
}

function setBasisSeriesData(history) {
  if (!state.basisSeries || !history) return;
  // Histogram data sorted by time, colored by sign. Use basis_pct so the
  // overlay scales sensibly across different price regimes.
  const seen = new Set();
  const data = [];
  for (const p of history) {
    const t = Math.floor(p.ts);
    if (seen.has(t)) continue; // dedupe identical timestamps
    seen.add(t);
    data.push({
      time: t,
      value: p.basis_pct,
      color: p.basis_pct >= 0
        ? "rgba(38, 166, 154, 0.55)"
        : "rgba(239, 83, 80, 0.55)",
    });
  }
  data.sort((a, b) => a.time - b.time);
  state.basisSeries.setData(data);
}

function pushBasisSample(sample) {
  if (!state.basisSeries || !sample) return;
  state.basisSeries.update({
    time: Math.floor(sample.ts),
    value: sample.basis_pct,
    color: sample.basis_pct >= 0
      ? "rgba(38, 166, 154, 0.55)"
      : "rgba(239, 83, 80, 0.55)",
  });
}

function connectBasis() {
  if (state.basisWs) {
    state.basisWs.onclose = null;
    state.basisWs.close();
  }
  const url = `${WS_PROTO}://${BACKEND}/ws/basis?token=${encodeURIComponent(state.token)}`;
  const ws = new WebSocket(url);
  state.basisWs = ws;
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.event === "snapshot") {
      applyBasis(msg.data.current);
      setBasisSeriesData(msg.data.history || []);
    } else if (msg.event === "tick") {
      applyBasis(msg.data);
    } else if (msg.event === "sample") {
      pushBasisSample(msg.data);
    }
  };
  ws.onclose = () => setTimeout(connectBasis, 2000);
}

/* ---------- Taker buy/sell ratio ---------- */

const REGIME_LABEL = {
  bull_strong: { text: "STRONG BULL FLOW", sub: "all timeframes aggressive buys" },
  bull:        { text: "BULL FLOW",         sub: "buyers aggressive across TFs" },
  bear_strong: { text: "STRONG BEAR FLOW", sub: "all timeframes aggressive sells" },
  bear:        { text: "BEAR FLOW",         sub: "sellers aggressive across TFs" },
  diverging:   { text: "DIVERGING",         sub: "timeframes disagree — fade or wait" },
  neutral:     { text: "NEUTRAL",           sub: "balanced flow" },
};

function fmtTaker(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return Number(v).toFixed(3);
}

function applyTakerLegend(snap) {
  if (!snap || !snap.tfs) return;
  for (const tf of ["5m", "15m", "1h"]) {
    const t = snap.tfs[tf];
    const el = $(`taker-${tf}`);
    if (!el || !t) continue;
    const v = t.ema ?? t.ratio;
    el.textContent = fmtTaker(v);
    el.style.color =
      v == null ? "var(--fg)" :
      v >= 0.5  ? "var(--up)" : "var(--down)";
    // Real-time line update — paint the in-progress bar's current ratio so the
    // line extends with each kline tick, not just on bar close.
    if (state.takerSeries[tf] && t.ts != null && t.ratio != null) {
      state.takerSeries[tf].update({
        time: Math.floor(t.ts / 1000),
        value: t.ratio,
      });
    }
  }
  const reg = snap.regime || "neutral";
  state.lastTakerRegime = reg;
  recomputeSignal();
  const lbl = REGIME_LABEL[reg] || REGIME_LABEL.neutral;
  const wrap = $("taker-regime");
  wrap.dataset.regime = reg;
  $("taker-regime-text").textContent = lbl.text;
  // If a divergence is active, override the sub line — it's the real signal.
  if (snap.divergence) {
    $("taker-regime-sub").textContent =
      (snap.divergence.type === "bullish" ? "BULL DIV" : "BEAR DIV") +
      " · " + snap.divergence.note;
  } else {
    $("taker-regime-sub").textContent = lbl.sub;
  }
}

function takerSeriesData(history) {
  // history per TF -> array of {ts, ratio} -> {time, value} dedup + sorted
  const out = {};
  for (const tf of ["5m", "15m", "1h"]) {
    const seen = new Set();
    const data = [];
    for (const b of (history?.[tf] || [])) {
      const t = Math.floor(b.ts / 1000);
      if (seen.has(t)) continue;
      seen.add(t);
      data.push({ time: t, value: b.ratio });
    }
    data.sort((a, b) => a.time - b.time);
    out[tf] = data;
  }
  return out;
}

function applyTakerSnapshot(snap) {
  if (!snap) return;
  const data = takerSeriesData(snap.history || {});
  for (const tf of ["5m", "15m", "1h"]) {
    if (state.takerSeries[tf]) state.takerSeries[tf].setData(data[tf]);
  }
  applyTakerLegend(snap.current);
}

function applyTakerBar(payload) {
  // payload: { tf, ts, ratio, ema, qv, close }
  const s = state.takerSeries[payload.tf];
  if (!s) return;
  s.update({ time: Math.floor(payload.ts / 1000), value: payload.ratio });
}

function connectTaker() {
  if (state.takerWs) {
    state.takerWs.onclose = null;
    state.takerWs.close();
  }
  const url = `${WS_PROTO}://${BACKEND}/ws/taker?token=${encodeURIComponent(state.token)}`;
  const ws = new WebSocket(url);
  state.takerWs = ws;
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.event === "snapshot") {
      applyTakerSnapshot(msg.data);
    } else if (msg.event === "bar") {
      applyTakerBar(msg.data);
    } else if (msg.event === "tick") {
      applyTakerLegend(msg.data);
    }
  };
  ws.onclose = () => setTimeout(connectTaker, 2000);
}

/* ---------- Live liquidation flash labels ---------- */

function showLiqFlash(ev) {
  if (!state.candleSeries) return;
  const pane = $("price");
  const y = state.candleSeries.priceToCoordinate(ev.price);
  if (y == null || isNaN(y)) return;

  const w = pane.clientWidth;
  const h = pane.clientHeight;

  const el = document.createElement("div");
  el.className = "liq-flash " + (ev.side === "long" ? "long" : "short");
  const arrow = ev.side === "long" ? "▼" : "▲";
  const lbl = ev.side === "long" ? "LONG REKT" : "SHORT REKT";
  el.textContent = `${arrow} ${fmtUsd(ev.usd)} ${lbl}`;
  pane.appendChild(el);

  // Place near right edge but not over the price scale; offset on overlap
  const dpr = state.lastPrice && ev.price > state.lastPrice ? -22 : 12;
  let top = Math.round(y + dpr);
  // De-overlap with recent labels at similar Y
  const near = (state._liqFlashes || []).filter(f => Math.abs(f.top - top) < 22);
  if (near.length) top += near.length * 22 * (ev.side === "long" ? 1 : -1);
  // Clamp inside pane
  if (top < 4) top = 4;
  if (top > h - 24) top = h - 24;

  const lw = el.offsetWidth || 140;
  let left = w - lw - 14;
  if (left < 8) left = 8;
  el.style.left = left + "px";
  el.style.top = top + "px";

  state._liqFlashes = state._liqFlashes || [];
  const entry = { el, top, until: Date.now() + 2400 };
  state._liqFlashes.push(entry);

  setTimeout(() => {
    el.remove();
    state._liqFlashes = state._liqFlashes.filter(f => f !== entry);
  }, 2500);
}

function connectLiveLiq() {
  if (state.liveLiqWs) {
    state.liveLiqWs.onclose = null;
    state.liveLiqWs.close();
  }
  const url = `${WS_PROTO}://${BACKEND}/ws/live-liq?token=${encodeURIComponent(state.token)}`;
  const ws = new WebSocket(url);
  state.liveLiqWs = ws;
  ws.onmessage = (m) => {
    try {
      const msg = JSON.parse(m.data);
      if (msg.event === "liq") showLiqFlash(msg.data);
    } catch {}
  };
  ws.onclose = () => {
    state.liveLiqWs = null;
    setTimeout(connectLiveLiq, 3000);
  };
  ws.onerror = () => {};
}

/* ---------- Liquidation heatmap ---------- */

function ensureLiqCanvas() {
  if (state.liqCanvas) return state.liqCanvas;
  const pane = $("price");
  const c = document.createElement("canvas");
  c.className = "liq-canvas";
  if (!state.liqVisible) c.classList.add("hidden");
  pane.appendChild(c);
  state.liqCanvas = c;

  const tip = document.createElement("div");
  tip.className = "liq-tooltip";
  pane.appendChild(tip);
  state.liqTooltip = tip;

  pane.addEventListener("mousemove", onLiqHover);
  pane.addEventListener("mouseleave", hideLiqTooltip);

  resizeLiqCanvas();
  return c;
}

function fmtUsd(v) {
  if (!v) return "$0";
  if (v >= 1e9) return `$${(v / 1e9).toFixed(2)}B`;
  if (v >= 1e6) return `$${(v / 1e6).toFixed(2)}M`;
  if (v >= 1e3) return `$${(v / 1e3).toFixed(1)}K`;
  return `$${Math.round(v).toLocaleString()}`;
}

function hideLiqTooltip() {
  if (state.liqTooltip) state.liqTooltip.classList.remove("show");
}

function onLiqHover(e) {
  const tip = state.liqTooltip;
  if (!tip) return;
  if (!state.liqVisible || !state.liqHeatmap || !state.candleSeries) return hideLiqTooltip();
  const data = state.liqHeatmap;
  if (!data.buckets || data.buckets.length === 0) return hideLiqTooltip();

  const pane = $("price");
  const rect = pane.getBoundingClientRect();
  const x = e.clientX - rect.left;
  const y = e.clientY - rect.top;

  const price = state.candleSeries.coordinateToPrice(y);
  if (price == null || isNaN(price)) return hideLiqTooltip();

  const bs = data.bucket_size;
  // Closest bucket whose [price - bs/2, price + bs/2] contains hovered price
  let bucket = null;
  let bestDist = Infinity;
  for (const b of data.buckets) {
    const d = Math.abs(b.price - price);
    if (d <= bs / 2 && d < bestDist) { bestDist = d; bucket = b; }
  }
  if (!bucket) return hideLiqTooltip();

  const total = bucket.long_usd + bucket.short_usd;
  const priceLo = bucket.price - bs / 2;
  const priceHi = bucket.price + bs / 2;
  tip.innerHTML = `
    <div class="row"><span class="lbl">level</span><span>${fmt(priceLo, 0)} – ${fmt(priceHi, 0)}</span></div>
    <div class="row"><span class="lbl">longs</span><span class="long">${fmtUsd(bucket.long_usd)}</span></div>
    <div class="row"><span class="lbl">shorts</span><span class="short">${fmtUsd(bucket.short_usd)}</span></div>
    <div class="row"><span class="lbl">total</span><span>${fmtUsd(total)}</span></div>
  `;
  tip.classList.add("show");

  // Position near cursor, clamped to pane
  let lx = x + 14, ly = y + 14;
  const lw = tip.offsetWidth, lh = tip.offsetHeight;
  if (lx + lw > rect.width - 4) lx = x - lw - 14;
  if (ly + lh > rect.height - 4) ly = y - lh - 14;
  if (lx < 4) lx = 4;
  if (ly < 4) ly = 4;
  tip.style.left = lx + "px";
  tip.style.top = ly + "px";
}

function resizeLiqCanvas() {
  const c = state.liqCanvas;
  if (!c) return;
  const pane = $("price");
  const w = pane.clientWidth;
  const h = pane.clientHeight;
  const dpr = window.devicePixelRatio || 1;
  c.width = Math.floor(w * dpr);
  c.height = Math.floor(h * dpr);
  c.style.width = w + "px";
  c.style.height = h + "px";
  const ctx = c.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

let _heatmapRafPending = false;
function drawHeatmap() {
  if (_heatmapRafPending) return;
  _heatmapRafPending = true;
  requestAnimationFrame(() => {
    _heatmapRafPending = false;
    _drawHeatmap();
  });
}

function _drawHeatmap() {
  const c = state.liqCanvas;
  if (!c || !state.liqVisible) return;
  const ctx = c.getContext("2d");
  const w = c.clientWidth;
  const h = c.clientHeight;
  ctx.clearRect(0, 0, w, h);

  const data = state.liqHeatmap;
  if (!data || !data.buckets || data.buckets.length === 0 || !data.max_usd) return;
  if (!state.candleSeries) return;

  const bucketSize = data.bucket_size;
  const maxBarPx = Math.max(40, Math.floor(w * 0.32));
  const lastClose = state.lastPrice ?? null;

  // Pre-compute coordinates and skip buckets outside the visible price range.
  const visibleTop = state.candleSeries.coordinateToPrice(0);
  const visibleBot = state.candleSeries.coordinateToPrice(h);
  const pMin = Math.min(visibleTop ?? Infinity, visibleBot ?? Infinity);
  const pMax = Math.max(visibleTop ?? -Infinity, visibleBot ?? -Infinity);

  for (const b of data.buckets) {
    if (b.price + bucketSize / 2 < pMin || b.price - bucketSize / 2 > pMax) continue;

    const yTop = state.candleSeries.coordinateToPrice ? state.candleSeries.priceToCoordinate?.(b.price + bucketSize / 2) : null;
    const yBot = state.candleSeries.priceToCoordinate?.(b.price - bucketSize / 2);
    const yCenter = state.candleSeries.priceToCoordinate?.(b.price);
    if (yCenter == null || isNaN(yCenter)) continue;

    let barH = (yBot != null && yTop != null && !isNaN(yTop) && !isNaN(yBot))
      ? Math.max(1, Math.abs(yBot - yTop) - 1)
      : 2;
    barH = Math.min(barH, 30);

    const total = b.long_usd + b.short_usd;
    const intensity = total / data.max_usd;          // 0..1
    const barW = Math.max(2, Math.floor(intensity * maxBarPx));

    // Color by side dominance, with current price as the divider hint.
    // Long liquidations are most relevant below price (teal).
    // Short liquidations are most relevant above price (red).
    const longDominant = b.long_usd >= b.short_usd;
    const baseColor = longDominant ? [38, 166, 154] : [239, 83, 80];
    const alpha = 0.18 + intensity * 0.55;           // 0.18..0.73

    const x = w - barW - 2;
    const y = Math.round(yCenter - barH / 2);

    ctx.fillStyle = `rgba(${baseColor[0]}, ${baseColor[1]}, ${baseColor[2]}, ${alpha})`;
    ctx.fillRect(x, y, barW, barH);

    // Thin minority sliver in the opposite color, if both sides present
    const minority = longDominant ? b.short_usd : b.long_usd;
    if (minority > 0 && total > 0) {
      const minColor = longDominant ? [239, 83, 80] : [38, 166, 154];
      const minW = Math.max(1, Math.floor((minority / total) * barW));
      ctx.fillStyle = `rgba(${minColor[0]}, ${minColor[1]}, ${minColor[2]}, ${alpha})`;
      ctx.fillRect(x, y, minW, barH);
    }

    // Bright leading edge for the bar (so cluster magnitude reads instantly)
    ctx.fillStyle = `rgba(${baseColor[0]}, ${baseColor[1]}, ${baseColor[2]}, ${Math.min(1, alpha + 0.25)})`;
    ctx.fillRect(x, y, 1, barH);
  }

  // Optional: dim cue line at the data window's right edge
  if (lastClose != null) {
    const ly = state.candleSeries.priceToCoordinate?.(lastClose);
    if (ly != null && !isNaN(ly)) {
      ctx.strokeStyle = "rgba(255,255,255,0.04)";
      ctx.beginPath(); ctx.moveTo(0, ly); ctx.lineTo(w, ly); ctx.stroke();
    }
  }
}

function setLiqVisible(on) {
  state.liqVisible = !!on;
  localStorage.setItem("ix_liq_on", on ? "1" : "0");
  const btn = $("liq-toggle");
  if (btn) btn.setAttribute("aria-pressed", on ? "true" : "false");
  const c = state.liqCanvas;
  if (c) c.classList.toggle("hidden", !on);
  if (!on) hideLiqTooltip();
  if (on) {
    if (!state.liqWs) connectLiquidations();
    drawHeatmap();
  }
}

function connectLiquidations() {
  if (state.liqWs) {
    state.liqWs.onclose = null;
    state.liqWs.close();
  }
  const url = `${WS_PROTO}://${BACKEND}/ws/liquidations?token=${encodeURIComponent(state.token)}`;
  const ws = new WebSocket(url);
  state.liqWs = ws;
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.event === "snapshot") {
      state.liqHeatmap = msg.data;
      drawHeatmap();
    }
  };
  ws.onclose = () => {
    state.liqWs = null;
    if (state.liqVisible) setTimeout(connectLiquidations, 5000);
  };
  ws.onerror = () => {};
}

/* ============================================================
   Paper trading
   ============================================================ */

function getPaperUid() {
  let uid = localStorage.getItem("ix_paper_uid");
  if (uid && uid.length >= 8) return uid;
  // crypto.randomUUID() yields a 36-char dashed UUID — passes our regex.
  uid = (crypto.randomUUID && crypto.randomUUID()) ||
        (Date.now().toString(36) + Math.random().toString(36).slice(2, 14));
  localStorage.setItem("ix_paper_uid", uid);
  return uid;
}

const paper = {
  uid: null,
  ws: null,
  side: "long",
  size: 100,
  leverage: 10,
  account: null,         // last snapshot from server
  history: [],           // closed trades (and open, but rendered separately)
  lines: {},             // tradeId -> [entryLine, liqLine]
  lastBalance: null,     // for color flash on change
};

function fmtUsdSigned(v) {
  if (v == null || Number.isNaN(v)) return "—";
  const s = v >= 0 ? "+" : "−";
  return `${s}$${Math.abs(v).toFixed(2)}`;
}
function fmtUsd2(v) {
  if (v == null || Number.isNaN(v)) return "—";
  return `$${v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
function fmtPriceK(p) {
  if (!p) return "—";
  return `$${(p / 1000).toFixed(2)}k`;
}

function paperHeaders() {
  return { "X-Auth-Token": state.token, "Content-Type": "application/json" };
}

async function paperPost(path, body) {
  const r = await fetch(`${HTTP}://${BACKEND}${path}`, {
    method: "POST",
    headers: paperHeaders(),
    body: JSON.stringify(body),
    credentials: "omit",
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `error ${r.status}`);
  return data;
}

async function paperGet(path) {
  const r = await fetch(`${HTTP}://${BACKEND}${path}`, {
    headers: paperHeaders(), cache: "no-store", credentials: "omit",
  });
  if (!r.ok) throw new Error(`error ${r.status}`);
  return r.json();
}

function setBalance(v) {
  const el = $("balance");
  if (!el) return;
  el.textContent = fmtUsd2(v);
  if (paper.lastBalance != null && Math.abs(v - paper.lastBalance) > 0.01) {
    el.classList.remove("up", "down");
    void el.offsetWidth;  // restart transition
    el.classList.add(v >= paper.lastBalance ? "up" : "down");
    setTimeout(() => el.classList.remove("up", "down"), 1500);
  }
  paper.lastBalance = v;
}

function liqPriceFor(side, mark, lev) {
  const mm = 0.005;
  if (!mark || !lev) return null;
  return side === "long"
    ? mark * (1 - 1 / lev + mm)
    : mark * (1 + 1 / lev - mm);
}

function updateTradePreview() {
  const size = Math.max(0, Number($("trade-size").value || 0));
  const lev  = Number($("trade-leverage").value || 1);
  const mark = paper.account?.mark || state.lastPrice || 0;
  const margin = lev > 0 ? size / lev : 0;
  $("lev-display").textContent = `${lev}x`;
  $("trade-margin").textContent = fmtUsd2(margin);
  $("trade-notional").textContent = fmtUsd2(size);
  $("trade-mark").textContent = mark ? fmtUsd2(mark) : "—";
  const liq = liqPriceFor(paper.side, mark, lev);
  $("trade-liq").textContent = liq ? fmtUsd2(liq) : "—";
  const btn = $("open-trade-btn");
  btn.classList.toggle("short", paper.side === "short");
  btn.textContent = `OPEN ${paper.side.toUpperCase()}`;
}

function setSide(side) {
  paper.side = side;
  document.querySelectorAll(".side-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.side === side);
  });
  updateTradePreview();
}

/* ----- chart price lines per open trade ----- */

function addTradeLines(t) {
  if (!state.candleSeries) return;
  if (paper.lines[t.id]) return;  // already drawn
  const sideColor = t.side === "long" ? "#26a69a" : "#ef5350";
  const entry = state.candleSeries.createPriceLine({
    price: t.entry_price,
    color: sideColor,
    lineWidth: 2,
    lineStyle: 0,
    axisLabelVisible: true,
    title: `${t.side === "long" ? "L" : "S"}${t.leverage}x #${t.id}`,
  });
  const liq = state.candleSeries.createPriceLine({
    price: t.liq_price,
    color: "#ef5350",
    lineWidth: 1,
    lineStyle: 2,    // dashed
    axisLabelVisible: true,
    title: `LIQ #${t.id}`,
  });
  paper.lines[t.id] = [entry, liq];
}

function removeTradeLines(tradeId) {
  const lines = paper.lines[tradeId];
  if (!lines || !state.candleSeries) return;
  lines.forEach((l) => {
    try { state.candleSeries.removePriceLine(l); } catch {}
  });
  delete paper.lines[tradeId];
}

function syncTradeLines(openTrades) {
  const wanted = new Set(openTrades.map((t) => t.id));
  // Remove lines for trades no longer open
  for (const id of Object.keys(paper.lines)) {
    if (!wanted.has(Number(id))) removeTradeLines(Number(id));
  }
  // Add lines for new ones
  for (const t of openTrades) addTradeLines(t);
}

/* ----- rendering ----- */

function applyPaperAccount(snap) {
  if (!snap) return;
  paper.account = snap;
  setBalance(snap.balance);
  // header badge for open count
  const cnt = snap.open_trades?.length || 0;
  const badge = $("open-pos-count");
  if (badge) {
    if (cnt > 0) { badge.hidden = false; badge.textContent = cnt; }
    else { badge.hidden = true; }
  }
  // modal equity strip
  $("modal-balance").textContent = fmtUsd2(snap.balance);
  $("modal-equity").textContent  = fmtUsd2(snap.equity);
  const unr = $("modal-unrealized");
  unr.textContent = fmtUsdSigned(snap.unrealized);
  unr.classList.toggle("up", snap.unrealized > 0);
  unr.classList.toggle("down", snap.unrealized < 0);
  // chart lines
  syncTradeLines(snap.open_trades || []);
  // active positions table
  renderActiveTrades(snap.open_trades || []);
  // refresh preview liq with fresh mark
  updateTradePreview();
  $("active-count").textContent = `(${cnt})`;
}

function renderActiveTrades(opens) {
  const wrap = $("active-trades");
  if (!wrap) return;
  wrap.innerHTML = "";
  for (const t of opens) {
    const row = document.createElement("div");
    row.className = "trade-row";
    const pnlCls = t.unrealized_pnl >= 0 ? "up" : "down";
    const pnlPct = (t.unrealized_pnl / t.margin) * 100;
    row.innerHTML = `
      <div class="col-side ${t.side}">${t.side.toUpperCase()}</div>
      <div class="col-lev">${t.leverage}x</div>
      <div>${fmtUsd2(t.entry_price)}</div>
      <div class="col-pnl ${pnlCls}">${fmtUsdSigned(t.unrealized_pnl)} <span class="col-lev">(${pnlPct >= 0 ? "+" : ""}${pnlPct.toFixed(1)}%)</span></div>
      <div class="col-meta">
        <span>size ${fmtUsd2(t.size_usd)}</span>
        <span>margin ${fmtUsd2(t.margin)}</span>
        <span>liq ${fmtUsd2(t.liq_price)}</span>
      </div>
      <button class="close-btn" data-close="${t.id}">close</button>
    `;
    wrap.appendChild(row);
  }
  wrap.querySelectorAll("[data-close]").forEach((b) => {
    b.onclick = () => closeTrade(Number(b.dataset.close));
  });
}

function renderHistory(trades) {
  const wrap = $("trade-history");
  if (!wrap) return;
  // Only show closed/liquidated/cancelled
  const past = trades.filter((t) => t.status !== "open").slice(0, 50);
  wrap.innerHTML = "";
  for (const t of past) {
    const row = document.createElement("div");
    row.className = "trade-row compact";
    const pnlCls = (t.pnl_usd || 0) >= 0 ? "up" : "down";
    const date = t.close_ts ? new Date(t.close_ts).toLocaleString() : "—";
    row.innerHTML = `
      <div class="col-side ${t.side}">${t.side.toUpperCase()}</div>
      <div class="col-lev">${t.leverage}x</div>
      <div>${fmtUsd2(t.entry_price)} → ${t.close_price ? fmtUsd2(t.close_price) : "—"}</div>
      <div class="col-pnl ${pnlCls}">${fmtUsdSigned(t.pnl_usd)}</div>
      <div class="col-meta">
        <span>size ${fmtUsd2(t.size_usd)}</span>
        <span>${date}</span>
      </div>
      <div class="col-status ${t.status}">${t.status}</div>
    `;
    wrap.appendChild(row);
  }
}

/* ----- actions ----- */

function clearTradeError() { $("trade-error").textContent = ""; }
function showTradeError(msg) { $("trade-error").textContent = msg; }

async function openTrade() {
  clearTradeError();
  const size = Number($("trade-size").value || 0);
  const lev  = Number($("trade-leverage").value || 1);
  if (size < 1) return showTradeError("size must be ≥ $1");
  const btn = $("open-trade-btn");
  btn.disabled = true;
  try {
    const res = await paperPost("/api/paper/open", {
      uid: paper.uid, side: paper.side, size_usd: size, leverage: lev,
    });
    applyPaperAccount(res.account);
    await refreshHistory();
  } catch (e) {
    showTradeError(e.message || "failed to open");
  } finally {
    btn.disabled = false;
  }
}

async function closeTrade(tradeId) {
  try {
    const res = await paperPost("/api/paper/close", {
      uid: paper.uid, trade_id: tradeId,
    });
    applyPaperAccount(res.account);
    await refreshHistory();
  } catch (e) {
    showTradeError(e.message || "failed to close");
  }
}

async function resetAccount() {
  if (!confirm("Reset account to $1000? All open positions will be cancelled.")) return;
  try {
    const res = await paperPost("/api/paper/reset", { uid: paper.uid });
    applyPaperAccount(res.account);
    await refreshHistory();
  } catch (e) {
    showTradeError(e.message || "failed to reset");
  }
}

async function refreshHistory() {
  try {
    const res = await paperGet(`/api/paper/trades?uid=${encodeURIComponent(paper.uid)}&limit=100`);
    paper.history = res.trades || [];
    renderHistory(paper.history);
  } catch {}
}

/* ----- modal control ----- */

function openTradesModal() {
  $("trades-modal").hidden = false;
  refreshHistory();
}
function closeTradesModal() {
  $("trades-modal").hidden = true;
}

/* ----- WS ----- */

function connectPaperWs() {
  if (paper.ws) {
    paper.ws.onclose = null;
    paper.ws.close();
  }
  const url = `${WS_PROTO}://${BACKEND}/ws/paper?token=${encodeURIComponent(state.token)}&uid=${encodeURIComponent(paper.uid)}`;
  const ws = new WebSocket(url);
  paper.ws = ws;
  ws.onmessage = (m) => {
    let msg;
    try { msg = JSON.parse(m.data); } catch { return; }
    if (msg.event === "snapshot" || msg.event === "tick") {
      applyPaperAccount(msg.data);
    } else if (msg.event === "liquidated") {
      // Force a fresh history pull so the row appears in the closed list,
      // and let the next snapshot strip the lines via syncTradeLines.
      refreshHistory();
    }
  };
  ws.onclose = () => setTimeout(connectPaperWs, 2000);
}

function initPaperUi() {
  paper.uid = getPaperUid();

  // Header button
  $("my-trades-btn").onclick = openTradesModal;

  // Modal close handlers
  document.querySelectorAll("#trades-modal [data-close]").forEach((el) => {
    el.onclick = closeTradesModal;
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("trades-modal").hidden) closeTradesModal();
  });

  // Side toggle
  document.querySelectorAll(".side-btn").forEach((b) => {
    b.onclick = () => setSide(b.dataset.side);
  });

  // Form inputs
  $("trade-size").oninput = updateTradePreview;
  $("trade-leverage").oninput = updateTradePreview;
  $("open-trade-btn").onclick = openTrade;
  $("reset-account").onclick = resetAccount;

  // Initial preview render
  updateTradePreview();
}

async function init() {
  state.token = await ensureAuth();
  document.querySelector("header").style.display = "";
  document.querySelector("main").style.display = "";
  $("logout").onclick = logout;
  state.liqVisible = localStorage.getItem("ix_liq_on") === "1";
  buildCharts();
  $("liq-toggle").setAttribute("aria-pressed", state.liqVisible ? "true" : "false");
  $("liq-toggle").onclick = () => setLiqVisible(!state.liqVisible);
  const r = await fetch(`${HTTP}://${BACKEND}/api/symbols`, {
    headers: { "X-Auth-Token": state.token },
    cache: "no-store",
    credentials: "omit",
  }).then((x) => x.json());
  const sel = $("symbol");
  r.symbols.forEach((s) => {
    const o = document.createElement("option");
    o.value = s; o.textContent = s;
    sel.appendChild(o);
  });
  sel.value = state.symbol;
  sel.onchange = () => { state.symbol = sel.value; connect(); };

  const tfs = $("tfs");
  r.timeframes.forEach((tf) => {
    const b = document.createElement("button");
    b.textContent = tf;
    if (tf === state.timeframe) b.classList.add("active");
    b.onclick = () => {
      state.timeframe = tf;
      [...tfs.children].forEach((c) => c.classList.toggle("active", c.textContent === tf));
      connect();
    };
    tfs.appendChild(b);
  });

  connect();
  connectFunding();
  connectBasis();
  connectTaker();
  connectGex();
  connectLiveLiq();
  if (state.liqVisible) connectLiquidations();

  initPaperUi();
  connectPaperWs();
}

init();

/* ---------- Measure tool (Shift+drag on price pane) ---------- */
(function attachMeasure() {
  const pane = document.getElementById("price");

  const hint = document.createElement("div");
  hint.className = "hint";
  hint.textContent = "shift + drag to measure";
  pane.appendChild(hint);

  let active = false;
  let start = null;       // { x, y, price, time }
  let box = null;
  let label = null;

  const tfMs = () => ({ "1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000 }[state.timeframe] || 60_000);

  function clear() {
    if (box) box.remove();
    if (label) label.remove();
    box = label = null;
    active = false;
    start = null;
  }

  function pointFromEvent(e) {
    const rect = pane.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    const price = state.candleSeries.coordinateToPrice(y);
    const time = state.priceChart.timeScale().coordinateToTime(x);
    return { x, y, price, time };
  }

  function fmtNum(n, d = 2) {
    return Number(n).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
  }

  function fmtDuration(ms) {
    const s = Math.abs(Math.round(ms / 1000));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${sec}s`;
    return `${sec}s`;
  }

  function render(end) {
    if (!start) return;
    const x1 = Math.min(start.x, end.x);
    const x2 = Math.max(start.x, end.x);
    const y1 = Math.min(start.y, end.y);
    const y2 = Math.max(start.y, end.y);
    const w = x2 - x1;
    const h = y2 - y1;

    const dPrice = end.price - start.price;
    const pct = (dPrice / start.price) * 100;
    const up = dPrice >= 0;

    if (!box) {
      box = document.createElement("div");
      box.className = "measure-box";
      pane.appendChild(box);
    }
    box.className = "measure-box " + (up ? "up" : "down");
    box.style.left = x1 + "px";
    box.style.top = y1 + "px";
    box.style.width = w + "px";
    box.style.height = h + "px";

    if (!label) {
      label = document.createElement("div");
      label.className = "measure-label";
      pane.appendChild(label);
    }

    let bars = "—", duration = "—";
    if (typeof start.time === "number" && typeof end.time === "number") {
      const ms = (end.time - start.time) * 1000;
      bars = Math.round(Math.abs(ms) / tfMs());
      duration = fmtDuration(ms);
    }

    const sign = up ? "+" : "−";
    const cls = up ? "pos" : "neg";
    label.innerHTML = `
      <div class="row"><span class="lbl">Δ</span><span class="${cls}">${sign}${fmtNum(Math.abs(dPrice))}  (${sign}${fmtNum(Math.abs(pct))}%)</span></div>
      <div class="row"><span class="lbl">bars</span><span>${bars}</span><span class="lbl">time</span><span>${duration}</span></div>
      <div class="row"><span class="lbl">from</span><span>${fmtNum(start.price)}</span><span class="lbl">to</span><span>${fmtNum(end.price)}</span></div>
    `;

    // Position label near the end point, clamped to pane
    const paneRect = pane.getBoundingClientRect();
    let lx = end.x + 12;
    let ly = end.y + 12;
    label.style.left = "0px"; label.style.top = "0px";
    const lw = label.offsetWidth, lh = label.offsetHeight;
    if (lx + lw > paneRect.width - 4) lx = end.x - lw - 12;
    if (ly + lh > paneRect.height - 4) ly = end.y - lh - 12;
    if (lx < 4) lx = 4;
    if (ly < 4) ly = 4;
    label.style.left = lx + "px";
    label.style.top = ly + "px";
  }

  pane.addEventListener("mousedown", (e) => {
    if (!e.shiftKey || e.button !== 0) return;
    e.preventDefault();
    clear();
    active = true;
    start = pointFromEvent(e);
    state.priceChart.applyOptions({ handleScroll: false, handleScale: false });
  });

  window.addEventListener("mousemove", (e) => {
    if (!active) return;
    render(pointFromEvent(e));
  });

  window.addEventListener("mouseup", () => {
    if (!active) return;
    active = false;
    state.priceChart.applyOptions({ handleScroll: true, handleScale: true });
  });

  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") clear();
  });
})();
