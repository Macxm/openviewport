// Browser renderer for the viewport wall.
// The agent decides layout and which stream each tile plays; this file only draws it.

import { StreamPlayer } from './player.js';

const params = new URLSearchParams(location.search);
const STALL_MS = 8000;          // no new frames for this long => reconnect the tile
const WATCHDOG_MS = 2000;       // how often that is checked
const SWAP_TIMEOUT_MS = 5000;   // max time to keep the old stream while the new one starts
const STATS_EVERY_MS = 5000;
const RENDERER_ID = params.get('renderer') || `browser-${Math.random().toString(36).slice(2, 8)}`;
// When the agent has auth.token set, the kiosk URL carries ?token=... and we pass it on.
// Browsers cannot set headers on a WebSocket handshake, so it has to be a query parameter.
const API_TOKEN = params.get('token') || '';

const wallEl = document.getElementById('wall');
const hudEl = document.getElementById('hud');
const toastEl = document.getElementById('toast');
const bannerEl = document.getElementById('banner');
const setupEl = document.getElementById('setup');
const clockEl = document.getElementById('clock');

let snapshot = null;
let lastFocusKey = '';
let socket = null;
let socketRetryMs = 1000;
let selectedIndex = 0;
const tiles = new Map();   // tile id -> TileView

// ---------------------------------------------------------------- helpers

function go2rtcBase() {
  const configured = params.get('go2rtc') || snapshot?.player?.go2rtc_url;
  return configured || `${location.protocol}//${location.hostname}:1984`;
}

// URL parameters are whatever the link said, so only known values are taken from them:
// `mode` once went straight into the status panel's HTML.
function choice(value, allowed) {
  return allowed.includes(value) ? value : '';
}

// `proxy` (default): video comes through the agent, which checks the token and the stream.
// `direct`: straight from go2rtc, which must then be reachable from this screen.
function transport() {
  return choice(params.get('transport'), ['proxy', 'direct']) || (params.get('go2rtc') ? 'direct' : '')
    || choice(snapshot?.player?.transport, ['proxy', 'direct']) || 'proxy';
}

function mediaUrl(stream) {
  if (transport() === 'direct') {
    return `${go2rtcBase().replace(/^http/, 'ws')}/api/ws?src=${encodeURIComponent(stream)}`;
  }
  const query = new URLSearchParams({ src: stream });
  if (API_TOKEN) query.set('token', API_TOKEN);
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  return `${scheme}://${location.host}/api/media/ws?${query}`;
}

function playerMode() {
  return choice(params.get('mode'), ['mse', 'mjpeg']) || choice(snapshot?.player?.mode, ['mse', 'mjpeg']) || 'mse';
}

function streamFor(name, mode) {
  return mode === 'mjpeg' ? `${name}_mjpeg` : name;
}

function send(msg) {
  if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(msg));
}

let toastTimer = null;
function toast(text) {
  toastEl.textContent = text;
  toastEl.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toastEl.hidden = true; }, 2000);
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

const DISPLAY_DEFAULTS = {
  fit: 'contain', show_labels: true, show_quality_badges: true, show_clock: false,
  offline_style: 'message', highlight_detections: true, hide_cursor_seconds: 3,
};

function display() {
  return { ...DISPLAY_DEFAULTS, ...(snapshot?.display || {}) };
}

// Display settings are presentation only, so the renderer applies them itself.
// Only on a real change: this runs for every snapshot, and restarting the idle timer
// each time would stop the cursor ever hiding while detections keep the wall busy.
let appliedDisplay = '';
function applyDisplay() {
  const d = display();
  const key = JSON.stringify(d);
  if (key === appliedDisplay) return;
  appliedDisplay = key;
  document.documentElement.style.setProperty('--fit', d.fit);
  document.body.classList.toggle('no-labels', !d.show_labels);
  document.body.classList.toggle('no-badges', !d.show_quality_badges);
  document.body.classList.toggle('blank-offline', d.offline_style === 'blank');
  clockEl.hidden = !d.show_clock;
  restartIdleTimer();
}

const DETECTION_ORDER = ['person', 'vehicle', 'animal', 'face', 'motion'];
const DETECTION_LABEL = {
  person: 'Person', vehicle: 'Vehicle', animal: 'Animal', face: 'Face', motion: 'Motion',
};

// A detection currently decides what is on screen (not just a highlight).
function focusOnScreen() {
  return !!(snapshot && snapshot.focus && snapshot.focus.presentation !== 'highlight');
}

