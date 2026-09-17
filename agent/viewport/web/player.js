// Minimal go2rtc live player (MSE over WebSocket, with an MJPEG fallback).
// Written for the wall: no controls, muted, low latency, and it reports its own health.

import { redact } from './redact.js';

const CODECS = ['avc1.640029', 'avc1.64002A', 'avc1.640033', 'hvc1.1.6.L153.B0'];
const MAX_LATENCY_S = 2.0;
const RETRY_MIN_MS = 1000;
const RETRY_MAX_MS = 30000;

function mediaSourceClass() {
  return window.ManagedMediaSource || window.MediaSource || null;
}

export function browserCodecs(list = CODECS) {
  const MS = mediaSourceClass();
  if (!MS) return '';
  return list.filter((c) => MS.isTypeSupported(`video/mp4; codecs="${c}"`)).join(',');
}

export class StreamPlayer {
  /**
   * @param {object} opts
   * @param {string} opts.baseUrl  go2rtc base URL (http[s]://host:port)
   * @param {string} opts.stream   go2rtc stream name
   * @param {'mse'|'mjpeg'} opts.mode
   * @param {(p: StreamPlayer) => void} [opts.onChange]
   * @param {string[]} [opts.codecs]  codec list to offer (tests only)
   * @param {(stream: string) => string} [opts.url]  builds the WebSocket URL; by default
   *   go2rtc's own /api/ws under baseUrl (the wall passes the agent's relay instead)
   */
  constructor({ baseUrl, stream, mode, onChange, codecs, url }) {
    this.codecList = codecs || CODECS;
    this.baseUrl = (baseUrl || '').replace(/\/$/, '');
    this.buildUrl = url
      || ((name) => `${this.baseUrl.replace(/^http/, 'ws')}/api/ws?src=${encodeURIComponent(name)}`);
    this.stream = stream;
    this.mode = mode;
    this.onChange = onChange || (() => {});
    this.state = 'idle';        // idle | connecting | playing | error | stopped
    this.error = '';
    this.firstFrame = false;
    this.lastFrameAt = 0;
    this.retryMs = RETRY_MIN_MS;
    this.mjpegFrames = 0;
    this._lastCurrentTime = -1;
    this._timer = null;
    this.element = this._createElement();
  }

  _createElement() {
    if (this.mode === 'mjpeg') {
      const img = document.createElement('img');
      img.className = 'media';
      img.alt = '';
      return img;
    }
    const video = document.createElement('video');
    video.className = 'media';
    video.muted = true;
    video.autoplay = true;
    video.playsInline = true;
    video.disablePictureInPicture = true;
    video.disableRemotePlayback = true;
    video.addEventListener('loadeddata', () => this._frame());
    video.addEventListener('timeupdate', () => {
      // readyState >= 2 means a decoded frame is actually available.
      if (video.readyState >= 2 && video.currentTime !== this._lastCurrentTime) {
        this._lastCurrentTime = video.currentTime;
        this._frame();
      }
    });
    return video;
  }

  _setState(state, error = '') {
    if (this.state === state && this.error === error) return;
    this.state = state;
    this.error = error;
    this.onChange(this);
  }

  _frame() {
    this.lastFrameAt = performance.now();
    if (!this.firstFrame) {
      this.firstFrame = true;
      this.retryMs = RETRY_MIN_MS;
      this._setState('playing');
    }
  }

  start() {
    if (this.state === 'stopped') return;
    this._teardown();
    this._setState('connecting');
    const ws = new WebSocket(this.buildUrl(this.stream));
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    ws.addEventListener('open', () => (this.mode === 'mjpeg' ? this._startMjpeg() : this._startMse()));
    ws.addEventListener('message', (ev) => this._onMessage(ev));
    ws.addEventListener('close', () => {
      if (this.ws === ws && this.state !== 'stopped') this._fail(this.error || 'connection closed');
    });
  }

  /** Reconnect now (used by the wall's stall watchdog). */
  restart(reason) {
    if (this.state === 'stopped') return;
    this._fail(reason || 'stalled');
  }

  stop() {
    this._setState('stopped');
    clearTimeout(this._timer);
    this._teardown();
    this.element.remove();
  }

  _fail(message) {
    if (this.state === 'stopped') return;
    this._teardown();
    // go2rtc's error text quotes the source URL, password included. Scrub it here so
    // nothing downstream (tile, HUD, stats sent to the agent) ever holds the raw text.
    this._setState('error', redact(message));
    clearTimeout(this._timer);
    this._timer = setTimeout(() => this.start(), this.retryMs);
    this.retryMs = Math.min(this.retryMs * 2, RETRY_MAX_MS);
  }

