/* =========================================================
   iX — Bitunix ICT Scalping Terminal
   app.js — complete frontend logic
   ========================================================= */
'use strict';

// ─── Backend URL resolution ───────────────────────────────
const _meta = document.querySelector('meta[name="ix-backend"]');
const BACKEND_HOST = (_meta && _meta.content) ? _meta.content.trim() : location.host;
const PROTO    = location.protocol === 'https:' ? 'https' : 'http';
const WS_PROTO = location.protocol === 'https:' ? 'wss'   : 'ws';
const API  = `${PROTO}://${BACKEND_HOST}`;
const WS   = `${WS_PROTO}://${BACKEND_HOST}`;

// ─── Auth ─────────────────────────────────────────────────
const AUTH_KEY = 'ix_token';
let TOKEN = localStorage.getItem(AUTH_KEY) || '';

const authOverlay = document.getElementById('auth-overlay');
const authForm    = document.getElementById('auth-form');
const authInput   = document.getElementById('auth-input');
const authError   = document.getElementById('auth-error');

authForm.addEventListener('submit', async e => {
  e.preventDefault();
  const candidate = authInput.value.trim();
  try {
    const r = await fetch(`${API}/api/auth/check`, {
      headers: { 'X-Auth-Token': candidate }
    });
    if (r.ok) {
      TOKEN = candidate;
      localStorage.setItem(AUTH_KEY, TOKEN);
      authOverlay.style.display = 'none';
      document.querySelector('header').style.display = '';
      document.querySelector('main').style.display   = '';
      init();
    } else {
      authError.textContent = 'invalid token';
    }
  } catch {
    authError.textContent = 'connection error';
  }
});

document.getElementById('logout').addEventListener('click', () => {
  localStorage.removeItem(AUTH_KEY);
  TOKEN = '';
  location.reload();
});

// Auto-check stored token
(async () => {
  if (!TOKEN) return;
  try {
    const r = await fetch(`${API}/api/auth/check`, {
      headers: { 'X-Auth-Token': TOKEN }
    });
    if (r.ok) {
      authOverlay.style.display = 'none';
      document.querySelector('header').style.display = '';
      document.querySelector('main').style.display   = '';
      init();
    } else {
      TOKEN = '';
      localStorage.removeItem(AUTH_KEY);
    }
  } catch { /* server down, show auth form */ }
})();

// ─── Global state ─────────────────────────────────────────
let currentSymbol    = 'BTCUSDT';
let currentTF        = '5m';
let currentICT       = null;
let currentSignals   = [];
let candleWS         = null;
let fundingWS        = null;
let fundingData      = {};

// ─── Chart instances ──────────────────────────────────────
let chart        = null;
let candleSeries = null;
let overlayCanvas = null;

// ─── DOM refs ─────────────────────────────────────────────
const statusEl      = document.getElementById('status');
const pxEl          = document.getElementById('px');
const tokenLogo     = document.getElementById('token-logo');
const tokenName     = document.getElementById('token-name');
const tokenPrice    = document.getElementById('token-price');
const tokenChange   = document.getElementById('token-change');
const tokenVolume   = document.getElementById('token-volume');
const tfsEl         = document.getElementById('tfs');
const signalList    = document.getElementById('signal-panel');
const fundRate      = document.getElementById('fund-rate');
const fundCountdown = document.getElementById('fund-countdown');
const fundMark      = document.getElementById('fund-mark');

// ─── ICT colour scheme ────────────────────────────────────
const ICT = {
  bullOB:       'rgba(0, 180, 90,  0.18)',
  bullOBBorder: 'rgba(0, 220, 100, 0.70)',
  bearOB:       'rgba(220, 40,  40, 0.18)',
  bearOBBorder: 'rgba(255, 60,  60, 0.70)',
  bullFVG:      'rgba(0, 200, 140, 0.12)',
  bullFVGBorder:'rgba(0, 240, 160, 0.50)',
  bearFVG:      'rgba(200, 50,  50, 0.12)',
  bearFVGBorder:'rgba(240, 80,  80, 0.50)',
  bullBOS:      '#00e676',
  bearBOS:      '#ff1744',
  bullCHoCH:    '#69f0ae',
  bearCHoCH:    '#ff6d00',
  eqh:          'rgba(220, 200, 60, 0.80)',
  eql:          'rgba(60,  200, 220, 0.80)',
  bsl:          'rgba(80,  140, 255, 0.65)',
  ssl:          'rgba(255, 100, 80,  0.65)',
  sweep:        'rgba(255, 215, 0,   0.90)',
  premium:      'rgba(220, 50,  50,  0.06)',
  discount:     'rgba(30,  180, 80,  0.06)',
  displacement: 'rgba(255, 200, 0,   0.85)',
};

// ─── Init ─────────────────────────────────────────────────
async function init() {
  try {
    const r = await fetch(`${API}/api/symbols?token=${TOKEN}`);
    const data = await r.json();
    currentSymbol = data.default || 'BTCUSDT';
    buildTimeframePicker(data.timeframes || ['1m','5m','15m','1h','4h']);
  } catch { buildTimeframePicker(['1m','5m','15m','1h','4h']); }

  buildChart();
  setupSymbolSearch();
  connectCandle(currentSymbol, currentTF);
  connectFunding(currentSymbol);
  setupTradesModal();
  setupAI();
  updateTokenMeta();
}