const REASON_TEXT = {
  empty: '',
  offline: 'Camera offline',
  hidden: '',
};

// ---------------------------------------------------------------- tile view

class TileView {
  constructor(id) {
    this.id = id;
    this.stream = null;
    this.epoch = 0;
    this.current = null;   // StreamPlayer on screen
    this.pending = null;   // StreamPlayer warming up to replace `current`
    this.swapTimer = null;
    this.stallChecks = 0;

    this.root = el('div', 'tile');
    this.root.tabIndex = -1;
    this.root.dataset.tile = id;
    this.placeholder = el('div', 'placeholder');
    this.label = el('div', 'label');
    this.dot = el('span', 'dot');
    this.name = el('span', 'name');
    this.badge = el('span', 'badge');
    // The detection badge sits beside HD/SD so the two read the same way.
    this.detection = el('span', 'badge detection');
    this.detection.hidden = true;
    this.label.append(this.dot, this.name, this.badge, this.detection);
    this.focusNote = el('div', 'focus-note');
    this.root.append(this.placeholder, this.label, this.focusNote);
    this.root.addEventListener('click', () => {
      if (!snapshot) return;
      if (focusOnScreen()) { send({ type: 'focus', action: 'dismiss' }); return; }
      send({ type: 'fullscreen', tile: snapshot.fullscreen === this.id ? null : this.id });
    });
  }

  update(tile, mode) {
    const r = this.root;
    r.style.gridColumn = `${tile.x + 1} / span ${tile.w}`;
    r.style.gridRow = `${tile.y + 1} / span ${tile.h}`;
    r.classList.toggle('fullscreen', snapshot.fullscreen === tile.id);
    // A camera's own picture fit; without one the tile inherits the display setting.
    if (tile.fit) r.style.setProperty('--fit', tile.fit);
    else r.style.removeProperty('--fit');

    const cam = tile.camera;
    this.name.textContent = cam ? cam.name : '';
    this.badge.textContent = tile.quality === 'main' ? 'HD' : tile.quality === 'sub' ? 'SD' : '';
    this.badge.hidden = !tile.quality;
    this.badge.classList.toggle('main', tile.quality === 'main');
    this.reason = tile.reason;

    // What the camera sees right now, most important first.
    const seen = display().highlight_detections
      ? DETECTION_ORDER.filter((d) => (tile.detections || []).includes(d)) : [];
    r.classList.toggle('detected', seen.length > 0);
    for (const d of DETECTION_ORDER) r.classList.toggle(d, seen[0] === d);
    this.detection.hidden = seen.length === 0;
    this.detection.textContent = seen.length ? DETECTION_LABEL[seen[0]] : '';
    this.detection.classList.toggle('alert', seen[0] === 'person' || seen[0] === 'vehicle');
    // A detection badge is worth seeing even when camera names are switched off.
    this.label.hidden = !cam || (!display().show_labels && seen.length === 0);
    r.classList.toggle('focused', !!tile.focus && !!snapshot.focus && snapshot.focus.presentation !== 'highlight');
    this.focusNote.textContent = tile.focus && snapshot.focus
      ? `${DETECTION_LABEL[snapshot.focus.reason] || 'Detection'} · click or Esc to go back` : '';

    const wanted = tile.stream ? streamFor(tile.stream, mode) : null;
    // A changed epoch means the same stream now has a different upstream (a transport
    // fallback), so reconnect even though the name is the same.
    if (wanted !== this.stream || mode !== this.mode || tile.epoch !== this.epoch) {
      this.stream = wanted;
      this.mode = mode;
      this.epoch = tile.epoch;
      this.switchTo(wanted, mode);
    }
    this.render();
  }

  switchTo(stream, mode) {
    clearTimeout(this.swapTimer);
    if (this.pending) { this.pending.stop(); this.pending = null; }
    if (!stream) {
      if (this.current) { this.current.stop(); this.current = null; }
      return;
    }
    const player = new StreamPlayer({
      stream, mode, url: mediaUrl, onChange: (p) => this.onPlayerChange(p),
    });
    if (!this.current || this.current.state !== 'playing') {
      // Nothing worth keeping on screen: show the new stream straight away.
      if (this.current) this.current.stop();
      this.current = player;
      this.root.append(player.element);
    } else {
      // Keep the old picture until the new stream shows its first frame (no black flash).
      this.pending = player;
      player.element.classList.add('pending');
      this.root.insertBefore(player.element, this.label);
      this.swapTimer = setTimeout(() => this.promote(), SWAP_TIMEOUT_MS);
    }
    player.start();
  }

