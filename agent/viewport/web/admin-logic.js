// The admin page's rules that do not touch the page: editing a layout's grid, and telling
// whether a section has unsaved changes. Kept apart so they can be tested in node
// (agent/tests/js/test_admin_logic.js, run by the `lint` service).

// ---------------------------------------------------------------- drafts

export function clone(value) {
  return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
}

/** JSON with object keys sorted, so the order properties were added in does not matter. */
function stable(value) {
  if (Array.isArray(value)) return `[${value.map(stable).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((k) => `${JSON.stringify(k)}:${stable(value[k])}`).join(',')}}`;
  }
  return JSON.stringify(value === undefined ? null : value);
}

/** Equal as saved: two drafts that would send the same JSON are the same. */
export function same(a, b) {
  return stable(a) === stable(b);
}

// ---------------------------------------------------------------- cameras

export const CAMERA_DEFAULTS = Object.freeze({ name: '', show: true, quality: 'auto', fit: 'default' });

/**
 * The Cameras section's draft: the order, and every camera's settings with the defaults
 * filled in. Settings for cameras not discovered right now are carried along, so saving
 * does not lose them.
 */
export function camerasDraft(config) {
  const settings = {};
  for (const [id, s] of Object.entries(config.camera_settings || {})) settings[id] = { ...CAMERA_DEFAULTS, ...s };
  for (const camera of config.cameras) settings[camera.id] = { ...CAMERA_DEFAULTS, ...(camera.settings || {}) };
  return { order: config.cameras.map((c) => c.id), settings };
}

/** How many shown cameras are set to Always HD beyond what the device will play at once. */
export function hdOverBudget(draft, maxMain) {
  const always = draft.order.filter((id) => {
    const s = draft.settings[id];
    return s && s.show && s.quality === 'main';
  }).length;
  return Math.max(0, always - maxMain);
}

/** A resolution the way people say it: "4K", "5 MP", "1080p". */
export function resolutionLabel(info) {
  if (!info || !info.width || !info.height) return '';
  if (info.width === 3840 && info.height === 2160) return '4K';
  const megapixels = (info.width * info.height) / 1e6;
  return megapixels >= 3 ? `${Math.round(megapixels)} MP` : `${info.height}p`;
}

function seconds(n) {
  return `${Number.isInteger(n) ? n : n.toFixed(1)} s`;
}

/** "3840×2160 · H.265 · 25 fps · 6 Mbit/s · keyframe every 2 s" */
export function describeStream(info) {
  if (!info) return '';
  const parts = [];
  if (info.width && info.height) parts.push(`${info.width}×${info.height}`);
  if (info.codec) parts.push({ h264: 'H.264', h265: 'H.265' }[info.codec] || info.codec.toUpperCase());
  if (info.fps) parts.push(`${info.fps} fps`);
  if (info.bitrate_kbps) {
    const mbit = info.bitrate_kbps / 1024;
    parts.push(mbit >= 1 ? `${Number.isInteger(mbit) ? mbit : mbit.toFixed(1)} Mbit/s` : `${info.bitrate_kbps} kbit/s`);
  }
  if (info.keyframe_seconds) parts.push(`keyframe every ${seconds(info.keyframe_seconds)}`);
  return parts.join(' · ');
}

/**
 * Advice when switching a camera to HD is slow: the player waits for the next keyframe,
 * so the NVR's I-frame interval is most of that wait. Null when there is nothing to say.
 */
export function keyframeAdvice(info) {
  if (!info || !info.keyframe_seconds || info.keyframe_seconds <= 1) return null;
  return `Switching to HD can take up to about ${seconds(info.keyframe_seconds + 0.5)}, most of it waiting `
    + `for a keyframe. An I-frame interval of 1× in the NVR's encoding settings makes it faster, `
    + 'for a slightly higher bitrate.';
}

// ---------------------------------------------------------------- passwords

export const MIN_PASSWORD_LENGTH = 8;

/** What is wrong with a new password and its confirmation, or null. */
export function passwordProblem(password, confirm) {
  if (password.length < MIN_PASSWORD_LENGTH) return `Use at least ${MIN_PASSWORD_LENGTH} characters.`;
  if (password !== confirm) return 'The two passwords are different.';
  return null;
}