// ─── Chart setup ──────────────────────────────────────────
function buildChart() {
  const container = document.getElementById('price');
  container.style.position = 'relative';
  container.innerHTML = '';

  chart = LightweightCharts.createChart(container, {
    layout: {
      background:  { type: 'solid', color: '#080810' },
      textColor:   '#666',
      fontFamily:  "'JetBrains Mono', 'Fira Code', 'Courier New', monospace",
      fontSize:    11,
    },
    grid: {
      vertLines: { color: 'rgba(255,255,255,0.03)' },
      horzLines: { color: 'rgba(255,255,255,0.03)' },
    },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Normal,
      vertLine: { color: 'rgba(255,255,255,0.15)', labelBackgroundColor: '#111120' },
      horzLine: { color: 'rgba(255,255,255,0.15)', labelBackgroundColor: '#111120' },
    },
    rightPriceScale: {
      borderColor: 'rgba(255,255,255,0.06)',
      scaleMargins: { top: 0.08, bottom: 0.08 },
    },
    timeScale: {
      borderColor: 'rgba(255,255,255,0.06)',
      timeVisible: true,
      secondsVisible: false,
    },
    handleScroll: { mouseWheel: true, pressedMouseMove: true },
    handleScale:  { mouseWheel: true, pinch: true },
    width:  container.clientWidth,
    height: container.clientHeight,
  });

  candleSeries = chart.addCandlestickSeries({
    upColor:        '#00e676',
    downColor:      '#ff1744',
    borderUpColor:  '#00e676',
    borderDownColor:'#ff1744',
    wickUpColor:    '#00e676',
    wickDownColor:  '#ff1744',
  });

  // Overlay canvas for ICT zone rendering
  overlayCanvas = document.createElement('canvas');
  overlayCanvas.style.cssText =
    'position:absolute;top:0;left:0;pointer-events:none;z-index:10;';
  container.appendChild(overlayCanvas);
  syncCanvas();

  // Use rAF so the canvas always draws *after* LightweightCharts
  // finishes updating its own coordinate system for that frame.
  let _raf = null;
  function scheduleDrawICT() {
    if (_raf) cancelAnimationFrame(_raf);
    _raf = requestAnimationFrame(() => { _raf = null; drawICT(); });
  }

  // Horizontal zoom / scroll
  chart.timeScale().subscribeVisibleLogicalRangeChange(scheduleDrawICT);
  // Vertical zoom (price scale drag)
  chart.priceScale('right').applyOptions({});   // ensure scale exists
  chart.timeScale().subscribeVisibleTimeRangeChange(scheduleDrawICT);
  // Crosshair moves (live cursor tracking)
  chart.subscribeCrosshairMove(scheduleDrawICT);

  const ro = new ResizeObserver(() => {
    chart.applyOptions({
      width:  container.clientWidth,
      height: container.clientHeight,
    });
    syncCanvas();
    scheduleDrawICT();
  });
  ro.observe(container);
}

function syncCanvas() {
  const c = document.getElementById('price');
  overlayCanvas.width        = c.clientWidth;
  overlayCanvas.height       = c.clientHeight;
  overlayCanvas.style.width  = c.clientWidth  + 'px';
  overlayCanvas.style.height = c.clientHeight + 'px';
}

