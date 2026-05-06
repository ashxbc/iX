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
  if (token && await verifyToken(token)) return token;

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
  location.reload();
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
  };
  onResize();
  window.addEventListener("resize", onResize);
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

async function init() {
  state.token = await ensureAuth();
  document.querySelector("header").style.display = "";
  document.querySelector("main").style.display = "";
  $("logout").onclick = logout;
  buildCharts();
  const r = await fetch(`${HTTP}://${BACKEND}/api/symbols`, {
    headers: { "X-Auth-Token": state.token },
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
