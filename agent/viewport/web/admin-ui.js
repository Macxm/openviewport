// Building blocks for the admin page: rows, switches, number fields with units, info
// toggles and icons. They build elements; they do not know about the configuration.

export function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

let uid = 0;
export function nextId() {
  uid += 1;
  return `f${uid}`;
}

// ---------------------------------------------------------------- icons

// Drawn with stroke="currentColor", so an icon takes the colour of the text beside it.
const ICONS = {
  lock: '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  unlock: '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 7.6-1.7"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 8h.01"/>',
  chevron: '<path d="m7 10 5 5 5-5"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
  warning: '<path d="M12 4 2.5 20h19Z"/><path d="M12 10v4"/><path d="M12 17h.01"/>',
  up: '<path d="M12 19V5M6 11l6-6 6 6"/>',
  down: '<path d="M12 5v14M6 13l6 6 6-6"/>',
  check: '<path d="m5 12 5 5 9-10"/>',
};

export function icon(name) {
  const span = el('span', `icon icon-${name}`);
  span.setAttribute('aria-hidden', 'true');
  span.innerHTML = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" `
    + `stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${ICONS[name]}</svg>`;
  return span;
}

// ---------------------------------------------------------------- pressable

/**
 * Something to click that is not a form control. Settings sit in a fieldset that is
 * disabled while the page is locked, which disables every button in it; toggles that
 * only show or hide information must keep working, so they are built from this instead.
 */
export function pressable(node, onPress) {
  node.setAttribute('role', 'button');
  node.tabIndex = 0;
  node.addEventListener('click', onPress);
  node.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' || ev.key === ' ') {
      ev.preventDefault();
      onPress(ev);
    }
  });
  return node;
}

// ---------------------------------------------------------------- rows and groups

/**
 * One setting: its name, a line saying what it does, optionally more behind an (i), and
 * its control beside it (or below it on a phone, unless it is small).
 */
export function setting({ label, help, info, control, fit = false, stacked = false }) {
  const row = el('div', stacked ? 'setting stacked' : 'setting');
  const text = el('div', 'setting-text');
  const input = control.matches('input, select, textarea') ? control
    : control.querySelector('input, select, textarea');
  const head = el('div', 'setting-head');
  const name = el(input ? 'label' : 'span', 'setting-label', label);
  if (input) {
    if (!input.id) input.id = nextId();
    name.htmlFor = input.id;
  }
  head.append(name);
  text.append(head);
  const described = [];
  if (help) {
    const note = el('div', 'setting-help');
    note.id = nextId();
    note.append(help);
    text.append(note);
    described.push(note.id);
  }
  if (info) {
    const more = el('p', 'setting-info');
    more.id = nextId();
    more.hidden = true;
    more.append(info);
    const toggle = pressable(el('span', 'info-toggle'), () => {
      more.hidden = !more.hidden;
      toggle.setAttribute('aria-expanded', String(!more.hidden));
    });
    toggle.setAttribute('aria-label', `More about “${label}”`);
    toggle.setAttribute('aria-expanded', 'false');
    toggle.setAttribute('aria-controls', more.id);
    toggle.title = 'More about this';
    toggle.append(icon('info'));
    head.append(toggle);
    text.append(more);
  }
  if (input && described.length) input.setAttribute('aria-describedby', described.join(' '));
  const box = el('div', fit ? 'setting-control fit' : 'setting-control');
  box.append(control);
  row.append(text, box);
  return row;
}

export function rowsOf(settings) {
  const rows = el('div', 'rows');
  rows.append(...settings);
  return rows;
}

/** Rows under a heading. `content` is a list of settings, or one node of its own. */
export function group(title, content, { note, advanced = false } = {}) {
  const body = Array.isArray(content) ? rowsOf(content) : content;
  if (advanced) {
    const details = el('details', 'group');
    details.append(el('summary', 'group-title', title), body);
    return details;
  }
  const box = el('div', 'group');
  if (title) box.append(el('h3', 'group-title', title));
  box.append(body);
  if (note) box.append(el('p', 'group-note', note));
  return box;
}

export function emptyNote(text) {
  return el('p', 'empty', text);
}