// ─── Timeframe picker ─────────────────────────────────────
function buildTimeframePicker(tfs) {
  tfsEl.innerHTML = '';
  tfs.forEach(tf => {
    const btn = document.createElement('button');
    btn.className  = 'tf-btn' + (tf === currentTF ? ' active' : '');
    btn.textContent = tf;
    btn.addEventListener('click', () => {
      if (tf === currentTF) return;
      currentTF = tf;
      tfsEl.querySelectorAll('.tf-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      reconnectCandle();
    });
    tfsEl.appendChild(btn);
  });
}

// ─── Symbol search ────────────────────────────────────────
function setupSymbolSearch() {
  const input   = document.getElementById('symbol-input');
  const results = document.getElementById('symbol-results');
  let debounce  = null;

  input.addEventListener('input', () => {
    clearTimeout(debounce);
    const q = input.value.trim();
    if (!q) { results.hidden = true; return; }
    debounce = setTimeout(async () => {
      try {
        const r = await fetch(
          `${API}/api/symbols/search?q=${encodeURIComponent(q)}&token=${TOKEN}`
        );
        const d = await r.json();
        renderSearch(d.results || []);
      } catch { results.hidden = true; }
    }, 220);
  });

  input.addEventListener('keydown', e => {
    if (e.key === 'Escape') { results.hidden = true; input.blur(); }
  });
  document.addEventListener('click', e => {
    if (!input.contains(e.target) && !results.contains(e.target))
      results.hidden = true;
  });

  function renderSearch(items) {
    results.innerHTML = '';
    if (!items.length) { results.hidden = true; return; }
    items.forEach(item => {
      const div = document.createElement('div');
      div.className = 'sr-item';
      div.innerHTML =
        `<span class="sr-sym">${item.symbol}</span>` +
        `<span class="sr-name">${item.name}</span>`;
      div.addEventListener('click', () => {
        input.value = '';
        results.hidden = true;
        selectSymbol(item.symbol);
      });
      results.appendChild(div);
    });
    results.hidden = false;
  }
}

async function selectSymbol(sym) {
  currentSymbol = sym.toUpperCase();
  reconnectCandle();
  reconnectFunding();
  updateTokenMeta();
}

async function updateTokenMeta() {
  try {
    const r = await fetch(
      `${API}/api/symbols/meta?symbol=${currentSymbol}&token=${TOKEN}`
    );
    if (!r.ok) return;
    const d = await r.json();
    tokenName.textContent  = d.base || currentSymbol;
    tokenPrice.textContent = fmt(d.price);
    const chg = parseFloat(d.change_24h_pct || 0);
    tokenChange.textContent = (chg >= 0 ? '+' : '') + chg.toFixed(2) + '%';
    tokenChange.className   = 'token-change ' + (chg >= 0 ? 'up' : 'down');
    tokenVolume.textContent = fmtVol(d.volume_24h_usd);
    if (d.logo) { tokenLogo.src = d.logo; tokenLogo.style.display = ''; }
    else tokenLogo.style.display = 'none';
  } catch { /* ignore */ }
}

// ─── WebSocket connections ────────────────────────────────
function connectCandle(sym, tf) {
  if (candleWS) { try { candleWS.close(); } catch{} candleWS = null; }
  setStatus('connecting');

  const url = `${WS}/ws/${sym}/${tf}?token=${TOKEN}`;
  candleWS   = new WebSocket(url);

  candleWS.onopen    = () => setStatus('live');
  candleWS.onmessage = e => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    if (msg.event === 'ping') return;
    handleCandleMessage(msg);
  };
  candleWS.onclose = () => {
    setStatus('disconnected');
    setTimeout(() => {
      if (currentSymbol === sym && currentTF === tf) connectCandle(sym, tf);
    }, 3000);
  };
  candleWS.onerror = () => setStatus('error');
}

function connectFunding(sym) {
  if (fundingWS) { try { fundingWS.close(); } catch{} fundingWS = null; }
  const url  = `${WS}/ws/funding?symbol=${sym}&token=${TOKEN}`;
  fundingWS  = new WebSocket(url);
  fundingWS.onmessage = e => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    if (msg.event === 'ping') return;
    handleFundingMessage(msg);
  };
  fundingWS.onclose = () => {
    setTimeout(() => {
      if (currentSymbol === sym) connectFunding(sym);
    }, 5000);
  };
}

function reconnectCandle() {
  currentICT     = null;
  currentSignals = [];
  if (candleSeries) candleSeries.setData([]);
  clearOverlay();
  clearSignals();
  connectCandle(currentSymbol, currentTF);
}

function reconnectFunding() {
  connectFunding(currentSymbol);
}

// ─── Candle message handling ──────────────────────────────
function handleCandleMessage(msg) {
  const { event, data } = msg;
  if (event === 'snapshot') { loadSnapshot(data); return; }
  if (event === 'tick' || event === 'candle') { updateCandle(data); return; }
  if (event === 'ict')     { currentICT = data; requestAnimationFrame(drawICT); return; }
  if (event === 'signals') {
    currentSignals = Array.isArray(data) ? data : [];
    renderSignals();
    return;
  }
}

function loadSnapshot(data) {
  const candles = (data.candles || []).map(c => ({
    time:  Math.floor(c.ts / 1000),
    open:  c.open,
    high:  c.high,
    low:   c.low,
    close: c.close,
  }));
  if (candles.length) {
    candleSeries.setData(candles);
    chart.timeScale().fitContent();
    pxEl.textContent = fmt(candles[candles.length - 1].close);
  }
  if (data.ict)     { currentICT     = data.ict;    requestAnimationFrame(drawICT); }
  if (data.signals) { currentSignals = data.signals; renderSignals(); }
}

function updateCandle(c) {
  candleSeries.update({
    time:  Math.floor(c.ts / 1000),
    open:  c.open,
    high:  c.high,
    low:   c.low,
    close: c.close,
  });
  pxEl.textContent = fmt(c.close);
}

// ─── Funding message handling ─────────────────────────────
function handleFundingMessage(msg) {
  const cur = msg.event === 'snapshot' ? msg.data.current : msg.data;
  if (!cur) return;
  fundingData = cur;

  const rate = parseFloat(cur.funding_rate || 0);
  fundRate.textContent = (rate * 100).toFixed(4) + '%';
  fundRate.className   = 'fund-val ' + (rate > 0 ? 'pos' : rate < 0 ? 'neg' : '');

  if (cur.next_funding_time) {
    const ms  = cur.next_funding_time - Date.now();
    const hrs = Math.max(0, Math.floor(ms / 3600000));
    const min = Math.max(0, Math.floor((ms % 3600000) / 60000));
    fundCountdown.textContent = `next ${hrs}h ${String(min).padStart(2,'0')}m`;
  }
  if (cur.mark_price) fundMark.textContent = fmt(cur.mark_price);
}

