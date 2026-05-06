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
  candleSeries: null,
  cvdSeries: null,
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
  let token = localStorage.getItem("ix_token") || "";
  if (token) {
    if (await verifyToken(token)) return token;
    // Stored token failed verification — drop it so we don't keep retrying
    // a broken value across refreshes.
    localStorage.removeItem("ix_token");
  }

  return new Promise((resolve) => {
    const overlay = $("auth-overlay");
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
        overlay.style.display = "none";
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

  const sig = c.signal || "neutral";
  const anim = $("funding-anim");
  const sigBox = $("funding-signal");
  anim.dataset.state = sig;
  sigBox.dataset.state = sig;
  $("signal-text").textContent = sig.toUpperCase();
  $("signal-sub").textContent =
    sig === "squeeze" ? "shorts trapped — bullish" :
    sig === "flush"   ? "longs trapped — bearish" :
                        "no setup";
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
  rightPriceScale: { borderColor: "#1c1c1c" },
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

  state.priceChart = LightweightCharts.createChart($("price"), chartOpts);
  state.candleSeries = state.priceChart.addCandlestickSeries({
    upColor: "#26a69a", downColor: "#ef5350",
    borderUpColor: "#26a69a", borderDownColor: "#ef5350",
    wickUpColor: "#26a69a", wickDownColor: "#ef5350",
  });

  // Basis histogram lives in the CVD pane — same time axis, separate overlay
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
  // Keep the CVD line in the top 75% so it never overlaps the basis bars.
  state.cvdChart.priceScale("right").applyOptions({
    scaleMargins: { top: 0.05, bottom: 0.28 },
  });

  state.cvdChart = LightweightCharts.createChart($("cvd-pane"), {
    ...chartOpts,
    timeScale: { ...chartOpts.timeScale, visible: false },
  });
  state.cvdSeries = state.cvdChart.addLineSeries({
    color: "#d4d4d4", lineWidth: 2, priceLineVisible: false,
  });

  // Sync time scales between panes — guarded to prevent feedback loop.
  let syncing = false;
  const sync = (src, dst) => src.timeScale().subscribeVisibleLogicalRangeChange((r) => {
    if (!r || syncing) return;
    syncing = true;
    try { dst.timeScale().setVisibleLogicalRange(r); } finally { syncing = false; }
  });
  sync(state.priceChart, state.cvdChart);
  sync(state.cvdChart, state.priceChart);

  const onResize = () => {
    state.priceChart.applyOptions({ width: $("price").clientWidth, height: $("price").clientHeight });
    state.cvdChart.applyOptions({ width: $("cvd-pane").clientWidth, height: $("cvd-pane").clientHeight });
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
  if (state.liqVisible) connectLiquidations();
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
