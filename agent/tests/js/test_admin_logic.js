// Runs in the `lint` service: node agent/tests/js/test_admin_logic.js
// The admin page's rules that do not need a browser.
import assert from 'node:assert/strict';
import {
  camerasDraft, cellOwners, clone, copyShape, describeStream, freeLayoutId, hdOverBudget,
  keyframeAdvice, mergeRect, normaliseLayout, passwordProblem, resizeLayout, resolutionLabel, same,
  splitTile, tileAt, toggleCamera, viewsToSave,
} from '../../viewport/web/admin-logic.js';

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

function grid(cols, rows) {
  const layout = { id: 'mine-1', name: 'Mine', cols, rows, tiles: [] };
  resizeLayout(layout, cols, rows);
  return layout;
}

/** Every cell covered exactly once: the rule the agent enforces on save. */
function assertTiled(layout) {
  const seen = new Set();
  for (const t of layout.tiles) {
    for (let x = t.x; x < t.x + t.w; x += 1) {
      for (let y = t.y; y < t.y + t.h; y += 1) {
        assert.ok(x < layout.cols && y < layout.rows, `tile off the grid at ${x},${y}`);
        assert.ok(!seen.has(`${x},${y}`), `overlap at ${x},${y}`);
        seen.add(`${x},${y}`);
      }
    }
  }
  assert.equal(seen.size, layout.cols * layout.rows, 'every cell covered');
}

test('a new grid is all single cells', () => {
  const layout = grid(3, 2);
  assert.equal(layout.tiles.length, 6);
  assertTiled(layout);
});

test('dragging across cells joins them into one tile, however the drag ran', () => {
  const layout = grid(3, 2);
  mergeRect(layout, 1, 1, 0, 0);            // dragged up and left
  assert.deepEqual(tileAt(layout, 0, 0), { x: 0, y: 0, w: 2, h: 2 });
  assert.equal(layout.tiles.length, 3);
  assertTiled(layout);
});

test('a 1+x layout can be built: one big tile over a row of small ones', () => {
  const layout = grid(3, 2);
  mergeRect(layout, 0, 0, 2, 0);
  assert.deepEqual(layout.tiles.map((t) => [t.x, t.y, t.w, t.h]),
                   [[0, 0, 3, 1], [0, 1, 1, 1], [1, 1, 1, 1], [2, 1, 1, 1]]);
});

test('joining over part of a big tile breaks the rest of it back into cells', () => {
  const layout = grid(3, 3);
  mergeRect(layout, 0, 0, 1, 1);
  mergeRect(layout, 1, 1, 2, 2);
  assert.deepEqual(tileAt(layout, 1, 1), { x: 1, y: 1, w: 2, h: 2 });
  assert.equal(tileAt(layout, 0, 0).w, 1);
  assertTiled(layout);
});

test('splitting a tile gives back single cells', () => {
  const layout = grid(2, 2);
  mergeRect(layout, 0, 0, 1, 1);
  splitTile(layout, layout.tiles[0]);
  assert.equal(layout.tiles.length, 4);
  assertTiled(layout);
});

test('shrinking the grid keeps the tiles that still fit', () => {
  const layout = grid(4, 3);
  mergeRect(layout, 0, 0, 1, 1);
  mergeRect(layout, 2, 2, 3, 2);            // goes when the grid loses a column
  resizeLayout(layout, 3, 3);
  assert.deepEqual(tileAt(layout, 0, 0), { x: 0, y: 0, w: 2, h: 2 });
  assert.ok(layout.tiles.every((t) => t.w === 1 || (t.x === 0 && t.y === 0)));
  assertTiled(layout);
});

test("copying a built-in layout's shape replaces the grid", () => {
  const layout = grid(2, 2);
  copyShape(layout, { cols: 3, rows: 3, tiles: [{ x: 0, y: 0, w: 2, h: 2 }, { x: 2, y: 0, w: 1, h: 1 },
    { x: 2, y: 1, w: 1, h: 1 }, { x: 0, y: 2, w: 1, h: 1 }, { x: 1, y: 2, w: 1, h: 1 },
    { x: 2, y: 2, w: 1, h: 1 }] });
  assert.equal(layout.tiles.length, 6);
  assertTiled(layout);
});

test('cellOwners names the tile under every cell', () => {
  const layout = grid(2, 1);
  mergeRect(layout, 0, 0, 1, 0);
  assert.deepEqual([...cellOwners(layout)], [['0,0', 0], ['1,0', 0]]);
});

test('an untouched layout from the agent is not "unsaved"', () => {
  const fromAgent = { id: 'mine-1', name: 'Mine', cols: 2, rows: 1,
                      tiles: [{ x: 1, y: 0, w: 1, h: 1 }, { x: 0, y: 0, w: 1, h: 1 }] };
  const saved = normaliseLayout(fromAgent);
  const draft = clone(saved);
  assert.ok(same(draft, saved));
  mergeRect(draft, 0, 0, 1, 0);
  assert.ok(!same(draft, saved));
});