// ─── ICT overlay drawing ──────────────────────────────────
function clearOverlay() {
  if (!overlayCanvas) return;
  const ctx = overlayCanvas.getContext('2d');
  ctx.clearRect(0, 0, overlayCanvas.width, overlayCanvas.height);
}

function drawICT() {
  if (!overlayCanvas || !candleSeries || !currentICT) { clearOverlay(); return; }
  syncCanvas();
  const ctx = overlayCanvas.getContext('2d');
  ctx.clearRect(0, 0, overlayCanvas.width, overlayCanvas.height);

  const W   = overlayCanvas.width;
  const ts  = chart.timeScale();
  const now = Math.floor(Date.now() / 1000);
  const ict = currentICT;

  function py(price)  {
    const v = candleSeries.priceToCoordinate(price);
    return (v != null && isFinite(v)) ? v : null;
  }
  function tx(timeS)  {
    const v = ts.timeToCoordinate(timeS);
    return (v != null && isFinite(v)) ? v : null;
  }
  function txNow()    { return tx(now) ?? W; }

  ctx.font      = '10px "JetBrains Mono", monospace';
  ctx.textAlign = 'left';

  // ── Premium / Discount background ──────────────────────
  const pd = ict.premium_discount;
  if (pd && pd.range_high && pd.range_low && pd.equilibrium) {
    const yH = py(pd.range_high);
    const yE = py(pd.equilibrium);
    const yL = py(pd.range_low);

    if (yH != null && yE != null) {
      ctx.fillStyle = ICT.premium;
      ctx.fillRect(0, Math.min(yH, yE), W, Math.abs(yE - yH));
    }
    if (yE != null && yL != null) {
      ctx.fillStyle = ICT.discount;
      ctx.fillRect(0, Math.min(yE, yL), W, Math.abs(yL - yE));
    }
    // 50% equilibrium line
    if (yE != null) {
      ctx.strokeStyle = 'rgba(180,180,180,0.30)';
      ctx.setLineDash([4, 6]);
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(0, yE); ctx.lineTo(W, yE); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = 'rgba(180,180,180,0.50)';
      ctx.fillText('EQ 50%', 5, yE - 4);
    }
    // 62% / 38% fibs
    if (pd.fib_618) {
      const y618 = py(pd.fib_618);
      if (y618 != null) {
        ctx.strokeStyle = 'rgba(220,80,80,0.25)';
        ctx.setLineDash([2,8]); ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(0, y618); ctx.lineTo(W, y618); ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = 'rgba(220,80,80,0.50)';
        ctx.fillText('62% Premium', 5, y618 - 4);
      }
    }
    if (pd.fib_382) {
      const y382 = py(pd.fib_382);
      if (y382 != null) {
        ctx.strokeStyle = 'rgba(40,180,80,0.25)';
        ctx.setLineDash([2,8]); ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(0, y382); ctx.lineTo(W, y382); ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = 'rgba(40,180,80,0.50)';
        ctx.fillText('38% Discount', 5, y382 + 12);
      }
    }
  }

  // ── Session levels ──────────────────────────────────────
  const SESS_COLOR = {
    asia:     'rgba(140,80,220,0.55)',
    london:   'rgba(80,160,220,0.55)',
    ny:       'rgba(220,160,40,0.55)',
    prev_day: 'rgba(180,180,180,0.45)',
  };
  const SESS_LABEL = { asia:'AS', london:'LN', ny:'NY', prev_day:'PD' };

  for (const [sess, levels] of Object.entries(ict.sessions || {})) {
    const col = SESS_COLOR[sess] || 'rgba(180,180,180,0.4)';
    const lbl = SESS_LABEL[sess] || sess.toUpperCase();
    ['high','low'].forEach(hl => {
      if (!levels[hl]) return;
      const y = py(levels[hl]);
      if (y == null) return;
      ctx.strokeStyle = col; ctx.setLineDash([3,6]); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = col;
      ctx.fillText(`${lbl}${hl[0].toUpperCase()}`, 5, hl === 'high' ? y - 3 : y + 11);
    });
  }

  // ── Sell-side / Buy-side liquidity lines ────────────────
  const liq = ict.liquidity || {};
  for (const lvl of (liq.sell_side || [])) {
    const y = py(lvl.price);
    if (y == null) continue;
    ctx.strokeStyle = ICT.ssl; ctx.setLineDash([2,7]); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
    ctx.setLineDash([]);
  }
  for (const lvl of (liq.buy_side || [])) {
    const y = py(lvl.price);
    if (y == null) continue;
    ctx.strokeStyle = ICT.bsl; ctx.setLineDash([2,7]); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
    ctx.setLineDash([]);
  }

  // ── Equal Highs / Equal Lows ────────────────────────────
  for (const eq of (ict.equal_levels || [])) {
    const y = py(eq.price);
    if (y == null) continue;
    const isH = eq.type === 'EQH';
    ctx.strokeStyle = isH ? ICT.eqh : ICT.eql;
    ctx.setLineDash([5, 4]); ctx.lineWidth = 1.2;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = ctx.strokeStyle;
    ctx.textAlign = 'right';
    ctx.fillText(`${eq.type}×${eq.count}`, W - 4, y - 3);
    ctx.textAlign = 'left';
  }

  // ── Liquidity Sweeps ────────────────────────────────────
  for (const sw of (liq.sweeps || [])) {
    const x = tx(Math.floor(sw.ts / 1000));
    const y = py(sw.price);
    if (x == null || y == null) continue;
    // Ring
    ctx.strokeStyle = ICT.sweep; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(x, y, 6, 0, Math.PI * 2); ctx.stroke();
    ctx.fillStyle = ICT.sweep;
    const arrow = sw.direction === 'bullish' ? '▲' : '▼';
    ctx.fillText(`${arrow}SWEEP`, x + 10, y + 4);
  }

  // ── Order Blocks ────────────────────────────────────────
  for (const ob of (ict.order_blocks || [])) {
    const xL = tx(Math.floor(ob.ts / 1000));
    const xR = txNow();
    const yT = py(ob.top);
    const yB = py(ob.bottom);
    if (xL == null || yT == null || yB == null) continue;

    const x1 = Math.max(0, xL);
    const x2 = Math.min(W + 80, xR);
    const y1 = Math.min(yT, yB);
    const y2 = Math.max(yT, yB);

    const isBull = ob.type === 'bullish';
    ctx.fillStyle   = isBull ? ICT.bullOB        : ICT.bearOB;
    ctx.strokeStyle = isBull ? ICT.bullOBBorder  : ICT.bearOBBorder;
    ctx.lineWidth   = ob.strength >= 80 ? 1.5 : 1;
    ctx.setLineDash([]);
    ctx.fillRect(x1, y1, x2 - x1, y2 - y1);
    ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);

    // Mid line
    const yMid = py(ob.mid);
    if (yMid != null) {
      ctx.strokeStyle = isBull ? 'rgba(0,220,100,0.30)' : 'rgba(255,60,60,0.30)';
      ctx.setLineDash([2,4]); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x1, yMid); ctx.lineTo(x2, yMid); ctx.stroke();
      ctx.setLineDash([]);
    }

    // Label
    ctx.fillStyle = isBull ? ICT.bullOBBorder : ICT.bearOBBorder;
    ctx.font      = 'bold 9px monospace';
    ctx.fillText(`OB ${ob.strength}`, x1 + 3, y1 + 11);
    ctx.font      = '10px monospace';

    // Mitigation diagonal hatch
    if (ob.mitigated) {
      ctx.strokeStyle = 'rgba(180,180,180,0.20)';
      ctx.lineWidth = 1; ctx.setLineDash([2,5]);
      for (let yy = y1 + 6; yy < y2; yy += 9) {
        ctx.beginPath(); ctx.moveTo(x1, yy); ctx.lineTo(x2, yy); ctx.stroke();
      }
      ctx.setLineDash([]);
    }
  }

  // ── Fair Value Gaps ─────────────────────────────────────
  for (const fvg of (ict.fvgs || [])) {
    const xL = tx(Math.floor(fvg.ts / 1000));
    const xR = txNow();
    const yT = py(fvg.top);
    const yB = py(fvg.bottom);
    if (xL == null || yT == null || yB == null) continue;

    const x1 = Math.max(0, xL);
    const x2 = Math.min(W + 80, xR);
    const y1 = Math.min(yT, yB);
    const y2 = Math.max(yT, yB);
    const isBull = fvg.type === 'bullish';
    const fp     = fvg.filled_pct || 0;

    ctx.fillStyle   = isBull ? ICT.bullFVG        : ICT.bearFVG;
    ctx.strokeStyle = isBull ? ICT.bullFVGBorder  : ICT.bearFVGBorder;
    ctx.setLineDash([3,3]); ctx.lineWidth = 1;
    ctx.fillRect(x1, y1, x2 - x1, y2 - y1);
    ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
    ctx.setLineDash([]);

    // Fill progress overlay
    if (fp > 0) {
      const fillH = (y2 - y1) * fp;
      ctx.fillStyle = isBull ? 'rgba(0,220,140,0.14)' : 'rgba(220,50,50,0.14)';
      ctx.fillRect(x1, isBull ? y2 - fillH : y1, x2 - x1, fillH);
    }

    ctx.fillStyle = isBull ? ICT.bullFVGBorder : ICT.bearFVGBorder;
    ctx.font      = '9px monospace';
    ctx.fillText(`FVG${fp > 0 ? ' ' + Math.round(fp*100) + '%' : ''}`, x1 + 3, y1 + 11);
    ctx.font = '10px monospace';
  }

  // ── BOS / CHoCH structure events ────────────────────────
  const events = (ict.structure || {}).events || [];
  for (const evt of events.slice(-20)) {
    const x = tx(Math.floor(evt.ts / 1000));
    const y = py(evt.price);
    if (x == null || y == null) continue;

    const isBull = evt.direction === 'bullish';
    const isBOS  = evt.type === 'BOS';
    const color  = isBull
      ? (isBOS ? ICT.bullBOS  : ICT.bullCHoCH)
      : (isBOS ? ICT.bearBOS  : ICT.bearCHoCH);

    ctx.strokeStyle = color;
    ctx.lineWidth   = isBOS ? 1.5 : 1.2;
    ctx.setLineDash(isBOS ? [] : [3,3]);
    ctx.beginPath();
    ctx.moveTo(Math.max(0, x - 80), y);
    ctx.lineTo(Math.min(W, x + 5), y);
    ctx.stroke();
    ctx.setLineDash([]);

    ctx.fillStyle = color;
    ctx.font      = `bold ${isBOS ? 10 : 9}px monospace`;
    ctx.fillText(evt.type, Math.max(2, x - 78), y - 3);
    ctx.font = '10px monospace';
  }

  // ── Swing High / Low dots ───────────────────────────────
  const swings = (ict.structure || {}).swings || {};
  for (const s of (swings.highs || [])) {
    const x = tx(Math.floor(s.ts / 1000));
    const y = py(s.price);
    if (x == null || y == null) continue;
    ctx.fillStyle = 'rgba(255,100,80,0.70)';
    ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI * 2); ctx.fill();
  }
  for (const s of (swings.lows || [])) {
    const x = tx(Math.floor(s.ts / 1000));
    const y = py(s.price);
    if (x == null || y == null) continue;
    ctx.fillStyle = 'rgba(0,220,130,0.70)';
    ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI * 2); ctx.fill();
  }

  // ── Displacement markers ─────────────────────────────────
  for (const d of (ict.displacement || [])) {
    const x = tx(Math.floor(d.ts / 1000));
    if (x == null) continue;
    const y1 = py(d.high);
    const y2 = py(d.low);
    if (y1 == null || y2 == null) continue;
    ctx.strokeStyle = ICT.displacement;
    ctx.lineWidth   = 2.5;
    ctx.beginPath(); ctx.moveTo(x, y1); ctx.lineTo(x, y2); ctx.stroke();
    ctx.fillStyle = ICT.displacement;
    ctx.font = 'bold 9px monospace';
    ctx.fillText('D', x - 4, d.direction === 'bullish' ? y2 + 12 : y1 - 5);
    ctx.font = '10px monospace';
  }
}

