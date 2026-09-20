// Admin page. Plain ES modules, no build step, same as the wall.
//
// * Anyone who may watch the wall can read the settings; changing them needs the admin
//   password. Until someone unlocks the page it shows everything locked. With no password
//   set, it says so in the header and offers to set one (Security).
// * Every section except Sources and Security edits a draft. The save bar appears while the
//   section on screen differs from what the agent has, and saving sends that section alone,
//   so unsaved edits elsewhere survive it. A source, and the password, save on their own.
// * A first run is walked through the essentials by the setup guide, which can be started
//   again from This device.

import {
  CAMERA_DEFAULTS, camerasDraft, cellOwners, clone, copyShape, describeStream, freeLayoutId,
  hdOverBudget, keyframeAdvice, mergeRect, normaliseLayout, normaliseView, passwordProblem,
  resizeLayout, resolutionLabel, same, splitTile, tileAt, toggleCamera, viewsToSave,
} from './admin-logic.js';
import {
  binder, el, emptyNote, fieldsValid, group, icon, moveButtons, notice, numberOf, pressable,
  refocus, rowsOf, selectOf, setting, switchOf, textOf, withUnit,
} from './admin-ui.js';

const params = new URLSearchParams(location.search);
const TOKEN = params.get('token') || '';

const SECTIONS = [
  { id: 'general', title: 'General', hint: 'This device and its network' },
  { id: 'sources', title: 'Sources', hint: 'Where cameras come from' },
  { id: 'cameras', title: 'Cameras', hint: 'Names, quality and order' },
  { id: 'views', title: 'Views', hint: 'What the wall shows' },
  { id: 'layouts', title: 'Layouts', hint: 'Tile arrangements' },
  { id: 'display', title: 'Display', hint: 'How the wall looks' },
  { id: 'detection', title: 'Detection', hint: 'When a camera sees something' },
  { id: 'device', title: 'This device', hint: 'Streams and limits' },
  { id: 'security', title: 'Security', hint: 'Password and access' },
];
// The setup guide: the order someone setting up for the first time should work in.
const GUIDE = ['welcome', 'security', 'sources', 'cameras', 'views', 'done'];
const PANELS = ['welcome', ...SECTIONS.map((s) => s.id), 'done'];
const DRAFTED = ['cameras', 'layouts', 'views', 'display', 'detection', 'device'];
const GUIDE_KEY = 'viewport-admin-guide';

const LAYOUT_TEXT = { auto: 'Automatic grid', 'auto-feature': 'Automatic, one big tile' };
const DETECTION_TEXT = {
  person: 'Person', vehicle: 'Vehicle', animal: 'Animal', face: 'Face', motion: 'Any motion',
};
const TYPE_TEXT = { reolink: 'Reolink', onvif: 'ONVIF', rtsp: 'Stream addresses' };
const QUALITY_HELP = {
  auto: 'HD when its tile is big enough (see This device), SD otherwise.',
  main: 'HD in any tile, while this screen has an HD stream to spare. Going full screen is instant.',
  sub: 'Never HD, not even full screen. The lightest on the NVR.',
};

const $ = (id) => document.getElementById(id);

let config = null;       // GET /api/config
let session = null;      // GET /api/admin/session
const saved = {};        // section -> what the agent has
const draft = {};        // section -> what is on screen
let current = SECTIONS[0].id;
let guideStep = -1;      // index into GUIDE while the setup guide is showing
let shown = false;       // the settings have been loaded and put on screen
let saving = false;
let saveError = '';

// ---------------------------------------------------------------- talking to the agent

function withToken(path) {
  return TOKEN ? `${path}${path.includes('?') ? '&' : '?'}token=${encodeURIComponent(TOKEN)}` : path;
}

function describeError(detail) {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    // FastAPI's validation errors: which field, and what is wrong with it.
    return detail.map((d) => `${(d.loc || []).filter((p) => p !== 'body').join(' → ')}: ${d.msg}`).join('; ');
  }
  return JSON.stringify(detail);
}

async function api(path, { method = 'GET', body } = {}) {
  const headers = {};
  if (TOKEN) headers.Authorization = `Bearer ${TOKEN}`;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const resp = await fetch(withToken(path), {
    method, headers, body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!resp.ok) {
    let detail = `${resp.status} ${resp.statusText}`;
    try {
      const data = await resp.json();
      if (data.detail) detail = describeError(data.detail);
    } catch (_) { /* not JSON */ }
    const error = new Error(detail);
    error.status = resp.status;
    throw error;
  }
  return resp.json();
}

// ---------------------------------------------------------------- feedback

function banner(text) {
  $('banner').textContent = text || '';
  $('banner').hidden = !text;
}

let toastTimer = null;
function toast(text, kind = 'ok') {
  const node = $('toast');
  node.textContent = text;
  node.className = kind === 'error' ? 'error' : '';
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, kind === 'error' ? 7000 : 2800);
}

function titleOf(section) {
  return (SECTIONS.find((s) => s.id === section) || { title: section }).title;
}

// ---------------------------------------------------------------- locked or not

function isLocked() {
  return !!config && config.locked;
}

function renderLock() {
  const button = $('lock-button');
  button.hidden = !session;
  if (!session) return;
  button.replaceChildren();
  button.className = '';
  if (!session.required) {
    button.classList.add('warn');
    button.append(icon('warning'), el('span', null, 'Not protected'));
    button.title = 'Anyone on your network can change these settings. Choose a password.';
    button.onclick = () => (guideStep >= 0 ? goGuide(GUIDE.indexOf('security')) : showSection('security'));
  } else if (isLocked()) {
    button.classList.add('locked');
    button.append(icon('lock'), el('span', null, 'Unlock'));
    button.title = 'Unlock to change settings';
    button.onclick = () => openUnlock();
  } else {
    button.classList.add('quiet');
    button.append(icon('unlock'), el('span', null, 'Lock'));
    button.title = 'Lock the settings again';
    button.onclick = () => lock();
  }
  $('lockable').disabled = isLocked();
}

let afterUnlock = null;

function openUnlock(reason = 'Enter the admin password to change settings.', then = null) {
  afterUnlock = then;
  $('unlock-reason').textContent = reason;
  $('unlock-user').value = (session && session.username) || 'admin';
  $('unlock-pass').value = '';
  $('unlock-error').hidden = true;
  const dialog = $('unlock-dialog');
  if (!dialog.open) dialog.showModal();
  $('unlock-pass').focus();
}

$('unlock-cancel').addEventListener('click', () => $('unlock-dialog').close());
$('unlock-dialog').addEventListener('close', () => { $('unlock-pass').value = ''; });

$('unlock-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const submit = $('unlock-submit');
  submit.disabled = true;
  try {
    await api('/api/admin/login', {
      method: 'POST', body: { username: $('unlock-user').value, password: $('unlock-pass').value },
    });
    $('unlock-dialog').close();
    const then = afterUnlock;
    afterUnlock = null;
    if (shown) await refresh();
    else await start();
    toast('Unlocked. Settings can be changed.');
    if (then) await then();
  } catch (err) {
    $('unlock-error').textContent = err.status === 401 ? 'Wrong username or password.' : err.message;
    $('unlock-error').hidden = false;
    $('unlock-pass').select();
  } finally {
    submit.disabled = false;
  }
});

async function lock() {
  if (DRAFTED.some(isDirty) && !window.confirm('Lock the settings and lose the changes you have not saved?')) return;
  await api('/api/admin/logout', { method: 'POST' }).catch(() => {});
  await refresh({ discardDrafts: true });
  toast('Locked.');
}

/** Read the session and the settings again: after unlocking, locking, or a new password. */
async function refresh({ discardDrafts = false } = {}) {
  session = await api('/api/admin/session');
  await refreshDevice();
  let next;
  try {
    next = await api('/api/config');
  } catch (err) {
    if (err.status === 401) {
      needsUnlockToView();
      return;
    }
    throw err;
  }
  load(next, { reset: discardDrafts ? DRAFTED : [] });
}

/** The API token is required and this page has none: only an admin session will do. */
function needsUnlockToView() {
  config = null;
  shown = false;
  $('shell').hidden = true;
  renderLock();
  renderBars();
  if (session && session.required) {
    banner('Unlock to see the settings.');
    openUnlock('Enter the admin password to see and change the settings.');
  } else {
    banner('This page needs the API token: open it as /admin?token=… (see Security in the docs).');
  }
}

// ---------------------------------------------------------------- drafts and saving

function isDirty(section) {
  return DRAFTED.includes(section) && section in saved && section in draft
    && !same(draft[section], saved[section]);
}

function changed() {
  if (saveError) saveError = '';
  renderBars();
  markTabs();
  renderGuide();
}

function visiblePanel() {
  return guideStep >= 0 ? GUIDE[guideStep] : current;
}

/** The bar at the bottom: the setup guide's buttons, "locked", or "unsaved changes". */
function renderBars() {
  const guide = guideStep >= 0 && shown;
  const locked = shown && isLocked();
  const dirty = shown && !locked && isDirty(visiblePanel());
  $('lockbar').hidden = guide || !locked;
  $('savebar').hidden = guide || !dirty;
  document.body.classList.toggle('has-lockbar', !guide && locked);
  document.body.classList.toggle('has-savebar', !guide && dirty);
  if (!dirty || guide) return;
  const text = $('savebar-text');
  text.className = '';
  if (!config.editable) {
    text.textContent = 'Read-only: changes here cannot be kept.';
    text.className = 'error';
  } else if (saveError) {
    text.textContent = saveError;
    text.className = 'error';
  } else {
    const others = DRAFTED.filter((s) => s !== visiblePanel() && isDirty(s)).length;
    text.textContent = others ? `Unsaved changes, and in ${others} other section${others > 1 ? 's' : ''}`
      : 'Unsaved changes';
  }
  $('save').disabled = saving || !config.editable;
  $('save').textContent = saving ? 'Saving…' : 'Save';
  $('discard').disabled = saving;
}