/** A highlighted remark above settings: `warn` for something to fix, otherwise a tip. */
export function notice(text, kind = 'tip') {
  const box = el('div', `notice ${kind}`);
  box.append(icon(kind === 'warn' ? 'warning' : 'info'), el('p', null, text));
  return box;
}

// ---------------------------------------------------------------- controls

export function switchOf(checked) {
  const input = el('input', 'switch');
  input.type = 'checkbox';
  input.setAttribute('role', 'switch');
  input.checked = !!checked;
  return input;
}

export function selectOf(options, value) {
  const select = el('select');
  for (const [v, text] of options) {
    const option = el('option', null, text);
    option.value = v;
    select.append(option);
  }
  select.value = value;
  return select;
}

export function textOf(value, { placeholder = '', type = 'text', autocomplete = 'off', maxLength } = {}) {
  const input = el('input');
  input.type = type;
  input.value = value || '';
  input.placeholder = placeholder;
  input.autocomplete = autocomplete;
  input.spellcheck = false;
  input.setAttribute('autocapitalize', 'none');
  if (maxLength) input.maxLength = maxLength;
  return input;
}

export function numberOf(value, { min, max, step = 'any', integer = false } = {}) {
  const input = el('input');
  input.type = 'number';
  input.inputMode = integer ? 'numeric' : 'decimal';
  if (min !== undefined) input.min = String(min);
  if (max !== undefined) input.max = String(max);
  input.step = String(integer ? 1 : step);
  input.required = true;
  input.value = value === null || value === undefined ? '' : String(value);
  return input;
}

export function withUnit(input, unit) {
  const box = el('span', 'unit-input');
  box.append(input, el('span', 'unit', unit));
  return box;
}

/** Controls that edit `target[key]` in a draft, and call `onChange` when they do. */
export function binder(target, onChange) {
  return {
    switch(key, { after } = {}) {
      const input = switchOf(target[key]);
      input.addEventListener('change', () => {
        target[key] = input.checked;
        after?.();
        onChange();
      });
      return input;
    },
    select(key, options, { after } = {}) {
      const select = selectOf(options, String(target[key]));
      select.addEventListener('change', () => {
        target[key] = typeof target[key] === 'boolean' ? select.value === 'true' : select.value;
        after?.();
        onChange();
      });
      return select;
    },
    number(key, { unit, scale = 1, ...limits } = {}) {
      const shown = target[key] == null ? '' : Math.round(target[key] * scale * 1000) / 1000;
      const input = numberOf(shown, limits);
      input.addEventListener('input', () => {
        // An empty or out-of-range field is still a change: saving reports what is wrong.
        target[key] = input.value === '' ? null : Number(input.value) / scale;
        onChange();
      });
      return unit ? withUnit(input, unit) : input;
    },
  };
}

/** ↑ and ↓ for an item in an ordered list. */
export function moveButtons(index, count, what, move) {
  const box = el('div', 'order-buttons');
  for (const delta of [-1, 1]) {
    const button = el('button', 'icon-button');
    button.type = 'button';
    button.append(icon(delta < 0 ? 'up' : 'down'));
    button.title = delta < 0 ? 'Move up' : 'Move down';
    button.setAttribute('aria-label', `${button.title}: ${what}`);
    button.dataset.move = String(delta);
    button.disabled = index + delta < 0 || index + delta >= count;
    button.addEventListener('click', () => move(delta));
    box.append(button);
  }
  return box;
}

/** After a list redraws, put focus back where it was, so ↑ can be pressed again. */
export function refocus(container, key, selector, fallback) {
  const item = [...container.querySelectorAll('[data-key]')].find((n) => n.dataset.key === key);
  if (!item) return;
  let target = item.querySelector(selector);
  if (target && target.disabled && fallback) target = item.querySelector(fallback);
  target?.focus();
}

/** Every field in a container that the browser's own checks reject, opening "Advanced" if need be. */
export function fieldsValid(container) {
  for (const input of container.querySelectorAll('input, select, textarea')) {
    if (input.closest('[hidden]') || input.checkValidity()) continue;
    const details = input.closest('details');
    if (details) details.open = true;
    input.focus();
    input.reportValidity();
    return false;
  }
  return true;
}