/** A layout as the agent stores it, so an untouched draft compares equal to it. */
export function normaliseLayout(layout) {
  const tiles = layout.tiles.map(({ x, y, w = 1, h = 1 }) => ({ x, y, w, h }));
  tiles.sort((a, b) => a.y - b.y || a.x - b.x);
  return { id: layout.id, name: layout.name || '', cols: layout.cols, rows: layout.rows, tiles };
}

export function normaliseView(view) {
  return { name: view.name, layout: view.layout, cameras: view.cameras === 'all' ? 'all' : [...view.cameras] };
}

/**
 * Tick or untick one camera in a "which cameras" list, keeping the order it already has
 * and any ids that are not discovered right now (a camera may just be offline).
 */
export function toggleCamera(list, id, on) {
  const without = list.filter((c) => c !== id);
  return on ? [...without, id] : without;
}

/** A free id for a new layout of the user's own: "mine-2" may exist when there is one. */
export function freeLayoutId(layouts) {
  const taken = new Set(layouts.map((l) => l.id));
  let n = 1;
  while (taken.has(`mine-${n}`)) n += 1;
  return { id: `mine-${n}`, n };
}

/** Views as they will be sent, or the reason they cannot be. */
export function viewsToSave(views) {
  const cleaned = views.map((v) => ({
    name: (v.name || '').trim() || 'Untitled view',
    layout: v.layout,
    cameras: v.cameras === 'all' ? 'all' : [...v.cameras],
  }));
  const empty = cleaned.find((v) => v.cameras !== 'all' && v.cameras.length === 0);
  return empty ? { error: `"${empty.name}" has no cameras chosen.` } : { views: cleaned };
}

// ---------------------------------------------------------------- layout grid

/** Which tile covers each cell, as "x,y" -> tile index. */
export function cellOwners(layout) {
  const owner = new Map();
  layout.tiles.forEach((tile, index) => {
    for (let x = tile.x; x < tile.x + tile.w; x += 1) {
      for (let y = tile.y; y < tile.y + tile.h; y += 1) owner.set(`${x},${y}`, index);
    }
  });
  return owner;
}

/** Every cell belongs to exactly one tile: fill whatever is uncovered with single cells. */
export function fillGaps(layout) {
  const covered = cellOwners(layout);
  for (let y = 0; y < layout.rows; y += 1) {
    for (let x = 0; x < layout.cols; x += 1) {
      if (!covered.has(`${x},${y}`)) layout.tiles.push({ x, y, w: 1, h: 1 });
    }
  }
  layout.tiles.sort((a, b) => a.y - b.y || a.x - b.x);
}

/** Join a rectangle of cells into one tile. Tiles it overlaps are broken up. */
export function mergeRect(layout, x0, y0, x1, y1) {
  const [ax, bx] = [Math.min(x0, x1), Math.max(x0, x1)];
  const [ay, by] = [Math.min(y0, y1), Math.max(y0, y1)];
  const overlaps = (t) => t.x + t.w > ax && t.x <= bx && t.y + t.h > ay && t.y <= by;
  layout.tiles = layout.tiles.filter((t) => !overlaps(t));
  layout.tiles.push({ x: ax, y: ay, w: bx - ax + 1, h: by - ay + 1 });
  fillGaps(layout);
}

export function splitTile(layout, tile) {
  mergeRect(layout, tile.x, tile.y, tile.x, tile.y);
}

/** Change the grid, keeping every tile that still fits. */
export function resizeLayout(layout, cols, rows) {
  layout.tiles = layout.tiles.filter((t) => t.x + t.w <= cols && t.y + t.h <= rows);
  layout.cols = cols;
  layout.rows = rows;
  fillGaps(layout);
}

/** Copy a built-in layout's shape as a starting point. */
export function copyShape(layout, preset) {
  layout.cols = preset.cols;
  layout.rows = preset.rows;
  layout.tiles = preset.tiles.map((t) => ({ x: t.x, y: t.y, w: t.w, h: t.h }));
}

/** The tile whose top-left corner is at a cell, if any. */
export function tileAt(layout, x, y) {
  return layout.tiles.find((t) => t.x === x && t.y === y) || null;
}