const SAVE = {
  cameras: () => api('/api/config/cameras', {
    method: 'PUT', body: { order: draft.cameras.order, settings: draft.cameras.settings },
  }),
  layouts: () => api('/api/config/layouts', { method: 'PUT', body: { layouts: draft.layouts } }),
  views: () => {
    const out = viewsToSave(draft.views);
    if (out.error) throw new Error(out.error);
    return api('/api/config/views', { method: 'PUT', body: { views: out.views } });
  },
  display: () => api('/api/config/display', { method: 'PUT', body: draft.display }),
  device: () => api('/api/config/device', { method: 'PUT', body: draft.device }),
  detection: () => {
    if (!draft.detection.triggers.length) throw new Error('Switch on at least one thing that counts.');
    return api('/api/config/detection', { method: 'PUT', body: draft.detection });
  },
};

async function saveSection(section) {
  if (!isDirty(section) || saving) return true;
  if (!fieldsValid($(`panel-${section}`))) return false;
  saving = true;
  saveError = '';
  renderBars();
  renderGuide();
  try {
    const next = await SAVE[section]();
    saving = false;
    load(next, { reset: [section] });
    toast(`${titleOf(section)} saved.`);
    return true;
  } catch (err) {
    saving = false;
    if (err.status === 401) {
      // The session ran out while someone was editing. Their draft survives the refresh.
      await refresh();
      openUnlock('Your session has ended. Unlock to save your changes.', () => saveSection(section));
      return false;
    }
    saveError = `Not saved: ${err.message}`;
    renderBars();
    renderGuide();
    if (guideStep >= 0) toast(saveError, 'error');
    return false;
  }
}

function discard(section) {
  draft[section] = clone(saved[section]);
  saveError = '';
  RENDER[section]();
  changed();
}

/** For a source or the password: run `work`, and if the session has ended, unlock and retry. */
async function guarded(work, what) {
  try {
    return await work();
  } catch (err) {
    if (err.status === 401 && session && session.required) {
      await refresh();
      openUnlock(`Your session has ended. Unlock to ${what}.`, () => guarded(work, what));
      return undefined;
    }
    throw err;
  }
}

/** New settings from the agent. Unsaved drafts survive, except the sections in `reset`. */
function load(next, { reset = [], render = true } = {}) {
  const keep = new Set(DRAFTED.filter((s) => isDirty(s) && !reset.includes(s)));
  config = next;
  saved.cameras = camerasDraft(config);
  saved.layouts = (config.custom_layouts || []).map(normaliseLayout);
  saved.views = config.views.map(normaliseView);
  saved.display = { ...config.display };
  saved.device = { ...config.device };
  saved.detection = {
    ...config.detection,
    triggers: [...config.detection.triggers],
    cameras: config.detection.cameras === 'all' ? 'all' : [...config.detection.cameras],
  };
  for (const section of DRAFTED) if (!keep.has(section)) draft[section] = clone(saved[section]);
  $('wall-link').href = withToken('/');
  $('done-wall').href = withToken('/');
  banner(config.editable ? ''
    : 'Read-only: no state file is configured, so changes cannot be kept (set VIEWPORT_STATE_FILE).');
  if (render) renderAll();
}

function renderAll() {
  renderTabs();
  for (const render of Object.values(RENDER)) render();
  renderLock();
  applyPanels();
}

// ---------------------------------------------------------------- sources

let addingSource = false;
let newSource = null;             // the form for a source being added, kept across redraws
const sourceEdits = {};           // source id -> fields changed but not saved

function renderSources() {
  const box = $('sources');
  box.replaceChildren();
  if (!config.sources.length && !addingSource) {
    box.append(emptyNote('No sources yet. Add your NVR or a camera to get started.'));
  }
  for (const source of config.sources) box.append(sourceCard(source));
  if (addingSource) box.append(newSourceCard());
  $('add-source').hidden = addingSource;
  $('add-source').disabled = !config.editable;
}

function sourceCard(source) {
  const edits = sourceEdits[source.id] || (sourceEdits[source.id] = {});
  const value = (key) => (key in edits ? edits[key] : source[key]);
  const card = el('article', 'card');
  card.dataset.source = source.id;

  const cameras = config.cameras.filter((c) => c.source === source.id);
  const offline = cameras.filter((c) => !c.online).length;
  const where = source.type === 'rtsp' ? '' : ` · ${source.host}${source.port ? `:${source.port}` : ''}`;
  const head = el('div', 'card-head');
  const title = el('div', 'card-title');
  title.append(el('h3', null, source.id), el('p', null, `${TYPE_TEXT[source.type] || source.type}${where}`));
  const tools = el('div', 'card-tools');
  tools.append(el('span', offline ? 'badge warn' : 'badge',
                  !cameras.length ? 'No cameras yet'
                    : offline ? `${offline} of ${cameras.length} offline`
                      : `${cameras.length} camera${cameras.length > 1 ? 's' : ''}`));
  head.append(title, tools);

  const save = el('button', 'primary', 'Save');
  save.type = 'button';
  const body = () => {
    const out = {};
    for (const key of ['protocol', 'protocol_fallback', 'refresh_seconds']) {
      if (key in edits && edits[key] !== source[key]) out[key] = edits[key];
    }
    // An empty username field leaves the saved one alone, as an empty password does.
    if (edits.username && edits.username !== source.username) out.username = edits.username;
    if (edits.password) out.password = edits.password;
    return out;
  };
  const refreshSave = () => { save.disabled = !config.editable || !Object.keys(body()).length; };
  const edit = (key, v) => { edits[key] = v; refreshSave(); };

  const username = textOf(isLocked() ? '' : value('username'), {
    autocomplete: 'off', placeholder: isLocked() && source.username_set ? 'Hidden while locked' : '',
  });
  username.addEventListener('input', () => edit('username', username.value));
  const password = textOf(edits.password, {
    type: 'password', autocomplete: 'new-password',
    placeholder: source.password_set ? 'Saved, not shown' : 'Not set',
  });
  password.addEventListener('input', () => edit('password', password.value));

  const rows = [
    setting({ label: 'Username', help: 'A view-only account on the NVR is enough.', control: username }),
    setting({ label: 'Password', control: password,
              help: source.password_set ? 'Leave empty to keep the saved one. Passwords are never shown.'
                : 'Not set yet.' }),
  ];

  const advanced = [];
  if (source.type === 'reolink') {
    const protocol = selectOf([['auto', 'Automatic (recommended)'], ['rtsp', 'RTSP'], ['flv', 'HTTP-FLV']],
                              value('protocol'));
    protocol.addEventListener('change', () => edit('protocol', protocol.value));
    const fallback = switchOf(value('protocol_fallback'));
    fallback.addEventListener('change', () => edit('protocol_fallback', fallback.checked));
    advanced.push(
      setting({ label: 'Stream protocol', control: protocol,
                info: 'Automatic uses HTTP-FLV for H.264 up to 5 MP and RTSP for H.265 or anything '
                  + 'bigger, as Reolink recommends. HTTP-FLV needs the NVR\'s HTTP port switched on.' }),
      setting({ label: 'Switch protocol if a stream keeps failing', control: fallback, fit: true,
                info: 'After a stream fails several times in a row it is retried over the other protocol.' }),
    );
  }
  const every = numberOf(value('refresh_seconds'), { min: 10, max: 3600, integer: true });
  every.addEventListener('input', () => edit('refresh_seconds', every.value === '' ? null : Number(every.value)));
  advanced.push(setting({ label: 'Check for new cameras every', control: withUnit(every, 's'), fit: true,
                          help: 'How often the NVR is asked for its list of cameras.' }));

  const foot = el('div', 'card-foot');
  if (source.declared) {
    foot.append(el('p', 'hint', 'Set in viewport.yaml or .env, so it is changed or removed there. '
      + 'A password saved here is used instead of the one in the environment.'));
  } else {
    const remove = el('button', 'danger', 'Remove source');
    remove.type = 'button';
    remove.disabled = !config.editable;
    remove.addEventListener('click', async () => {
      if (!window.confirm(`Remove ${source.id}? Its cameras leave the wall, and its saved password is deleted.`)) return;
      try {
        const next = await guarded(() => api(`/api/config/sources/${encodeURIComponent(source.id)}`,
                                             { method: 'DELETE' }), `remove ${source.id}`);
        if (!next) return;
        delete sourceEdits[source.id];
        load(next);
        toast(`Removed ${source.id}.`);
      } catch (err) {
        toast(`Could not remove ${source.id}: ${err.message}`, 'error');
      }
    });
    foot.append(remove);
  }
  save.addEventListener('click', async () => {
    if (!fieldsValid(card)) return;
    save.disabled = true;
    save.textContent = 'Saving…';
    try {
      const next = await guarded(() => api(`/api/config/sources/${encodeURIComponent(source.id)}`,
                                           { method: 'PUT', body: body() }), `save ${source.id}`);
      if (!next) return;
      delete sourceEdits[source.id];
      load(next);
      toast(`Saved ${source.id}.`);
    } catch (err) {
      toast(`Could not save ${source.id}: ${err.message}`, 'error');
      save.textContent = 'Save';
      refreshSave();
    }
  });
  foot.append(save);
  refreshSave();

  card.append(head, rowsOf(rows), group('Connection', advanced, { advanced: true }), foot);
  return card;
}