  promote() {
    if (!this.pending) return;
    clearTimeout(this.swapTimer);
    const old = this.current;
    this.current = this.pending;
    this.pending = null;
    this.current.element.classList.remove('pending');
    if (old) old.stop();
    this.render();
  }

  onPlayerChange(player) {
    if (player === this.pending && player.state === 'playing') this.promote();
    if (player === this.current) this.render();
    scheduleHud();
  }

  render() {
    const state = this.current ? this.current.state : 'idle';
    this.root.dataset.state = state;
    this.placeholder.replaceChildren();
    let text = '';
    if (!this.stream) {
      text = REASON_TEXT[this.reason] ?? (this.reason || '');
      if (this.reason && this.reason.startsWith('none:')) text = 'Stream limit reached';
    } else if (state === 'connecting') {
      this.placeholder.append(el('div', 'spinner'));
    } else if (state === 'error') {
      // Plain words on the TV: the detail is technical, and belongs in the HUD.
      text = 'Reconnecting…';
    }
    if (text) this.placeholder.append(el('div', '', text));
    this.placeholder.hidden = state === 'playing' && this.stream !== null;
  }

  // Called every 2 s by the watchdog.
  checkStall(visible) {
    const p = this.current;
    if (!p || p.state !== 'playing' || !visible) return;
    if (performance.now() - p.lastFrameAt > STALL_MS) p.restart('no frames');
  }

  /** The page itself stopped running: measure the next stall window from now. */
  excuseStall() {
    if (this.current) this.current.lastFrameAt = performance.now();
  }

  stats() {
    const out = { tile: this.id, reason: this.reason };
    if (this.current) Object.assign(out, this.current.stats());
    return out;
  }

  destroy() {
    clearTimeout(this.swapTimer);
    if (this.pending) this.pending.stop();
    if (this.current) this.current.stop();
    this.root.remove();
  }
}

// ---------------------------------------------------------------- rendering

/** A QR code from the agent's matrix, drawn rather than styled: no CSS, no dependency. */
function qrCanvas(matrix, scale) {
  const size = matrix.length;
  const quiet = 4;                       // the margin a reader needs around the code
  const canvas = document.createElement('canvas');
  canvas.width = (size + quiet * 2) * scale;
  canvas.height = canvas.width;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = '#000000';
  for (let y = 0; y < size; y += 1) {
    for (let x = 0; x < size; x += 1) {
      if (matrix[y][x]) ctx.fillRect((x + quiet) * scale, (y + quiet) * scale, scale, scale);
    }
  }
  return canvas;
}

function setupStep(number, title, lines, matrix) {
  const step = document.createElement('div');
  step.className = 'setup-step';
  const words = document.createElement('div');
  words.className = 'setup-words';
  const heading = document.createElement('h2');
  heading.textContent = `${number}. ${title}`;
  words.append(heading);
  for (const [label, value] of lines) {
    const row = document.createElement('p');
    row.className = 'setup-line';
    const name = document.createElement('span');
    name.className = 'setup-label';
    name.textContent = label;
    const text = document.createElement('span');
    text.className = 'setup-value';
    text.textContent = value;              // never innerHTML: this comes from the device
    row.append(name, text);
    words.append(row);
  }
  step.append(words);
  if (matrix && matrix.length) step.append(qrCanvas(matrix, 5));
  return step;
}

/** How to reach a device that has no network yet. Nothing else on the screen is any use. */
function renderSetup(setup) {
  const same = JSON.stringify(setup || null) === setupEl.dataset.shown;
  if (same) return;
  setupEl.dataset.shown = JSON.stringify(setup || null);
  setupEl.replaceChildren();
  setupEl.hidden = !setup;
  if (!setup) return;

  const head = document.createElement('header');
  const title = document.createElement('h1');
  title.textContent = 'Set up this screen';
  const hint = document.createElement('p');
  hint.textContent = 'This device has no network yet, so it is showing its own.';
  head.append(title, hint);
  setupEl.append(head);

  const steps = document.createElement('div');
  steps.className = 'setup-steps';
  steps.append(setupStep(1, 'Join this network', [['Network', setup.ssid],
                                                  ['Password', setup.password]],
                         setup.join_code));
  if (setup.url) {
    steps.append(setupStep(2, 'Open the settings', [['Address', setup.url.split('?')[0]]],
                           setup.url_code));
  }
  setupEl.append(steps);
}