  _teardown() {
    this.firstFrame = false;
    this._lastCurrentTime = -1;
    if (this.ws) {
      const ws = this.ws;
      this.ws = null;
      try { ws.close(); } catch (_) { /* already closed */ }
    }
    this.sb = null;
    this.queue = [];
    if (this.mode === 'mjpeg') {
      if (this._imgUrl) URL.revokeObjectURL(this._imgUrl);
      this._imgUrl = null;
    } else if (this.element) {
      const video = this.element;
      if (this._msUrl) URL.revokeObjectURL(this._msUrl);
      this._msUrl = null;
      video.removeAttribute('src');
      video.srcObject = null;
      try { video.load(); } catch (_) { /* ignore */ }
    }
  }

  _send(msg) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(msg));
  }

  _startMjpeg() {
    this._send({ type: 'mjpeg' });
  }

  _startMse() {
    const MS = mediaSourceClass();
    const codecs = browserCodecs(this.codecList);
    if (!MS || !codecs) {
      this._fail('this browser cannot play H.264/H.265 (try ?mode=mjpeg)');
      return;
    }
    const ms = new MS();
    const video = this.element;
    ms.addEventListener('sourceopen', () => this._send({ type: 'mse', value: codecs }), { once: true });
    if (window.ManagedMediaSource && MS === window.ManagedMediaSource) {
      video.srcObject = ms;
    } else {
      this._msUrl = URL.createObjectURL(ms);
      video.src = this._msUrl;
    }
    this.ms = ms;
    video.play().catch(() => {});
  }

  _onMessage(ev) {
    if (typeof ev.data === 'string') {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }
      if (msg.type === 'mse') this._addSourceBuffer(msg.value);
      else if (msg.type === 'error') this._fail(msg.value || 'go2rtc error');
      return;
    }
    if (this.mode === 'mjpeg') this._onJpeg(ev.data);
    else this._onSegment(ev.data);
  }

  _onJpeg(data) {
    const url = URL.createObjectURL(new Blob([data], { type: 'image/jpeg' }));
    const old = this._imgUrl;
    this._imgUrl = url;
    this.element.src = url;
    if (old) URL.revokeObjectURL(old);
    this.mjpegFrames += 1;
    this._frame();
  }

  _addSourceBuffer(mime) {
    try {
      const sb = this.ms.addSourceBuffer(mime);
      sb.mode = 'segments';
      sb.addEventListener('updateend', () => this._onUpdateEnd());
      this.sb = sb;
      this.codec = mime;
    } catch (e) {
      this._fail(`unsupported stream: ${mime}`);
    }
  }

  _onSegment(buf) {
    if (!this.sb) return;
    if (this.sb.updating || this.queue.length) this.queue.push(buf);
    else this._append(buf);
  }

  _append(buf) {
    try {
      this.sb.appendBuffer(buf);
    } catch (e) {
      this._fail(`buffer error: ${e.name}`);
    }
  }

  _onUpdateEnd() {
    const sb = this.sb;
    if (!sb || sb.updating) return;
    if (this.queue.length) {
      const size = this.queue.reduce((n, b) => n + b.byteLength, 0);
      const joined = new Uint8Array(size);
      let offset = 0;
      for (const b of this.queue) { joined.set(new Uint8Array(b), offset); offset += b.byteLength; }
      this.queue = [];
      this._append(joined);
      return;
    }
    this._manageBuffer();
  }

  // Keep playback close to live and the buffer small.
  _manageBuffer() {
    const sb = this.sb;
    const video = this.element;
    if (!sb.buffered.length) return;
    const start = sb.buffered.start(0);
    const end = sb.buffered.end(sb.buffered.length - 1);
    if (video.currentTime < start) video.currentTime = start;
    const gap = end - video.currentTime;
    if (gap > MAX_LATENCY_S) {
      video.currentTime = Math.max(start, end - 0.3);   // fell too far behind: jump to live
    } else if (gap > 1.0) {
      video.playbackRate = 1.05;                        // drift back gently (not noticeable)
    } else if (gap < 0.5) {
      video.playbackRate = 1.0;
    }
    if (video.paused) video.play().catch(() => {});
    if (end - start > 15 && video.currentTime - start > 10) {
      try { sb.remove(start, video.currentTime - 5); } catch (_) { /* ignore */ }
    }
  }

  stats() {
    const out = { stream: this.stream, mode: this.mode, state: this.state, error: this.error || undefined };
    if (this.mode === 'mjpeg') {
      out.frames = this.mjpegFrames;
      out.width = this.element.naturalWidth;
      out.height = this.element.naturalHeight;
      return out;
    }
    const video = this.element;
    out.width = video.videoWidth;
    out.height = video.videoHeight;
    out.codec = this.codec;
    if (video.getVideoPlaybackQuality) {
      const q = video.getVideoPlaybackQuality();
      out.frames = q.totalVideoFrames;
      out.dropped = q.droppedVideoFrames;
    }
    if (this.sb && this.sb.buffered.length) {
      out.latency_s = +(this.sb.buffered.end(this.sb.buffered.length - 1) - video.currentTime).toFixed(2);
    }
    return out;
  }
}