function newSourceCard() {
  const form = newSource || (newSource = {
    id: '', type: 'reolink', host: '', port: '', https: false, username: '', password: '', urls: '',
  });
  const card = el('article', 'card');
  card.id = 'new-source';
  const discardNew = () => {
    addingSource = false;
    newSource = null;
    renderSources();
    $('add-source').focus();
  };

  const head = el('div', 'card-head');
  const title = el('div', 'card-title');
  title.append(el('h3', null, 'New source'), el('p', null, 'Its cameras appear a few seconds after it is added.'));
  const close = el('button', 'icon-button quiet');
  close.type = 'button';
  close.title = 'Discard this new source';
  close.setAttribute('aria-label', 'Discard this new source');
  close.append(icon('close'));
  close.addEventListener('click', discardNew);
  const tools = el('div', 'card-tools');
  tools.append(close);
  head.append(title, tools);

  const bind = (input, key, event = 'input') => {
    input.addEventListener(event, () => { form[key] = input.type === 'checkbox' ? input.checked : input.value; });
    return input;
  };
  const id = bind(textOf(form.id, { placeholder: 'garage-nvr', maxLength: 32 }), 'id');
  id.required = true;
  id.pattern = '[a-z0-9][a-z0-9\\-]{0,31}';
  const type = bind(selectOf([['reolink', 'Reolink NVR or camera'], ['onvif', 'ONVIF: most other brands'],
                              ['rtsp', 'Stream addresses (RTSP)']], form.type), 'type', 'change');
  const host = bind(textOf(form.host, { placeholder: '192.168.1.50' }), 'host');
  const port = bind(numberOf(form.port, { min: 1, max: 65535, integer: true }), 'port');
  port.required = false;
  port.placeholder = 'Standard';
  const https = bind(switchOf(form.https), 'https', 'change');
  const username = bind(textOf(form.username), 'username');
  const password = bind(textOf(form.password, { type: 'password', autocomplete: 'new-password' }), 'password');
  const urls = bind(el('textarea'), 'urls');
  urls.rows = 4;
  urls.value = form.urls;
  urls.spellcheck = false;
  urls.placeholder = 'Front gate, rtsp://10.0.0.5/main, rtsp://10.0.0.5/sub';

  const hostRow = setting({ label: 'Address', help: 'Its IP address or host name.', control: host });
  const portRow = setting({ label: 'Port', help: 'Leave empty unless it was changed on the device.',
                            control: port, fit: true });
  const httpsRow = setting({ label: 'Use HTTPS', help: 'Only if its web page opens with https://.',
                             control: https, fit: true });
  const urlsRow = setting({
    label: 'Cameras', stacked: true, control: urls,
    help: 'One camera per line: a name, its stream address, and optionally a low-resolution one, separated by commas.',
  });
  const sync = () => {
    const rtsp = type.value === 'rtsp';
    hostRow.hidden = rtsp;
    portRow.hidden = rtsp;
    httpsRow.hidden = rtsp;
    urlsRow.hidden = !rtsp;
    host.required = !rtsp;
  };
  type.addEventListener('change', sync);
  sync();

  const cancel = el('button', 'quiet', 'Cancel');
  cancel.type = 'button';
  cancel.addEventListener('click', discardNew);
  const add = el('button', 'primary', 'Add source');
  add.type = 'button';
  add.disabled = !config.editable;
  add.addEventListener('click', async () => {
    if (!fieldsValid(card)) return;
    const body = { id: id.value.trim(), type: type.value, username: username.value, password: password.value };
    if (type.value === 'rtsp') {
      body.cameras_urls = urls.value.split('\n').map((line) => line.split(',').map((p) => p.trim()))
        .filter((parts) => parts[0] && parts[1])
        .map((parts) => ({ name: parts[0], main: parts[1], sub: parts[2] || '' }));
      if (!body.cameras_urls.length) {
        toast('Add at least one camera: a name, a comma, then its address.', 'error');
        urls.focus();
        return;
      }
    } else {
      body.host = host.value.trim();
      if (port.value) body.port = Number(port.value);
      body.https = https.checked;
    }
    add.disabled = true;
    add.textContent = 'Adding…';
    try {
      const next = await guarded(() => api('/api/config/sources', { method: 'POST', body }), 'add the source');
      if (!next) return;
      addingSource = false;
      newSource = null;
      load(next);
      if (next.cameras.some((c) => c.source === body.id)) {
        toast(`Added ${body.id}.`);
      } else {
        toast(`Added ${body.id}. Looking for its cameras…`);
        watchForCameras(body.id);
      }
    } catch (err) {
      toast(`Could not add the source: ${err.message}`, 'error');
    } finally {
      add.disabled = !config.editable;
      add.textContent = 'Add source';
    }
  });
  const foot = el('div', 'card-foot');
  foot.append(cancel, add);

  card.append(head, rowsOf([
    setting({ label: 'Name', help: 'A short name, like garage-nvr: lower-case letters, numbers and dashes.',
              control: id }),
    setting({ label: 'Type', control: type,
              info: 'Reolink devices are read through their own API, which also gives camera names and '
                + 'detections. ONVIF works with most other brands. Stream addresses are for anything else.' }),
    hostRow, portRow, httpsRow, urlsRow,
    setting({ label: 'Username', help: 'A view-only account is enough.', control: username }),
    setting({ label: 'Password', control: password }),
  ]), foot);
  return card;
}

/**
 * A source whose first look for cameras failed (the NVR was slow, or still starting) finds
 * them on a later try: keep checking for a couple of minutes, and show them without a reload.
 */
function watchForCameras(sourceId, tries = 0) {
  if (tries >= 30 || !config || !config.sources.some((s) => s.id === sourceId)) return;
  setTimeout(async () => {
    let next;
    try {
      next = await api('/api/config');
    } catch (_) {
      watchForCameras(sourceId, tries + 1);
      return;
    }
    load(next, { render: false });
    const active = document.activeElement;
    const typing = active && $('panels').contains(active) && active.matches('input, textarea, select');
    if (!typing && !addingSource) renderAll();
    if (!next.cameras.some((c) => c.source === sourceId)) watchForCameras(sourceId, tries + 1);
  }, 4000);
}

// ---------------------------------------------------------------- cameras

const openCameras = new Set();    // which camera cards are open (not part of any draft)

function hdStreams(n) {
  return n === 0 ? 'no HD streams' : `${n} HD stream${n > 1 ? 's' : ''}`;
}

function renderCameras() {
  const box = $('cameras');
  box.replaceChildren();
  if (!config.cameras.length) {
    box.append(emptyNote('No cameras yet. Add a source, and the cameras it finds appear here.'));
    return;
  }
  const d = draft.cameras;
  const byId = new Map(config.cameras.map((c) => [c.id, c]));
  // A camera found while this draft had unsaved changes joins at the bottom.
  for (const camera of config.cameras) {
    if (!d.order.includes(camera.id)) d.order.push(camera.id);
    if (!d.settings[camera.id]) d.settings[camera.id] = { ...CAMERA_DEFAULTS };
  }
  const order = d.order.filter((id) => byId.has(id));
  const maxMain = saved.device.max_main_streams;
  const over = hdOverBudget({ order, settings: d.settings }, maxMain);
  if (over > 0) {
    box.append(notice(`${over + maxMain} cameras are set to Always HD, but this screen plays ${hdStreams(maxMain)} `
      + 'at a time (This device). The biggest tile gets it; the others play SD.', 'warn'));
  }
  const list = el('ol', 'camera-list');
  order.forEach((id, index) => list.append(cameraItem(byId.get(id), d.settings[id], index, order)));
  box.append(list);
}

function fillBadges(box, camera, s) {
  box.replaceChildren();
  const add = (text, cls = '') => box.append(el('span', `badge ${cls}`, text));
  if (!camera.online) add('Offline', 'warn');
  if (!s.show) add('Not on the wall', 'off');
  if (s.quality === 'main') add('Always HD', 'hd');
  if (s.quality === 'sub') add('Always SD');
  if (s.fit === 'cover') add('Fills its tile');
  if (s.fit === 'contain') add('Whole picture');
  const renamed = s.name.trim() && s.name.trim() !== camera.reported_name;
  if (renamed) add(`NVR: ${camera.reported_name}`, 'off');
  if (!box.childElementCount && resolutionLabel(camera.main)) add(resolutionLabel(camera.main), 'off');
}

function cameraItem(camera, s, index, order) {
  const open = openCameras.has(camera.id);
  const item = el('li', open ? 'camera-item open' : 'camera-item');
  item.dataset.key = camera.id;

  const title = el('div', 'camera-title');
  const name = el('span', 'camera-name', s.name.trim() || camera.reported_name);
  const badges = el('div', 'camera-badges');
  fillBadges(badges, camera, s);
  title.append(name, badges);

  // Opening a camera only shows its settings, so it works while the page is locked too.
  const toggle = pressable(el('div', 'camera-toggle'), () => {
    if (openCameras.has(camera.id)) openCameras.delete(camera.id);
    else openCameras.add(camera.id);
    renderCameras();
    refocus($('cameras'), camera.id, '.camera-toggle');
  });
  toggle.setAttribute('aria-expanded', String(open));
  toggle.setAttribute('aria-label', `${name.textContent}: ${open ? 'hide' : 'show'} settings`);
  toggle.append(el('span', 'rank', String(index + 1)), title, icon('chevron'));

  const moves = moveButtons(index, order.length, name.textContent, (delta) => {
    const d = draft.cameras;
    const a = d.order.indexOf(camera.id);
    const b = d.order.indexOf(order[index + delta]);
    [d.order[a], d.order[b]] = [d.order[b], d.order[a]];
    renderCameras();
    refocus($('cameras'), camera.id, `[data-move="${delta}"]`, `[data-move="${-delta}"]`);
    changed();
  });
  const head = el('div', 'camera-head');
  head.append(toggle, moves);
  item.append(head);
  if (open) item.append(cameraBody(camera, s, name, badges));
  return item;
}