function render(snap) {
  const previousView = snapshot?.view?.index;
  snapshot = snap;
  renderSetup(snap.setup);
  const { cols, rows } = snap.layout;
  wallEl.style.gridTemplateColumns = `repeat(${cols}, minmax(0, 1fr))`;
  wallEl.style.gridTemplateRows = `repeat(${rows}, minmax(0, 1fr))`;
  wallEl.classList.toggle('has-fullscreen', !!snap.fullscreen);
  document.title = `${snap.view.name} · ${snap.device.name}`;
  applyDisplay();

  const mode = playerMode();
  const seen = new Set();
  for (const tile of snap.tiles) {
    seen.add(tile.id);
    let view = tiles.get(tile.id);
    if (!view) {
      view = new TileView(tile.id);
      tiles.set(tile.id, view);
      wallEl.append(view.root);
    }
    view.update(tile, mode);
  }
  for (const [id, view] of tiles) {
    if (!seen.has(id)) { view.destroy(); tiles.delete(id); }
  }
  selectedIndex = Math.min(selectedIndex, snap.tiles.length - 1);
  markSelected();
  if (previousView !== undefined && previousView !== snap.view.index) toast(snap.view.name);
  const focusKey = snap.focus ? `${snap.focus.camera}/${snap.focus.reason}` : '';
  if (focusKey && focusKey !== lastFocusKey && snap.focus.presentation !== 'highlight') {
    toast(`${DETECTION_LABEL[snap.focus.reason] || 'Detection'} · ${snap.focus.name}`);
  }
  lastFocusKey = focusKey;
  renderHud();
}

function markSelected() {
  const ids = snapshot ? snapshot.tiles.map((t) => t.id) : [];
  for (const [id, view] of tiles) {
    view.root.classList.toggle('selected', !hudEl.hidden && ids[selectedIndex] === id);
  }
}

function renderHud() {
  if (hudEl.hidden || !snapshot) return;
  const b = snapshot.budget;
  const rows = snapshot.tiles.map((t) => {
    const s = tiles.get(t.id)?.stats() || {};
    const res = s.width ? `${s.width}×${s.height}` : '';
    const frames = s.frames ? `${s.frames}${s.dropped ? ` (${s.dropped} dropped)` : ''}` : '';
    const lat = s.latency_s !== undefined ? `${s.latency_s}s` : '';
    // Everything is escaped, including values that "can only be" safe: this is innerHTML.
    return `<tr><td>${escapeHtml(t.id)}</td><td>${t.camera ? escapeHtml(t.camera.name) : '—'}</td>`
      + `<td>${escapeHtml(t.quality || '—')}</td><td title="${escapeHtml(s.error || '')}">${escapeHtml(s.state || '')}`
      + `${s.error ? ` — ${escapeHtml(String(s.error).slice(0, 60))}` : ''}</td>`
      + `<td>${escapeHtml(res)}</td><td>${escapeHtml(frames)}</td><td>${escapeHtml(lat)}</td></tr>`;
  }).join('');
  hudEl.innerHTML = `
    <h2>${escapeHtml(snapshot.device.name)} · ${escapeHtml(snapshot.view.name)}
      (${snapshot.view.index + 1}/${snapshot.views.length}) · ${escapeHtml(snapshot.layout.id)}</h2>
    <div>Main streams ${escapeHtml(b.main_in_use)}/${escapeHtml(b.max_main)} · Streams ${escapeHtml(b.streams_in_use)}/${escapeHtml(b.max_total)}
      · Player ${escapeHtml(playerMode())} via ${transport() === 'direct' ? `go2rtc ${escapeHtml(go2rtcBase())}` : 'agent relay'}</div>
    <table><tr><th>Tile</th><th>Camera</th><th>Stream</th><th>State</th><th>Size</th><th>Frames</th><th>Delay</th></tr>${rows}</table>
    <div class="keys">Click or Enter: full screen · Esc: back · ←/→: select · [ ]: view · I: this panel</div>`;
}