// ─── Signal panel ─────────────────────────────────────────
function clearSignals() {
  if (signalList)
    signalList.innerHTML = '<div class="sig-empty">awaiting data…</div>';
}

function renderSignals() {
  if (!signalList) return;
  if (!currentSignals || !currentSignals.length) {
    signalList.innerHTML =
      '<div class="sig-empty">no ICT setup detected — waiting for confluence…</div>';
    return;
  }
  signalList.innerHTML = '';
  for (const sig of currentSignals) {
    const card     = document.createElement('div');
    const conf     = sig.confidence || 0;
    const confCls  = conf >= 65 ? 'conf-high' : conf >= 45 ? 'conf-mid' : 'conf-low';
    const sweepBdg = sig.sweep_warning
      ? '<span class="sweep-badge">⚡ SWEEP</span>' : '';
    const rrTxt    = sig.rr ? `<span class="sig-rr">RR ${sig.rr}</span>` : '';
    card.className = `sig-card sig-${sig.type}`;
    card.innerHTML = `
      <div class="sig-header">
        <span class="sig-type">${sigTypeLabel(sig.type)}</span>
        ${sweepBdg}
        <span class="sig-conf ${confCls}">${conf}<span class="conf-unit">%</span></span>
      </div>
      ${rrTxt}
      <div class="sig-factors">${
        (sig.factors || []).slice(0, 4)
          .map(f => `<div class="sig-factor">· ${f}</div>`).join('')
      }</div>
      <div class="sig-reason">${sig.reason || ''}</div>
    `;
    signalList.appendChild(card);
  }
}