function cameraBody(camera, s, nameEl, badges) {
  const body = el('div', 'camera-body');
  const update = () => {
    nameEl.textContent = s.name.trim() || camera.reported_name;
    fillBadges(badges, camera, s);
    changed();
  };

  const name = textOf(s.name, { placeholder: camera.reported_name, maxLength: 48 });
  name.addEventListener('input', () => { s.name = name.value; update(); });
  const show = switchOf(s.show);
  show.addEventListener('change', () => { s.show = show.checked; update(); });
  const qualityHelp = el('span', null, QUALITY_HELP[s.quality]);
  const quality = selectOf([['auto', 'Automatic'], ['main', 'Always HD'], ['sub', 'Always SD']], s.quality);
  quality.addEventListener('change', () => {
    s.quality = quality.value;
    qualityHelp.textContent = QUALITY_HELP[s.quality];
    update();
  });
  const fit = selectOf([['default', 'As set under Display'], ['contain', 'Show the whole picture'],
                        ['cover', 'Fill the tile']], s.fit);
  fit.addEventListener('change', () => { s.fit = fit.value; update(); });

  body.append(rowsOf([
    setting({ label: 'Name on the wall', control: name,
              help: `Leave empty to use the NVR's name, “${camera.reported_name}”. Nothing is changed on the NVR.` }),
    setting({ label: 'Show on the wall', control: show, fit: true,
              help: 'Off keeps it out of every view, and its detections are ignored.' }),
    setting({ label: 'Video quality', help: qualityHelp, control: quality,
              info: 'HD is the camera\'s full-resolution stream and SD its low-resolution one. An NVR only '
                + 'allows a couple of HD viewers per camera, and the phone app needs one, so HD is '
                + 'used sparingly: how many play at once is set under This device.' }),
    setting({ label: 'Picture', help: 'How the picture fills its tile.', control: fit }),
  ]));

  const facts = el('dl', 'facts');
  const fact = (term, value) => { if (value) facts.append(el('dt', null, term), el('dd', null, value)); };
  fact('Status', camera.online ? 'Online' : 'Offline');
  fact('Model', camera.model);
  fact('Source', `${camera.source}, channel ${camera.channel + 1}`);
  fact('HD stream', describeStream(camera.main));
  fact('SD stream', describeStream(camera.sub));
  body.append(facts);
  const advice = keyframeAdvice(camera.main);
  if (advice && s.quality !== 'sub') body.append(notice(advice));
  return body;
}

// ---------------------------------------------------------------- views

function renderViews() {
  const box = $('views');
  box.replaceChildren();
  draft.views.forEach((view, index) => box.append(viewCard(view, index)));
}

function layoutLabel(id) {
  return LAYOUT_TEXT[id] || (config.layout_names || {})[id] || id;
}

/** A small drawing of a layout, as the agent has it. */
function layoutThumb(id) {
  const geometry = (config.layout_geometry || {})[id];
  if (!geometry) return null;
  const thumb = el('span', 'layout-thumb');
  thumb.setAttribute('aria-hidden', 'true');
  thumb.style.gridTemplateColumns = `repeat(${geometry.cols}, 1fr)`;
  thumb.style.gridTemplateRows = `repeat(${geometry.rows}, 1fr)`;
  for (const t of geometry.tiles) {
    const tile = el('i');
    tile.style.gridColumn = `${t.x + 1} / span ${t.w}`;
    tile.style.gridRow = `${t.y + 1} / span ${t.h}`;
    thumb.append(tile);
  }
  return thumb;
}

function layoutHelp(id) {
  const box = el('span', 'with-thumb');
  const thumb = layoutThumb(id);
  if (thumb) box.append(thumb);
  const geometry = (config.layout_geometry || {})[id];
  box.append(id === 'auto' ? 'The smallest grid that fits every camera.'
    : id === 'auto-feature' ? 'One big tile and the rest around it, sized to the number of cameras.'
      : geometry ? `${geometry.tiles.length} tiles; the first camera gets tile 1.` : '');
  return box;
}

function viewCard(view, index) {
  const count = draft.views.length;
  const card = el('article', 'card');
  card.dataset.key = String(index);

  const head = el('div', 'card-head');
  const title = el('div', 'card-title');
  const heading = el('h3', null, view.name.trim() || 'Untitled view');
  title.append(heading, el('p', null, count > 1 ? `View ${index + 1} of ${count}` : 'The only view'));
  head.append(title);
  if (count > 1) {
    head.append(moveButtons(index, count, view.name, (delta) => {
      const [moved] = draft.views.splice(index, 1);
      draft.views.splice(index + delta, 0, moved);
      renderViews();
      refocus($('views'), String(index + delta), `[data-move="${delta}"]`, `[data-move="${-delta}"]`);
      changed();
    }));
  }

  const name = textOf(view.name, { placeholder: 'Front of the house', maxLength: 64 });
  name.addEventListener('input', () => {
    view.name = name.value;
    heading.textContent = name.value.trim() || 'Untitled view';
    changed();
  });
  const layout = selectOf(config.layouts.map((id) => [id, layoutLabel(id)]), view.layout);
  const layoutRow = setting({ label: 'Layout', help: layoutHelp(view.layout), control: layout });
  layout.addEventListener('change', () => {
    view.layout = layout.value;
    layoutRow.querySelector('.setting-help').replaceChildren(layoutHelp(view.layout));
    changed();
  });

  const foot = el('div', 'card-foot');
  const remove = el('button', 'danger', 'Remove view');
  remove.type = 'button';
  remove.disabled = count <= 1;           // the wall always needs one view
  remove.title = remove.disabled ? 'A wall needs at least one view' : '';
  remove.addEventListener('click', () => {
    draft.views.splice(index, 1);
    renderViews();
    changed();
  });
  foot.append(remove);

  card.append(head, rowsOf([setting({ label: 'Name', control: name }), layoutRow]),
              rowsOf(cameraChoice(view, 'cameras')), foot);
  return card;
}

/** "Every camera", or a switch per camera. Cameras kept off the wall cannot be chosen. */
function cameraChoice(target, key, { help = 'In the order set under Cameras. New cameras join by themselves.' } = {}) {
  const every = switchOf(target[key] === 'all');
  const rows = [setting({ label: 'Every camera', help, control: every, fit: true })];
  const cameraRows = config.cameras.map((camera) => {
    const hidden = draft.cameras.settings[camera.id] && !draft.cameras.settings[camera.id].show;
    const on = switchOf(!hidden && (target[key] === 'all' || target[key].includes(camera.id)));
    on.disabled = !!hidden;
    on.addEventListener('change', () => {
      const list = target[key] === 'all' ? config.cameras.map((c) => c.id) : target[key];
      target[key] = toggleCamera(list, camera.id, on.checked);
      changed();
    });
    const note = hidden ? 'Kept off the wall under Cameras' : (camera.online ? '' : 'Offline');
    const row = setting({ label: camera.name, help: note, control: on, fit: true });
    row.classList.add('camera-choice');
    return row;
  });
  const sync = () => { for (const row of cameraRows) row.hidden = target[key] === 'all'; };
  every.addEventListener('change', () => {
    target[key] = every.checked ? 'all' : config.cameras.map((c) => c.id);
    for (const row of cameraRows) {
      const input = row.querySelector('input');
      if (!input.disabled) input.checked = true;
    }
    sync();
    changed();
  });
  sync();
  if (!config.cameras.length) rows.push(setting({ label: 'No cameras found yet', control: el('span'), fit: true }));
  return [...rows, ...cameraRows];
}

// ---------------------------------------------------------------- layouts

const selections = {};   // layout id -> the tile being sized

function renderLayouts() {
  const box = $('layouts');
  box.replaceChildren();
  if (!draft.layouts.length) {
    box.append(emptyNote('None yet. The built-in layouts are always there; add one here for an '
      + 'arrangement they do not have.'));
  }
  draft.layouts.forEach((layout, index) => box.append(layoutCard(layout, index)));
}

function layoutCard(layout, index) {
  const selection = selections[layout.id] || (selections[layout.id] = { tile: null });
  const card = el('article', 'card');
  card.dataset.layout = layout.id;

  const head = el('div', 'card-head');
  const title = el('div', 'card-title');
  const heading = el('h3', null, layout.name || 'Untitled layout');
  title.append(heading, el('p', null, `${layout.tiles.length} tiles on a ${layout.cols} × ${layout.rows} grid`));
  head.append(title);

  const name = textOf(layout.name, { placeholder: 'My layout', maxLength: 48 });
  name.addEventListener('input', () => {
    layout.name = name.value;
    heading.textContent = name.value || 'Untitled layout';
    changed();
  });

  const sizes = [1, 2, 3, 4, 5, 6, 7, 8].map((n) => [String(n), String(n)]);
  const cols = selectOf(sizes, String(layout.cols));
  const rows = selectOf(sizes, String(layout.rows));
  cols.setAttribute('aria-label', 'Columns');
  rows.setAttribute('aria-label', 'Rows');
  const resize = () => {
    resizeLayout(layout, Number(cols.value), Number(rows.value));
    selection.tile = null;
    renderLayouts();
    changed();
  };
  cols.addEventListener('change', resize);
  rows.addEventListener('change', resize);
  const gridSize = el('span', 'grid-size');
  gridSize.append(cols, el('span', null, '×'), rows);

  const presets = Object.keys(config.layout_geometry || {}).filter((id) => id !== layout.id);
  const preset = selectOf([['', 'Choose a layout…'], ...presets.map((id) => [id, layoutLabel(id)])], '');
  preset.addEventListener('change', () => {
    if (!preset.value) return;
    copyShape(layout, config.layout_geometry[preset.value]);
    selection.tile = null;
    renderLayouts();
    changed();
  });

  const editor = el('div', 'card-body');
  editor.append(layoutEditor(layout, selection), tilePanel(layout, selection));

  const users = [...draft.views.filter((v) => v.layout === layout.id).map((v) => `“${v.name}”`),
    ...(draft.detection.promote_layout === layout.id ? ['detection'] : [])];
  const foot = el('div', 'card-foot');
  if (users.length) foot.append(el('p', 'hint', `Used by ${users.join(' and ')}.`));
  const remove = el('button', 'danger', 'Remove layout');
  remove.type = 'button';
  remove.disabled = users.length > 0;
  remove.title = users.length ? 'Choose another layout there first' : '';
  remove.addEventListener('click', () => {
    draft.layouts.splice(index, 1);
    delete selections[layout.id];
    renderLayouts();
    changed();
  });
  foot.append(remove);

  card.append(head, rowsOf([
    setting({ label: 'Name', help: 'How it is listed when a view picks a layout.', control: name }),
    setting({ label: 'Grid size', help: 'Columns × rows. Tiles that still fit are kept.', control: gridSize, fit: true }),
    setting({ label: 'Start from', help: "Copy another layout's shape, then adjust it.", control: preset }),
  ]), editor, foot);
  return card;
}