test('unticking and re-ticking a camera keeps an offline camera in the list', () => {
  let list = ['nvr:3', 'gone:1', 'nvr:0'];
  list = toggleCamera(list, 'nvr:3', false);
  list = toggleCamera(list, 'nvr:3', true);
  assert.deepEqual(list, ['gone:1', 'nvr:0', 'nvr:3']);
  assert.ok(list.includes('gone:1'));
});

test('a view with no cameras chosen is refused before saving', () => {
  assert.match(viewsToSave([{ name: 'Back', layout: 'auto', cameras: [] }]).error, /Back/);
  const ok = viewsToSave([{ name: '  ', layout: '2x2', cameras: 'all' }]);
  assert.deepEqual(ok.views, [{ name: 'Untitled view', layout: '2x2', cameras: 'all' }]);
});

test('a new layout gets an id nobody has', () => {
  assert.deepEqual(freeLayoutId([{ id: 'mine-2' }]), { id: 'mine-1', n: 1 });
  assert.deepEqual(freeLayoutId([{ id: 'mine-1' }, { id: 'mine-2' }]), { id: 'mine-3', n: 3 });
});

test('drafts compare equal whatever order their keys were added in', () => {
  assert.ok(same({ a: 1, b: { c: 2, d: 3 } }, { b: { d: 3, c: 2 }, a: 1 }));
  assert.ok(!same({ a: [1, 2] }, { a: [2, 1] }));          // but order in a list matters
});

const CONFIG = {
  cameras: [
    { id: 'nvr:0', settings: { name: 'Gate', show: true, quality: 'main', fit: 'default' } },
    { id: 'nvr:3', settings: { name: '', show: true, quality: 'auto', fit: 'default' } },
  ],
  camera_settings: { 'nvr:0': { name: 'Gate', quality: 'main' }, 'gone:1': { show: false } },
};

test("the cameras draft keeps the settings of a camera that is not found right now", () => {
  const draft = camerasDraft(CONFIG);
  assert.deepEqual(draft.order, ['nvr:0', 'nvr:3']);
  assert.equal(draft.settings['gone:1'].show, false);
  assert.equal(draft.settings['gone:1'].quality, 'auto');   // defaults filled in
  assert.ok(same(camerasDraft(CONFIG), draft));             // built twice, still "saved"
});

test('more cameras on Always HD than the device plays at once is noticed', () => {
  const draft = camerasDraft(CONFIG);
  assert.equal(hdOverBudget(draft, 1), 0);
  draft.settings['nvr:3'].quality = 'main';
  assert.equal(hdOverBudget(draft, 1), 1);
  draft.settings['nvr:3'].show = false;                      // hidden cameras play nothing
  assert.equal(hdOverBudget(draft, 1), 0);
});

test('a stream is described in words a person can read', () => {
  assert.equal(describeStream({ width: 3840, height: 2160, codec: 'h265', fps: 25, bitrate_kbps: 6144, keyframe_seconds: 2 }),
               '3840×2160 · H.265 · 25 fps · 6 Mbit/s · keyframe every 2 s');
  assert.equal(describeStream({ width: 640, height: 360, bitrate_kbps: 256 }), '640×360 · 256 kbit/s');
  assert.equal(describeStream(null), '');
});

test('resolutions read the way people say them', () => {
  assert.equal(resolutionLabel({ width: 3840, height: 2160 }), '4K');
  assert.equal(resolutionLabel({ width: 2560, height: 1920 }), '5 MP');
  assert.equal(resolutionLabel({ width: 1920, height: 1080 }), '1080p');
  assert.equal(resolutionLabel({}), '');
});

test('slow switching to HD is explained only when the keyframe interval makes it slow', () => {
  assert.match(keyframeAdvice({ keyframe_seconds: 2 }), /2\.5 s.*1×/);
  assert.equal(keyframeAdvice({ keyframe_seconds: 1 }), null);
  assert.equal(keyframeAdvice({}), null);
});

test('a new password must be long enough and typed the same twice', () => {
  assert.match(passwordProblem('short', 'short'), /at least 8/);
  assert.match(passwordProblem('long enough', 'long enougH'), /different/);
  assert.equal(passwordProblem('long enough', 'long enough'), null);
});

let failed = 0;
for (const [name, fn] of tests) {
  try {
    fn();
  } catch (err) {
    failed += 1;
    console.error(`FAIL ${name}\n  ${err.message}`);
  }
}
if (failed) {
  console.error(`${failed} of ${tests.length} admin logic tests failed`);
  process.exit(1);
}
console.log(`JS admin logic OK: ${tests.length} tests`);