let hudQueued = false;
function scheduleHud() {
  if (hudQueued || hudEl.hidden) return;
  hudQueued = true;
  requestAnimationFrame(() => { hudQueued = false; renderHud(); });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ---------------------------------------------------------------- agent connection

function connect() {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  const query = new URLSearchParams({ renderer: RENDERER_ID });
  if (API_TOKEN) query.set('token', API_TOKEN);
  const ws = new WebSocket(`${scheme}://${location.host}/api/wall/ws?${query}`);
  socket = ws;
  ws.addEventListener('open', () => {
    socketRetryMs = 1000;
    bannerEl.hidden = true;
  });
  ws.addEventListener('message', (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === 'wall') render(msg);
  });
  ws.addEventListener('close', (ev) => {
    if (socket !== ws) return;
    if (ev.code === 1008) {
      bannerEl.textContent = 'The viewport agent rejected this token. Check the ?token= in the URL.';
      bannerEl.hidden = false;
      socket = null;
      return;                       // retrying with the same bad token is pointless
    }
    bannerEl.textContent = 'Lost connection to the viewport agent. Reconnecting…';
    bannerEl.hidden = false;
    setTimeout(connect, socketRetryMs);
    socketRetryMs = Math.min(socketRetryMs * 2, 15000);
  });
}

// ---------------------------------------------------------------- input

document.addEventListener('keydown', (ev) => {
  if (!snapshot) return;
  const count = snapshot.tiles.length;
  const tileId = snapshot.tiles[selectedIndex]?.id;
  if (focusOnScreen() && ['Enter', 'f', 'Escape', 'Backspace'].includes(ev.key)) {
    send({ type: 'focus', action: 'dismiss' });   // back to the view, and let it be for a while
    ev.preventDefault();
    return;
  }
  switch (ev.key) {
    case 'Enter':
    case 'f':
      send({ type: 'fullscreen', tile: snapshot.fullscreen ? null : tileId });
      break;
    case 'Escape':
    case 'Backspace':
      send({ type: 'fullscreen', tile: null });
      break;
    case 'ArrowRight':
      selectedIndex = (selectedIndex + 1) % count;
      if (snapshot.fullscreen) send({ type: 'fullscreen', tile: snapshot.tiles[selectedIndex].id });
      break;
    case 'ArrowLeft':
      selectedIndex = (selectedIndex - 1 + count) % count;
      if (snapshot.fullscreen) send({ type: 'fullscreen', tile: snapshot.tiles[selectedIndex].id });
      break;
    case ']':
    case 'PageDown':
      send({ type: 'view', step: 1 });
      break;
    case '[':
    case 'PageUp':
      send({ type: 'view', step: -1 });
      break;
    case 'i':
      hudEl.hidden = !hudEl.hidden;
      renderHud();
      break;
    default:
      if (/^[1-9]$/.test(ev.key) && Number(ev.key) <= count) {
        selectedIndex = Number(ev.key) - 1;
        send({ type: 'fullscreen', tile: snapshot.tiles[selectedIndex].id });
      } else {
        return;
      }
  }
  ev.preventDefault();
  markSelected();
});

let idleTimer = null;
function restartIdleTimer() {
  const seconds = display().hide_cursor_seconds;
  clearTimeout(idleTimer);
  document.body.classList.remove('idle');
  if (seconds > 0) idleTimer = setTimeout(() => document.body.classList.add('idle'), seconds * 1000);
}
document.addEventListener('pointermove', restartIdleTimer);

setInterval(() => {
  if (clockEl.hidden) return;
  clockEl.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}, 1000);

// ---------------------------------------------------------------- watchdog + stats

// A blocked main thread, an occluded window or a sleeping machine freezes these timers
// and the video's own frame events alike, so every tile looks stalled at once and they
// all reconnect together — which costs far more than the pause did, a 4K stream most of
// all. If our own tick was late, the page stopped running: excuse the tiles this round.
let lastTick = performance.now();
setInterval(() => {
  const now = performance.now();
  const ranLate = now - lastTick > WATCHDOG_MS * 2;
  lastTick = now;
  if (!snapshot) return;
  for (const t of snapshot.tiles) {
    const tile = tiles.get(t.id);
    if (ranLate) tile?.excuseStall();
    else tile?.checkStall(t.visible && !document.hidden);
  }
  renderHud();
}, WATCHDOG_MS);

setInterval(() => {
  send({ type: 'stats', user_agent: navigator.userAgent, tiles: [...tiles.values()].map((v) => v.stats()) });
}, STATS_EVERY_MS);

if (params.get('hud') === '1') hudEl.hidden = false;
connect();

// Exposed for automated checks and debugging in the console.
window.viewport = {
  get snapshot() { return snapshot; },
  stats: () => [...tiles.values()].map((v) => v.stats()),
};