/**
 * The grid itself. Dragging across cells joins them; a tap picks the tile under it.
 * Where the pointer is comes from its coordinates rather than per-cell events: a touch
 * is captured by the element it started on, so the other cells never hear about it.
 */
function layoutEditor(layout, selection) {
  const columns = `repeat(${layout.cols}, 1fr)`;
  const rowsTemplate = `repeat(${layout.rows}, 1fr)`;
  const grid = el('div', 'layout-grid');
  grid.style.gridTemplateColumns = columns;
  grid.style.gridTemplateRows = rowsTemplate;
  layout.tiles.forEach((tile, index) => {
    // The number is the order cameras fill the tiles, so the big one is usually first.
    const box = el('div', 'layout-tile');
    box.append(el('span', 'n', String(index + 1)));
    if (tile.w > 1 || tile.h > 1) box.append(el('span', 'size', `${tile.w}×${tile.h}`));
    box.classList.toggle('picked', selection.tile === tile);
    box.style.gridColumn = `${tile.x + 1} / span ${tile.w}`;
    box.style.gridRow = `${tile.y + 1} / span ${tile.h}`;
    grid.append(box);
  });

  const cells = el('div', 'layout-cells');
  cells.style.gridTemplateColumns = columns;
  cells.style.gridTemplateRows = rowsTemplate;
  for (let y = 0; y < layout.rows; y += 1) {
    for (let x = 0; x < layout.cols; x += 1) {
      const cell = el('div', 'layout-cell');
      cell.dataset.x = x;
      cell.dataset.y = y;
      cells.append(cell);
    }
  }

  let from = null;
  let to = null;
  const cellAt = (ev) => {
    const hit = document.elementFromPoint(ev.clientX, ev.clientY);
    const cell = hit && hit.closest ? hit.closest('.layout-cell') : null;
    return cell && cells.contains(cell) ? { x: Number(cell.dataset.x), y: Number(cell.dataset.y) } : null;
  };
  const paint = () => {
    for (const cell of cells.children) {
      const cx = Number(cell.dataset.x);
      const cy = Number(cell.dataset.y);
      cell.classList.toggle('picking', !!from
        && cx >= Math.min(from.x, to.x) && cx <= Math.max(from.x, to.x)
        && cy >= Math.min(from.y, to.y) && cy <= Math.max(from.y, to.y));
    }
  };
  cells.addEventListener('pointerdown', (ev) => {
    // Not a form control, so the locked fieldset does not stop it: check by hand.
    if (ev.button !== 0 || isLocked()) return;
    const at = cellAt(ev);
    if (!at) return;
    ev.preventDefault();
    try {
      cells.setPointerCapture(ev.pointerId);   // keep hearing a drag that strays off the grid
    } catch (_) { /* the pointer already lifted; pointerup is still on its way */ }
    from = at;
    to = at;
    paint();
  });
  cells.addEventListener('pointermove', (ev) => {
    if (!from) return;
    const at = cellAt(ev);
    if (at && (at.x !== to.x || at.y !== to.y)) {
      to = at;
      paint();
    }
  });
  cells.addEventListener('pointerup', () => {
    if (!from) return;
    const [start, end] = [from, to];
    from = null;
    if (start.x === end.x && start.y === end.y) {
      selection.tile = layout.tiles[cellOwners(layout).get(`${start.x},${start.y}`)] || null;
      renderLayouts();
      return;
    }
    mergeRect(layout, start.x, start.y, end.x, end.y);
    selection.tile = tileAt(layout, Math.min(start.x, end.x), Math.min(start.y, end.y));
    renderLayouts();
    changed();
  });
  cells.addEventListener('pointercancel', () => {
    from = null;
    paint();
  });

  const stack = el('div', 'layout-stack');
  stack.append(grid, cells);
  return stack;
}

/** Pick a tile (without a pointer too), then size or split it. */
function tilePanel(layout, selection) {
  const panel = el('div', 'tile-panel');
  const picked = selection.tile ? layout.tiles.indexOf(selection.tile) : -1;
  const picker = selectOf([['', 'Choose a tile'], ...layout.tiles.map((t, i) => [String(i), `Tile ${i + 1}`])],
                          picked >= 0 ? String(picked) : '');
  picker.setAttribute('aria-label', 'Tile to size');
  picker.style.width = 'auto';
  picker.addEventListener('change', () => {
    selection.tile = picker.value === '' ? null : layout.tiles[Number(picker.value)];
    renderLayouts();
  });
  panel.append(picker);
  const tile = selection.tile;
  if (!tile || picked < 0) {
    panel.append(el('span', 'hint', 'or tap one. Drag across cells to join them.'));
    return panel;
  }

  const maxW = layout.cols - tile.x;
  const maxH = layout.rows - tile.y;
  const width = numberOf(tile.w, { min: 1, max: maxW, integer: true });
  const height = numberOf(tile.h, { min: 1, max: maxH, integer: true });
  const apply = () => {
    const w = Math.min(Math.max(1, Number(width.value) || 1), maxW);
    const h = Math.min(Math.max(1, Number(height.value) || 1), maxH);
    mergeRect(layout, tile.x, tile.y, tile.x + w - 1, tile.y + h - 1);
    selection.tile = tileAt(layout, tile.x, tile.y);
    renderLayouts();
    changed();
  };
  width.addEventListener('change', apply);
  height.addEventListener('change', apply);
  const widthLabel = el('label', null, 'Wide');
  widthLabel.append(width);
  const heightLabel = el('label', null, 'High');
  heightLabel.append(height);

  const split = el('button', null, 'Split');
  split.type = 'button';
  split.disabled = tile.w === 1 && tile.h === 1;
  split.addEventListener('click', () => {
    splitTile(layout, tile);
    selection.tile = null;
    renderLayouts();
    changed();
  });
  panel.append(widthLabel, heightLabel, split);
  return panel;
}

// ---------------------------------------------------------------- display

function renderDisplay() {
  const box = $('display');
  box.replaceChildren();
  const f = binder(draft.display, changed);
  box.append(
    group('On each tile', [
      setting({ label: 'Camera names', help: 'Show each camera\'s name in its tile.',
                info: 'A camera that detects something is always named, whatever this is set to.',
                control: f.switch('show_labels'), fit: true }),
      setting({ label: 'HD / SD badge', help: 'Show which quality each tile is playing.',
                control: f.switch('show_quality_badges'), fit: true }),
      setting({ label: 'Detection highlight', help: 'Outline a tile and say what it saw.',
                control: f.switch('highlight_detections'), fit: true }),
      setting({ label: 'Picture', help: 'The whole picture may leave black bars; filling the tile crops the edges.',
                control: f.select('fit', [['contain', 'Show the whole picture'], ['cover', 'Fill the tile']]) }),
      setting({ label: 'Offline cameras', help: 'What a tile shows when its camera is unreachable.',
                control: f.select('offline_style', [['message', 'Say it is offline'], ['blank', 'Leave the tile dark']]) }),
    ]),
    group('On the screen', [
      setting({ label: 'Clock', help: 'Show the time in a corner of the wall.',
                control: f.switch('show_clock'), fit: true }),
      setting({ label: 'Hide the mouse pointer after', help: 'Once the mouse stops moving. 0 keeps it visible.',
                control: f.number('hide_cursor_seconds', { min: 0, max: 120, integer: true, unit: 's' }), fit: true }),
    ]),
    screenSchedule(),
  );
}

/** Turning the television off overnight, where the device can do it at all. */
function screenSchedule() {
  const screen = draft.display.screen
    || (draft.display.screen = { enabled: false, off_at: '23:00', on_at: '06:30' });
  const s = binder(screen, changed);

  const timeOf = (key) => {
    const input = textOf(screen[key], { type: 'time' });
    input.addEventListener('change', () => {
      if (input.value) { screen[key] = input.value; changed(); }
    });
    return input;
  };
  const offRow = setting({ label: 'Off at', control: timeOf('off_at'), fit: true });
  const onRow = setting({ label: 'On again at', control: timeOf('on_at'), fit: true });
  const showRows = () => { offRow.hidden = onRow.hidden = !screen.enabled; };
  showRows();

  const rows = [
    setting({ label: 'Turn the screen off overnight',
              help: 'The television is asked to switch off, not just go dark, so it uses almost nothing.',
              info: 'Over HDMI-CEC where the television supports it; otherwise the device stops '
                  + 'driving its output, which blanks the picture but leaves the set awake. Times '
                  + 'are this device\'s own, so its time zone has to be right.',
              control: s.switch('enabled', { after: showRows }), fit: true }),
    offRow,
    onRow,
  ];
  const note = device && device.host && device.host.kind !== 'none'
    && device.host.can_control_display === false
    ? 'This device has no way to turn a screen off: install cec-utils for HDMI-CEC.'
    : undefined;
  return group('Screen schedule', rows, note ? { note } : undefined);
}

// ---------------------------------------------------------------- detection