function sigTypeLabel(type) {
  return {
    long_setup:    '⬆ LONG SETUP',
    short_setup:   '⬇ SHORT SETUP',
    long_watch:    '↗ LONG WATCH',
    short_watch:   '↘ SHORT WATCH',
    sweep_warning: '⚡ SWEEP WARNING',
  }[type] || type.replace(/_/g,' ').toUpperCase();
}

// ─── Status ───────────────────────────────────────────────
function setStatus(s) {
  statusEl.textContent = s;
  statusEl.className   = 'status status-' + s.replace(/\s/g, '');
}

// ─── Paper trades modal ───────────────────────────────────
function setupTradesModal() {
  const btn   = document.getElementById('my-trades-btn');
  const modal = document.getElementById('trades-modal');
  if (!btn || !modal) return;
  modal.querySelectorAll('[data-close]')
    .forEach(el => el.addEventListener('click', () => { modal.hidden = true; }));
  btn.addEventListener('click', () => { modal.hidden = false; refreshPaper(); });

  const UID_KEY = 'ix_uid';
  let UID = localStorage.getItem(UID_KEY);
  if (!UID) { UID = crypto.randomUUID(); localStorage.setItem(UID_KEY, UID); }

  const sideToggle  = modal.querySelectorAll('.side-btn');
  const sizeInp     = document.getElementById('trade-size');
  const levInp      = document.getElementById('trade-leverage');
  const levDisp     = document.getElementById('lev-display');
  const openBtn     = document.getElementById('open-trade-btn');
  const tradeErr    = document.getElementById('trade-error');
  const resetBtn    = document.getElementById('reset-account');
  let   activeSide  = 'long';

  sideToggle.forEach(b => b.addEventListener('click', () => {
    sideToggle.forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    activeSide = b.dataset.side;
    openBtn.textContent = `OPEN ${activeSide.toUpperCase()}`;
    openBtn.className   = `primary-btn ${activeSide}`;
    updateTradeSummary();
  }));
  levInp.addEventListener('input', () => {
    levDisp.textContent = levInp.value + 'x';
    updateTradeSummary();
  });
  sizeInp.addEventListener('input', updateTradeSummary);

  function updateTradeSummary() {
    const size = parseFloat(sizeInp.value) || 0;
    const lev  = parseFloat(levInp.value)  || 1;
    const mark = parseFloat((fundMark.textContent || '').replace(/,/g,'')) || 0;
    document.getElementById('trade-margin').textContent   = '$' + (size / lev).toFixed(2);
    document.getElementById('trade-notional').textContent = '$' + size.toFixed(2);
    document.getElementById('trade-mark').textContent     = mark ? fmt(mark) : '—';
    if (mark) {
      const liq = activeSide === 'long'
        ? mark * (1 - 1/lev * 0.85)
        : mark * (1 + 1/lev * 0.85);
      document.getElementById('trade-liq').textContent = fmt(Math.max(0, liq));
    }
  }

  openBtn.addEventListener('click', async () => {
    tradeErr.textContent = '';
    const size = parseFloat(sizeInp.value);
    const lev  = parseFloat(levInp.value);
    if (!size || size <= 0) { tradeErr.textContent = 'enter size'; return; }
    try {
      const r = await fetch(`${API}/api/paper/open`, {
        method:  'POST',
        headers: { 'X-Auth-Token': TOKEN, 'Content-Type': 'application/json' },
        body:    JSON.stringify({ uid: UID, symbol: currentSymbol,
                                  side: activeSide, size_usd: size, leverage: lev }),
      });
      if (!r.ok) { tradeErr.textContent = (await r.json()).detail || 'error'; return; }
      await refreshPaper();
    } catch (err) { tradeErr.textContent = String(err); }
  });

  resetBtn.addEventListener('click', async () => {
    await fetch(`${API}/api/paper/reset`, {
      method:  'POST',
      headers: { 'X-Auth-Token': TOKEN, 'Content-Type': 'application/json' },
      body:    JSON.stringify({ uid: UID }),
    });
    await refreshPaper();
  });

  async function refreshPaper() {
    try {
      const [accR, trR] = await Promise.all([
        fetch(`${API}/api/paper/account?uid=${UID}&token=${TOKEN}`),
        fetch(`${API}/api/paper/trades?uid=${UID}&token=${TOKEN}`),
      ]);
      const acc    = await accR.json();
      const trades = (await trR.json()).trades || [];
      const open   = trades.filter(t => t.status === 'open');
      const closed = trades.filter(t => t.status !== 'open');

      document.getElementById('modal-balance').textContent    = '$' + (acc.balance||0).toFixed(2);
      document.getElementById('modal-equity').textContent     = '$' + (acc.equity||0).toFixed(2);
      document.getElementById('modal-unrealized').textContent = '$' + (acc.unrealized_pnl||0).toFixed(2);
      document.getElementById('balance').textContent          = '$' + (acc.balance||0).toFixed(2);
      const badge = document.getElementById('open-pos-count');
      badge.textContent = open.length; badge.hidden = open.length === 0;
      document.getElementById('active-count').textContent = `(${open.length})`;

      document.getElementById('active-trades').innerHTML = open.map(t => `
        <div class="trade-row">
          <span class="tr-sym">${t.symbol}</span>
          <span class="tr-side tr-${t.side}">${t.side.toUpperCase()}</span>
          <span class="tr-lev">${t.leverage}x</span>
          <span class="tr-entry">${fmt(t.entry_price)}</span>
          <span class="tr-notional">$${t.size_usd.toFixed(0)}</span>
          <span class="tr-pnl ${(acc.open_pnl?.[t.id]||0)>=0?'up':'down'}">
            ${(acc.open_pnl?.[t.id]||0)>=0?'+':''}$${(acc.open_pnl?.[t.id]||0).toFixed(2)}
          </span>
          <button class="close-trade-btn" data-id="${t.id}">✕</button>
        </div>`).join('') || '<div class="no-trades">no open positions</div>';

      document.getElementById('active-trades').querySelectorAll('.close-trade-btn')
        .forEach(btn => btn.addEventListener('click', async () => {
          await fetch(`${API}/api/paper/close`, {
            method:  'POST',
            headers: { 'X-Auth-Token': TOKEN, 'Content-Type': 'application/json' },
            body:    JSON.stringify({ uid: UID, trade_id: parseInt(btn.dataset.id) }),
          });
          await refreshPaper();
        }));

      document.getElementById('trade-history').innerHTML = closed.slice(0,20).map(t => `
        <div class="trade-row compact">
          <span class="tr-sym">${t.symbol}</span>
          <span class="tr-side tr-${t.side}">${t.side[0].toUpperCase()}</span>
          <span>${fmt(t.entry_price)} → ${t.close_price ? fmt(t.close_price) : '—'}</span>
          <span class="tr-pnl ${(t.pnl_usd||0)>=0?'up':'down'}">
            ${(t.pnl_usd||0)>=0?'+':''}$${(t.pnl_usd||0).toFixed(2)}
          </span>
        </div>`).join('') || '<div class="no-trades">no history</div>';
    } catch { /* ignore */ }
  }
}