function renderDetection() {
  const d = draft.detection;
  const box = $('detection');
  box.replaceChildren();
  const f = binder(d, changed);

  const rest = el('div', 'stack');
  const master = rowsOf([setting({
    label: 'Detection', help: 'Bring a camera forward when it sees something.',
    info: 'Every few seconds the NVR or camera is asked what it has detected, in one request for '
      + 'all cameras. Nothing is analysed on this device, and nothing is asked while this is off.',
    control: f.switch('enabled', { after: () => rest.classList.toggle('inactive', !d.enabled) }),
    fit: true,
  })]);
  rest.classList.toggle('inactive', !d.enabled);

  const promoteRow = setting({
    label: 'Layout meanwhile', help: "Keeping the view's own layout stops the wall changing shape.",
    control: f.select('promote_layout', [['same', "Keep the view's layout"],
                                          ...config.layouts.map((id) => [id, layoutLabel(id)])]),
  });
  const action = f.select('action', [
    ['fullscreen', 'Show it full screen'],
    ['promote', 'Move it to the big tile'],
    ['highlight', 'Only highlight its tile'],
  ], { after: () => { promoteRow.hidden = d.action !== 'promote'; } });
  promoteRow.hidden = d.action !== 'promote';

  rest.append(
    group('When a camera sees something', [
      setting({ label: 'What the wall does', control: action }),
      promoteRow,
      setting({ label: 'Switch that camera to HD', help: 'Sharper, but opens an HD stream on the NVR while it lasts.',
                control: f.switch('use_main_stream'), fit: true }),
    ]),
    group('What counts', triggerList(d),
          { note: 'Most important first: when two cameras see something at once, the higher one wins.' }),
    group('Cameras to watch', cameraChoice(d, 'cameras', { help: 'New cameras are watched by themselves.' })),
    group('Timing', [
      setting({ label: 'React to one thing for at most',
                help: 'Then it counts as part of the scene until it stops and happens again.',
                info: 'Reolink reports what a camera can see, not what just changed: a car parked on the '
                    + 'drive is reported for as long as it sits there. Without a limit its tile would stay '
                    + 'marked all day. 0 reacts for as long as the NVR keeps reporting it.',
                control: f.number('max_event_seconds', { min: 0, max: 3600, unit: 's' }), fit: true }),
      setting({ label: 'Stay on the camera after it stops', help: 'Once nothing more is detected.',
                control: f.number('hold_seconds', { min: 0, max: 600, unit: 's' }), fit: true }),
      setting({ label: 'Keep the badge for', help: 'How long a tile keeps saying what it saw.',
                control: f.number('highlight_seconds', { min: 0, max: 600, unit: 's' }), fit: true }),
      setting({ label: 'Pause after someone uses the wall', help: 'A detection never takes the screen from a person.',
                control: f.number('manual_override_seconds', { min: 0, max: 3600, unit: 's' }), fit: true }),
    ]),
    group('Advanced', [
      setting({ label: 'Minimum time on a camera',
                info: 'Before another camera can take over. Stops two busy cameras swapping back and forth.',
                control: f.number('min_focus_seconds', { min: 0, max: 300, unit: 's' }), fit: true }),
      setting({ label: 'Check for detections every',
                info: 'All cameras are asked in one request. Shorter reacts sooner but asks more of the NVR.',
                control: f.number('poll_seconds', { min: 1, max: 60, unit: 's' }), fit: true }),
    ], { advanced: true }),
  );
  box.append(master, rest);
}

/** Which detections count, in priority order. Switched-off ones keep their place too. */
function triggerList(d) {
  const order = [...d.triggers, ...config.detection_types.filter((t) => !d.triggers.includes(t))];
  const list = el('ol', 'order-list');
  const draw = () => {
    list.replaceChildren();
    order.forEach((type, i) => {
      const on = d.triggers.includes(type);
      const label = DETECTION_TEXT[type] || type;
      const item = el('li', on ? 'order-item' : 'order-item off');
      item.dataset.key = type;
      const tick = switchOf(on);
      tick.id = `trigger-${type}`;
      tick.addEventListener('change', () => {
        const enabled = new Set(d.triggers);
        if (tick.checked) enabled.add(type); else enabled.delete(type);
        d.triggers = order.filter((t) => enabled.has(t));
        draw();
        refocus(list, type, 'input.switch');
        changed();
      });
      const main = el('div', 'order-main');
      const name = el('label', 'order-name', label);
      name.htmlFor = tick.id;
      main.append(name);
      const rank = d.triggers.indexOf(type);
      const moves = moveButtons(i, order.length, label, (delta) => {
        [order[i], order[i + delta]] = [order[i + delta], order[i]];
        d.triggers = order.filter((t) => d.triggers.includes(t));
        draw();
        refocus(list, type, `[data-move="${delta}"]`, `[data-move="${-delta}"]`);
        changed();
      });
      item.append(el('span', 'rank', rank >= 0 ? String(rank + 1) : '–'), main, tick, moves);
      list.append(item);
    });
  };
  draw();
  return list;
}

// ---------------------------------------------------------------- this device

function renderDevice() {
  const box = $('device');
  box.replaceChildren();
  const f = binder(draft.device, changed);
  const guide = el('button', null, 'Start');
  guide.type = 'button';
  guide.addEventListener('click', () => goGuide(0));
  box.append(
    group('HD (full resolution)', [
      setting({ label: 'When to use HD', control: f.select('main_stream_only_on_detection',
                                                          [['false', 'On the biggest tile'], ['true', 'Only during a detection']]),
                help: 'A camera made full screen, or set to Always HD under Cameras, gets HD either way.' }),
      setting({ label: 'HD streams at once', help: 'Keep 1 on a Raspberry Pi.',
                info: 'Each HD stream uses one of the NVR\'s full-resolution viewer slots. A Reolink allows '
                  + 'two per camera, and its phone app needs one.',
                control: f.number('max_main_streams', { min: 0, max: 16, integer: true }), fit: true }),
    ]),
    group('Views', [
      setting({ label: 'Rotate views every', help: '0 stays on the same view. Paused while a camera is full screen.',
                control: f.number('cycle_views_seconds', { min: 0, max: 86400, integer: true, unit: 's' }), fit: true }),
    ]),
    group('Setup guide', [
      setting({ label: 'Setup guide', help: 'Walk through the essentials again.', control: guide, fit: true }),
    ]),
    group('Advanced', [
      setting({ label: 'Streams at once', help: 'HD and SD together. A camera shown twice counts once.',
                control: f.number('max_total_streams', { min: 1, max: 64, integer: true }), fit: true }),
      setting({ label: 'Tile size needed for HD',
                help: 'Share of the screen: full screen is 100 %, the big tile in 1+5 is 44 %.',
                control: f.number('main_stream_min_fraction', { min: 1, max: 100, scale: 100, unit: '%' }), fit: true }),
      setting({ label: 'Keep HD after leaving full screen', help: 'Going back within this time is instant.',
                control: f.number('main_stream_hold_seconds', { min: 0, max: 600, unit: 's' }), fit: true }),
      setting({ label: 'Keep the grid playing in full screen',
                help: 'Returning to the grid is instant, but streams nobody sees are still decoded.',
                control: f.switch('keep_grid_warm'), fit: true }),
    ], { advanced: true }),
  );
}

// ---------------------------------------------------------------- general

let device = null;              // what the device says about itself
let networks = null;            // the last scan, while the join dialog is open

async function refreshDevice() {
  try {
    device = await api('/api/device');
  } catch (err) {
    device = null;              // locked out or offline: the panel says so rather than lying
  }
}

function duration(seconds) {
  if (!Number.isFinite(seconds)) return '—';
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return `${days} d ${hours} h`;
  if (hours) return `${hours} h ${minutes} min`;
  return `${minutes} min`;
}

function gigabytes(bytes) {
  return Number.isFinite(bytes) ? `${(bytes / 1024 ** 3).toFixed(1)} GB` : '—';
}

/** How the device is on the network, in the words someone would use for it. */
function linkLabel(host) {
  if (host.access_point) return 'Its own setup network';
  if (host.link === 'wifi') return host.ssid ? `Wi-Fi · ${host.ssid}` : 'Wi-Fi';
  if (host.link === 'ethernet') return 'Ethernet';
  if (host.link === 'none' || !host.link) return 'Not connected';
  return host.link;
}

function fact(label, value, help) {
  return setting({ label, help, control: el('span', 'fact', value ?? '—'), fit: true });
}

function renderGeneral() {
  const box = $('general');
  box.replaceChildren();
  if (!device) {
    box.append(notice('This device cannot be read from here.', 'warn'));
    return;
  }
  const host = device.host || { kind: 'none' };
  const rows = [
    fact('Name', device.name, 'Change it under This device.'),
    fact('Version', device.version),
    fact('Cameras', `${device.cameras} from ${device.sources.length} source${device.sources.length === 1 ? '' : 's'}`),
  ];
  if (Number.isFinite(host.uptime_seconds)) rows.push(fact('Running for', duration(host.uptime_seconds)));
  if (Number.isFinite(host.temperature_c)) {
    rows.push(fact('Temperature', `${host.temperature_c.toFixed(1)} °C`,
                   host.temperature_c >= 80 ? 'Hot: check that nothing is covering it.' : undefined));
  }
  if (Number.isFinite(host.disk_free_bytes)) {
    rows.push(fact('Disk free', `${gigabytes(host.disk_free_bytes)} of ${gigabytes(host.disk_total_bytes)}`));
  }

  const net = [fact('Connection', linkLabel(host))];
  if (host.addresses && host.addresses.length) net.push(fact('Address', host.addresses.join(', ')));
  if (host.hostname) net.push(fact('Name on the network', `${host.hostname}.local`));

  box.append(group('This device', rows));

  if (host.kind === 'none') {
    box.append(group('Network', net, { note: 'Connecting this device to a different network, '
      + 'restarting it and resetting it are done by a helper that runs on the device itself. '
      + 'This installation has none, so only the settings above can be changed here.' }));
    box.append(resetGroup());
    return;
  }
  if (host.kind === 'fake' || host.simulated) {
    box.append(notice('These controls are simulated on this machine: nothing here reaches real '
                      + 'hardware. On the device itself they do.', 'warn'));
  }
  if (host.error) box.append(notice(`The device helper answered: ${host.error}`, 'warn'));

  net.push(setting({
    label: 'Wi-Fi', help: host.access_point
      ? 'This device is showing its own setup network. Joining yours turns that off.'
      : 'Join a different network, or forget the one it is on.',
    control: buttons([
      ['Choose a network', openJoin],
      ...(host.ssid ? [['Forget ' + host.ssid, () => forgetNetwork(host.ssid), 'danger']] : []),
    ]),
  }));
  net.push(setting({
    label: 'Prefer', help: 'Which connection to use when both are available.',
    control: buttons([['Ethernet', () => prefer('ethernet')], ['Wi-Fi', () => prefer('wifi')]]),
  }));
  net.push(setting({
    label: 'Setup network', help: 'Show this device\'s own network, to set it up from a phone.',
    control: buttons([[host.access_point ? 'Turn it off' : 'Turn it on',
                       () => accessPoint(!host.access_point)]]), fit: true,
  }));
  box.append(group('Network', net));

  box.append(group('Power', [
    setting({ label: 'Restart', help: 'The wall is back in a minute or so.',
              control: buttons([['Restart', () => confirmAction({
                title: 'Restart this device?',
                body: 'The wall goes dark for about a minute.',
                confirm: 'Restart', path: '/api/device/reboot',
              })]]), fit: true }),
    setting({ label: 'Shut down',
              help: 'It stays off until someone switches the power off and on again.',
              control: buttons([['Shut down', () => confirmAction({
                title: 'Shut this device down?',
                body: 'Nothing here can switch it back on: somebody has to unplug it and plug it in again.',
                confirm: 'Shut down', path: '/api/device/shutdown', danger: true,
              })]]), fit: true }),
  ]));
  box.append(resetGroup());
}

function resetGroup() {
  return group('Start again', [
    setting({ label: 'Reset the settings',
              help: 'Cameras, views and layouts go back to how they were installed. The admin '
                + 'password and the NVR\'s login stay.',
              control: buttons([['Reset the settings', () => confirmAction({
                title: 'Reset the settings?',
                body: 'Views, layouts, camera names and detection go back to the beginning, and the '
                  + 'setup guide starts again. Your password and your NVR login are kept.',
                confirm: 'Reset the settings', path: '/api/device/reset',
                body_json: { scope: 'configuration' }, restarts: true,
              })]]), fit: true }),
    setting({ label: 'Reset the device',
              help: 'Everything, including the admin password and every saved credential. For '
                + 'handing this device to someone else.',
              control: buttons([['Reset the device', () => confirmAction({
                title: 'Reset the whole device?',
                body: 'Everything this device knows is forgotten: the settings, the admin password, '
                  + 'the NVR\'s username and password, and the wi-fi network. It comes back as if '
                  + 'newly installed, and whoever opens it next chooses the password.',
                confirm: 'Reset everything', path: '/api/device/reset', danger: true,
                body_json: { scope: 'device', forget_network: true }, restarts: true,
                typeToConfirm: 'reset',
              })]]), fit: true }),
  ]);
}

/** A row of buttons, which the locked fieldset disables along with everything else. */
function buttons(items) {
  const row = el('div', 'button-row');
  for (const [label, onClick, kind] of items) {
    const button = el('button', kind === 'danger' ? 'danger' : null, label);
    button.type = 'button';
    button.addEventListener('click', onClick);
    row.append(button);
  }
  return row;
}

async function deviceAction(path, body, { restarts = false } = {}) {
  try {
    await api(path, { method: 'POST', body: body || {} });
  } catch (err) {
    toast(err.message, 'warn');
    return false;
  }
  if (restarts) {
    toast('Done. This device is restarting: the page will come back by itself.', 'ok');
  } else {
    toast('Done.', 'ok');
  }
  await refreshDevice();
  renderGeneral();
  return true;
}

const prefer = (link) => deviceAction('/api/device/network/prefer', { link });
const accessPoint = (on) => deviceAction('/api/device/access-point', { on });

function forgetNetwork(ssid) {
  confirmAction({
    title: `Forget ${ssid}?`,
    body: 'This device stops using that network. If it has no cable, it will show its own setup '
      + 'network instead, and you will have to join that to set it up again.',
    confirm: 'Forget it', danger: true, path: '/api/device/network/forget', body_json: { ssid },
  });
}

/** Ask first, and for the worst of them ask for a word to be typed. */
function confirmAction({ title, body, confirm, path, body_json, danger = false, restarts = false,
                         typeToConfirm = null }) {
  const dialog = el('dialog', 'confirm');
  const form = el('form');
  form.method = 'dialog';
  const typed = typeToConfirm ? textOf('', { placeholder: typeToConfirm }) : null;
  const go = el('button', danger ? 'danger' : 'primary', confirm);
  go.type = 'submit';
  const cancel = el('button', null, 'Cancel');
  cancel.type = 'button';
  cancel.addEventListener('click', () => dialog.close());
  const foot = el('div', 'card-foot');
  foot.append(cancel, go);
  form.append(el('h3', null, title), el('p', null, body));
  if (typed) {
    form.append(setting({ label: `Type “${typeToConfirm}” to confirm`, control: typed }));
    go.disabled = true;
    typed.addEventListener('input', () => { go.disabled = typed.value.trim() !== typeToConfirm; });
  }
  form.append(foot);
  dialog.append(form);
  document.body.append(dialog);
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    dialog.close();
    await deviceAction(path, body_json, { restarts });
  });
  dialog.addEventListener('close', () => dialog.remove());
  dialog.showModal();
}

/** Pick a network and join it. */
async function openJoin() {
  const dialog = el('dialog', 'join');
  const list = el('div', 'network-list', 'Looking for networks…');
  const form = el('form');
  form.method = 'dialog';
  const close = el('button', null, 'Cancel');
  close.type = 'button';
  close.addEventListener('click', () => dialog.close());
  const foot = el('div', 'card-foot');
  foot.append(close);
  form.append(el('h3', null, 'Choose a network'), list, foot);
  dialog.append(form);
  document.body.append(dialog);
  dialog.addEventListener('close', () => dialog.remove());
  dialog.showModal();

  try {
    networks = (await api('/api/device/networks')).networks || [];
  } catch (err) {
    list.replaceChildren(notice(err.message, 'warn'));
    return;
  }
  if (!networks.length) {
    list.replaceChildren(notice('No networks in range.', 'warn'));
    return;
  }
  list.replaceChildren(...networks.map((n) => {
    const row = el('button', 'network', '');
    row.type = 'button';
    row.append(el('span', 'network-name', n.ssid),
               el('span', 'network-detail', `${n.security || 'open'} · ${n.signal}%${n.active ? ' · in use' : ''}`));
    row.addEventListener('click', () => { dialog.close(); askPassword(n); });
    return row;
  }));
}

function askPassword(network) {
  if (!network.security) {
    joinNetwork(network.ssid, '');
    return;
  }
  const dialog = el('dialog', 'join');
  const form = el('form');
  form.method = 'dialog';
  form.noValidate = true;
  const password = textOf('', { type: 'password', autocomplete: 'off' });
  const error = el('p', 'form-error');
  error.hidden = true;
  const go = el('button', 'primary', 'Join');
  go.type = 'submit';
  const cancel = el('button', null, 'Cancel');
  cancel.type = 'button';
  cancel.addEventListener('click', () => dialog.close());
  const foot = el('div', 'card-foot');
  foot.append(error, cancel, go);
  form.append(el('h3', null, `Join ${network.ssid}`),
              el('p', null, 'This device will use this network from now on. If you are connected '
                          + 'to its setup network, rejoin your own network afterwards.'),
              setting({ label: 'Password', control: password }), foot);
  dialog.append(form);
  document.body.append(dialog);
  dialog.addEventListener('close', () => dialog.remove());
  dialog.showModal();
  password.focus();

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    go.disabled = true;
    try {
      await api('/api/device/network/join', { method: 'POST', body: { ssid: network.ssid, password: password.value } });
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
      go.disabled = false;
      return;
    }
    dialog.close();
    toast(`Joined ${network.ssid}.`, 'ok');
    await refreshDevice();
    renderGeneral();
  });
}

async function joinNetwork(ssid, password) {
  if (await deviceAction('/api/device/network/join', { ssid, password })) {
    toast(`Joined ${ssid}.`, 'ok');
  }
}

// ---------------------------------------------------------------- security

function renderSecurity() {
  const box = $('security');
  box.replaceChildren();
  if (!session) return;
  const s = session;

  const status = el('div', `status ${s.required ? 'good' : 'bad'}`);
  const words = el('div');
  if (s.required) {
    words.append(el('strong', null, 'Settings are protected'),
                 el('span', 'hint', 'Changing anything needs the admin password.'));
  } else {
    words.append(el('strong', null, 'Anyone on your network can change these settings'),
                 el('span', 'hint', 'Choose a password so that only you can.'));
  }
  status.append(icon(s.required ? 'lock' : 'warning'), words);
  box.append(rowsOf([status]));

  if (s.password_source === 'config') {
    box.append(notice('The password is set in the configuration (VIEWPORT_ADMIN_PASSWORD), so it is changed '
      + 'there rather than here.'));
  } else if (!s.can_set_password) {
    box.append(notice('There is nowhere to keep a password: set VIEWPORT_SECRETS_FILE, then restart.', 'warn'));
  } else {
    box.append(passwordForm(!s.required));
  }

  box.append(group('Watching the wall', [
    setting({ label: 'Screens need a token', control: el('span', s.token_required ? 'badge' : 'badge warn',
                                                         s.token_required ? 'Yes' : 'No'), fit: true,
              help: s.token_required ? 'A screen opens the wall with ?token=… in its address.'
                : 'Anyone on your network can watch the cameras. Set VIEWPORT_API_TOKEN to require a token.' }),
  ]), group('Forgotten the password?', [
    setting({ label: 'Reset it from the configuration', control: el('span'), fit: true,
              help: 'Set VIEWPORT_ADMIN_PASSWORD in .env and restart. It takes the place of a password chosen here.' }),
  ]));
}