// ─── AI modal ─────────────────────────────────────────────
function setupAI() {
  const aiBtn  = document.getElementById('ai-btn');
  const modal  = document.getElementById('ai-modal');
  if (!aiBtn || !modal) return;

  modal.querySelectorAll('[data-close]')
    .forEach(el => el.addEventListener('click', () => {
      modal.hidden = true;
      if (aiWS) { aiWS.close(); aiWS = null; }
    }));
  aiBtn.addEventListener('click', () => {
    modal.hidden = false;
    document.getElementById('ai-symbol-display').textContent = currentSymbol;
    document.getElementById('ai-intro').hidden   = false;
    document.getElementById('ai-running').hidden = true;
    document.getElementById('ai-result').hidden  = true;
  });

  let aiWS = null;
  const runBtn   = document.getElementById('ai-run-btn');
  const rerunBtn = document.getElementById('ai-rerun-btn');
  [runBtn, rerunBtn].forEach(b => b && b.addEventListener('click', startAnalysis));

  function startAnalysis() {
    document.getElementById('ai-intro').hidden   = true;
    document.getElementById('ai-running').hidden = false;
    document.getElementById('ai-result').hidden  = true;
    document.getElementById('ai-stream').textContent  = '';
    document.getElementById('ai-answer').textContent  = '';
    document.getElementById('ai-answer-head').hidden  = true;
    document.getElementById('ai-status').textContent  = 'initializing…';
    document.querySelectorAll('.ai-phases li')
      .forEach(li => li.removeAttribute('data-active'));

    if (aiWS) aiWS.close();
    aiWS = new WebSocket(
      `${WS}/ws/ai-analysis?symbol=${currentSymbol}&token=${TOKEN}`
    );
    aiWS.onopen    = () => aiWS.send(JSON.stringify({ action: 'start' }));
    aiWS.onmessage = e => {
      try { handleAIMsg(JSON.parse(e.data)); } catch { /* ignore */ }
    };
    aiWS.onclose = () => {
      if (!document.getElementById('ai-running').hidden) {
        document.getElementById('ai-running').hidden = true;
        document.getElementById('ai-result').hidden  = false;
      }
    };
  }

  function handleAIMsg(msg) {
    if (msg.event === 'phase') {
      document.getElementById('ai-status').textContent = msg.label || msg.phase;
      document.querySelectorAll('.ai-phases li').forEach(li => {
        li.removeAttribute('data-active');
        if (li.dataset.phase === msg.phase) li.setAttribute('data-active','1');
      });
    }
    if (msg.event === 'thinking_delta') {
      const el = document.getElementById('ai-stream');
      el.textContent += msg.text; el.scrollTop = el.scrollHeight;
    }
    if (msg.event === 'content_delta') {
      document.getElementById('ai-answer-head').hidden = false;
      const el = document.getElementById('ai-answer');
      el.textContent += msg.text; el.scrollTop = el.scrollHeight;
    }
    if (msg.event === 'result') {
      document.getElementById('ai-running').hidden = true;
      document.getElementById('ai-result').hidden  = false;
      const r = msg.data || {};
      document.getElementById('ai-candle').textContent     = r.next_candle  || '—';
      document.getElementById('ai-direction').textContent  = r.direction    || '—';
      document.getElementById('ai-confidence').textContent = r.confidence
        ? r.confidence + '%' : '—';
      document.getElementById('ai-factors').innerHTML =
        (r.key_factors || []).map(f => `<li>${f}</li>`).join('');
      document.getElementById('ai-reasoning').textContent = r.reasoning || '';
    }
    if (msg.event === 'error') {
      document.getElementById('ai-running').hidden = true;
      document.getElementById('ai-result').hidden  = false;
      const errEl = document.getElementById('ai-error');
      if (errEl) { errEl.hidden = false; errEl.textContent = msg.message; }
    }
  }
}

// ─── Formatters ───────────────────────────────────────────
function fmt(v) {
  const n = parseFloat(v);
  if (isNaN(n)) return '—';
  if (n >= 10000) return n.toLocaleString('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  if (n >= 100)   return n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (n >= 1)     return n.toLocaleString('en-US', { minimumFractionDigits: 4, maximumFractionDigits: 4 });
  return n.toLocaleString('en-US', { minimumFractionDigits: 6, maximumFractionDigits: 6 });
}

function fmtVol(v) {
  const n = parseFloat(v);
  if (isNaN(n)) return '—';
  if (n >= 1e9) return (n/1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n/1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n/1e3).toFixed(1) + 'K';
  return n.toFixed(2);
}