function passwordForm(first) {
  const form = el('form', 'card');
  // Checked below, with messages beside the form rather than the browser's own bubbles: one
  // of the checks (the two passwords match) is not something the browser can do anyway.
  form.noValidate = true;
  const heading = el('div', 'card-head');
  heading.append(el('div', 'card-title'));
  heading.firstChild.append(el('h3', null, first ? 'Choose a password' : 'Change the password'));

  const username = textOf(first ? 'admin' : session.username, { autocomplete: 'username', maxLength: 64 });
  username.required = true;
  const current = textOf('', { type: 'password', autocomplete: 'current-password' });
  current.required = true;
  const next = textOf('', { type: 'password', autocomplete: 'new-password' });
  next.required = true;
  next.minLength = 8;
  const confirm = textOf('', { type: 'password', autocomplete: 'new-password' });
  confirm.required = true;

  const rows = [setting({ label: 'Username', control: username })];
  if (!first) rows.push(setting({ label: 'Current password', control: current }));
  rows.push(
    setting({ label: first ? 'Password' : 'New password', help: 'At least 8 characters. A few words together work well.',
              control: next }),
    setting({ label: 'Type it again', control: confirm }),
  );

  const error = el('p', 'form-error');
  error.setAttribute('role', 'alert');
  error.hidden = true;
  const submit = el('button', 'primary', first ? 'Set password' : 'Change password');
  submit.type = 'submit';
  const foot = el('div', 'card-foot');
  foot.append(error, submit);

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const [problem, field] = !username.value.trim() ? ['Enter a username.', username]
      : !first && !current.value ? ['Enter the current password.', current]
        : [passwordProblem(next.value, confirm.value), next.value.length < 8 ? next : confirm];
    if (problem) {
      error.textContent = problem;
      error.hidden = false;
      field.focus();
      return;
    }
    error.hidden = true;
    submit.disabled = true;
    try {
      session = await api('/api/admin/password', {
        method: 'POST',
        body: { username: username.value, current_password: first ? '' : current.value, new_password: next.value },
      });
      await refresh();
      toast(first ? 'Password set. Settings are now protected.'
        : 'Password changed. Other devices need to unlock again.');
    } catch (err) {
      error.textContent = err.status === 401 && !first ? 'The current password is wrong.' : err.message;
      error.hidden = false;
      submit.disabled = false;
    }
  });

  form.append(heading, rowsOf(rows), foot);
  return form;
}

const RENDER = {
  general: renderGeneral,
  sources: renderSources,
  cameras: renderCameras,
  views: renderViews,
  layouts: renderLayouts,
  display: renderDisplay,
  detection: renderDetection,
  device: renderDevice,
  security: renderSecurity,
};

// ---------------------------------------------------------------- sections

function renderTabs() {
  const tabs = $('tabs');
  tabs.replaceChildren();
  for (const section of SECTIONS) {
    const tab = el('button', 'tab');
    tab.type = 'button';
    tab.dataset.section = section.id;
    tab.append(el('span', 'tab-title', section.title), el('span', 'tab-hint', section.hint));
    tab.addEventListener('click', () => showSection(section.id));
    tabs.append(tab);
  }
  markTabs();
}

function markTabs() {
  for (const tab of $('tabs').children) {
    const on = tab.dataset.section === current;
    tab.classList.toggle('on', on);
    tab.classList.toggle('dirty', isDirty(tab.dataset.section));
    if (on) tab.setAttribute('aria-current', 'page');
    else tab.removeAttribute('aria-current');
  }
}

/** Show whichever panel should be on screen, and the bars and guide that go with it. */
function applyPanels() {
  const id = visiblePanel();
  for (const panel of PANELS) $(`panel-${panel}`).hidden = panel !== id;
  document.body.classList.toggle('guide', guideStep >= 0);
  markTabs();
  renderGuide();
  renderBars();
}

function showSection(id, { smooth = true } = {}) {
  const next = SECTIONS.some((s) => s.id === id) ? id : SECTIONS[0].id;
  if (next !== current) saveError = '';
  current = next;
  if (location.hash.slice(1) !== current) history.replaceState(null, '', `#${current}`);
  applyPanels();
  // Temperature, addresses and which network it is on all move: read them again on arrival
  // rather than showing whatever was true when the page was opened.
  if (current === 'general') refreshDevice().then(renderGeneral);
  window.scrollTo(0, 0);
  // On a phone the tabs are one row to swipe along: bring the chosen one into view.
  const tabs = $('tabs');
  const tab = [...tabs.children].find((t) => t.dataset.section === current);
  if (tab && tabs.scrollWidth > tabs.clientWidth) {
    tabs.scrollTo({ left: Math.max(0, tab.offsetLeft - (tabs.clientWidth - tab.offsetWidth) / 2),
                    behavior: smooth ? 'smooth' : 'auto' });
  }
}

// ---------------------------------------------------------------- the setup guide

function rememberGuide() {
  try {
    sessionStorage.setItem(GUIDE_KEY, String(guideStep));
  } catch (_) { /* private mode: the guide just starts over on a reload */ }
}

function recalledGuide() {
  try {
    const value = sessionStorage.getItem(GUIDE_KEY);
    return value === null ? null : Number(value);
  } catch (_) {
    return null;
  }
}

function renderGuide() {
  const active = guideStep >= 0 && shown;
  $('guide-head').hidden = !active;
  $('guide-foot').hidden = !active;
  if (!active) return;
  const step = GUIDE[guideStep];
  const steps = GUIDE.length - 2;         // the welcome and the finish are not steps

  const head = $('guide-head');
  const meta = el('div', 'guide-meta');
  meta.append(el('span', 'guide-step', step === 'welcome' ? 'Setup guide'
    : step === 'done' ? 'Setup guide: finished' : `Setup guide: step ${guideStep} of ${steps}`));
  if (step !== 'done') {
    const exit = el('button', 'quiet', 'Exit the guide');
    exit.type = 'button';
    exit.addEventListener('click', exitGuide);
    meta.append(exit);
  }
  const progress = el('div', 'guide-progress');
  for (let i = 1; i <= steps; i += 1) progress.append(el('i', i <= guideStep ? 'done' : ''));
  head.replaceChildren(meta, progress);

  const buttons = el('div', 'guide-buttons');
  const button = (text, cls, onClick) => {
    const b = el('button', cls, text);
    b.type = 'button';
    b.addEventListener('click', onClick);
    buttons.append(b);
    return b;
  };
  if (step !== 'welcome' && step !== 'done') button('Back', 'quiet', () => goGuide(guideStep - 1));
  buttons.append(el('span', 'spacer'));
  if (step === 'welcome') {
    button('Start', 'primary', () => goGuide(1));
  } else if (step === 'done') {
    button('Go to settings', 'primary', leaveGuide);
  } else if (isLocked()) {
    button('Unlock to continue', 'primary', () => openUnlock());
  } else if (step === 'security' && session && !session.required && session.can_set_password) {
    button('Skip for now', 'quiet', () => goGuide(guideStep + 1));
  } else {
    const dirty = isDirty(step);
    const next = button(saving ? 'Saving…' : dirty ? 'Save and continue' : 'Next', 'primary', guideNext);
    next.disabled = saving;
  }
  $('guide-foot').replaceChildren(buttons);
}

async function guideNext() {
  const step = GUIDE[guideStep];
  if (DRAFTED.includes(step) && isDirty(step) && !(await saveSection(step))) return;
  goGuide(guideStep + 1);
}

function goGuide(index) {
  guideStep = Math.max(0, Math.min(GUIDE.length - 1, index));
  const finished = GUIDE[guideStep] === 'done';
  // A reload mid-way picks the guide up again; once it is finished, a reload is the settings.
  if (finished) {
    try {
      sessionStorage.setItem(GUIDE_KEY, '-1');
    } catch (_) { /* private mode */ }
  } else {
    rememberGuide();
  }
  if (finished && config && config.configured === false) finishSetup();
  applyPanels();
  window.scrollTo(0, 0);
}

async function finishSetup() {
  try {
    load(await api('/api/config/setup-done', { method: 'POST' }));
  } catch (_) { /* locked, or read-only: the guide is still over for this visit */ }
}

async function exitGuide() {
  if (config && config.configured === false) await finishSetup();
  leaveGuide();
}

function leaveGuide() {
  guideStep = -1;
  rememberGuide();
  showSection(current, { smooth: false });
}

$('done-settings').addEventListener('click', leaveGuide);

// ---------------------------------------------------------------- page actions

$('add-source').addEventListener('click', () => {
  addingSource = true;
  renderSources();
  const card = $('new-source');
  card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  card.querySelector('input').focus({ preventScroll: true });
});

$('add-layout').addEventListener('click', () => {
  const { id, n } = freeLayoutId([...draft.layouts, ...saved.layouts]);
  const layout = { id, name: `My layout ${n}`, cols: 3, rows: 2, tiles: [] };
  resizeLayout(layout, 3, 2);
  draft.layouts.push(layout);
  renderLayouts();
  changed();
  $('layouts').lastElementChild.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
});

$('add-view').addEventListener('click', () => {
  draft.views.push({ name: `View ${draft.views.length + 1}`, layout: 'auto', cameras: 'all' });
  renderViews();
  changed();
  const card = $('views').lastElementChild;
  card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  card.querySelector('input').focus({ preventScroll: true });
});

$('save').addEventListener('click', () => saveSection(visiblePanel()));
$('discard').addEventListener('click', () => discard(visiblePanel()));
$('lockbar-unlock').addEventListener('click', () => openUnlock());
$('lockbar-icon').append(icon('lock'));

document.addEventListener('keydown', (ev) => {
  if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === 's' && shown) {
    ev.preventDefault();
    if (!isLocked()) saveSection(visiblePanel());
  }
});

window.addEventListener('beforeunload', (ev) => {
  if (config && DRAFTED.some(isDirty)) {
    ev.preventDefault();
    ev.returnValue = '';
  }
});

window.addEventListener('hashchange', () => {
  if (guideStep < 0) showSection(location.hash.slice(1));
});

// ---------------------------------------------------------------- start

async function start() {
  try {
    session = await api('/api/admin/session');
  } catch (err) {
    banner(`Could not reach the agent: ${err.message}`);
    return;
  }
  let next;
  try {
    next = await api('/api/config');
  } catch (err) {
    if (err.status === 401) {
      needsUnlockToView();
      return;
    }
    banner(`Could not load the settings: ${err.message}`);
    return;
  }
  await refreshDevice();
  shown = true;
  $('shell').hidden = false;
  load(next, { render: false });
  const recalled = recalledGuide();
  if (recalled !== null && recalled >= 0 && recalled < GUIDE.length) guideStep = recalled;
  else if (next.configured === false && recalled !== -1) guideStep = 0;
  const hash = location.hash.slice(1);
  if (SECTIONS.some((s) => s.id === hash)) current = hash;
  renderAll();
  if (guideStep < 0) showSection(current, { smooth: false });
}

start();
