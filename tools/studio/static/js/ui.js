// Small DOM and formatting helpers shared by the views. No framework: h() builds elements.

/** Build an element: h('button', {class: 'btn', onclick: fn}, 'Label', child, ...). */
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'text') el.textContent = value;
    else if (key === 'dataset') Object.assign(el.dataset, value);
    else if (key === 'style' && typeof value === 'object') Object.assign(el.style, value);
    else if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), value);
    else if (key === 'value' && 'value' in el) el.value = value;
    else if (value === true) el.setAttribute(key, '');
    else el.setAttribute(key, String(value));
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const child of [children].flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : String(child));
  }
  return el;
}

export function clear(el) {
  while (el.firstChild) el.firstChild.remove();
  return el;
}

/** A labelled form field around an input. */
export function field(label, input, hint) {
  return h('label', { class: 'field' }, h('span', {}, label), input, hint ? h('span', { class: 'hint' }, hint) : null);
}

export function checkbox(label, checked = false) {
  const input = h('input', { type: 'checkbox' });
  input.checked = checked;
  const wrap = h('label', { class: 'check' }, input, label);
  wrap.input = input;
  return wrap;
}

export function select(options, value) {
  const el = h('select', {}, options.map(([v, label]) => h('option', { value: v }, label)));
  if (value !== undefined) el.value = value;
  return el;
}

/** A definition list from [label, value] pairs; empty values are left out. */
export function kv(pairs) {
  const dl = h('dl', { class: 'kv' });
  for (const [k, v] of pairs) {
    if (v === null || v === undefined || v === '') continue;
    dl.append(h('dt', {}, k), h('dd', {}, v));
  }
  return dl;
}

/** A pipeline step card (Game page): title, text, state line, options behind a toggle, Run. */
export function stepCard(num, title, text, options, openOptions = false) {
  const state = h('div', { class: 'state' }, '');
  const run = h('button', { class: 'btn primary', type: 'button' }, 'Run');
  const optsEl = options ? h('div', { class: `options ${openOptions ? 'open' : ''}` }, options) : null;
  const toggle = options && !openOptions ? h('button', { class: 'btn ghost', type: 'button', 'aria-expanded': 'false' }, 'Options') : null;
  if (toggle) {
    toggle.addEventListener('click', () => {
      const open = !optsEl.classList.contains('open');
      optsEl.classList.toggle('open', open);
      toggle.setAttribute('aria-expanded', String(open));
    });
  }
  const el = h('div', { class: 'step' },
    h('div', { class: 'row' }, num ? h('span', { class: 'step-num' }, num) : null, h('h3', {}, title)),
    h('p', {}, text),
    state,
    optsEl,
    h('div', { class: 'btn-row' }, run, toggle));
  return { el, state, run };
}

// ------------------------------------------------------------------ feedback

export function toast(message, kind = 'info', ms = 5500) {
  const box = document.getElementById('toasts');
  const el = h('div', { class: `toast ${kind}`, role: kind === 'error' ? 'alert' : 'status' }, message);
  box.append(el);
  setTimeout(() => el.remove(), kind === 'error' ? ms * 1.6 : ms);
}

export function notice(kind, title, text, action) {
  return h('div', { class: `notice ${kind}` },
    h('div', { class: 'grow' }, title ? h('p', {}, h('b', {}, title)) : null, text ? h('p', { class: 'muted' }, text) : null),
    action || null);
}

export function emptyState(title, text, action) {
  return h('div', { class: 'empty' }, h('h3', {}, title), text ? h('p', {}, text) : null, action || null);
}

/** Ask a yes/no question in the shared <dialog>. Resolves true when confirmed. */
export function confirmDialog({ title, message, detail, confirm = 'Continue', cancel = 'Cancel', danger = false }) {
  const dialog = document.getElementById('dialog');
  dialog.className = 'dialog';
  return new Promise((resolve) => {
    const done = (value) => { resolve(value); dialog.close(); };
    clear(dialog).append(
      h('div', { class: 'dialog-body' },
        h('h2', {}, title),
        h('p', {}, message),
        detail ? h('code', { class: 'chip' }, detail) : null),
      h('div', { class: 'dialog-actions' },
        h('button', { class: 'btn ghost', type: 'button', onclick: () => done(false) }, cancel),
        h('button', { class: `btn ${danger ? 'danger' : 'primary'}`, type: 'button', onclick: () => done(true) }, confirm)));
    dialog.onclose = () => resolve(false);
    dialog.onclick = (e) => { if (e.target === dialog) done(false); };
    dialog.showModal();
  });
}

/** Show an image large (flagged strips). */
export function lightbox(src, caption) {
  const dialog = document.getElementById('dialog');
  dialog.className = 'dialog wide';
  clear(dialog).append(
    h('div', { class: 'dialog-body' }, h('img', { class: 'full', src, alt: caption || '' }), caption ? h('p', {}, caption) : null),
    h('div', { class: 'dialog-actions' },
      h('a', { class: 'btn ghost', href: src, target: '_blank', rel: 'noopener' }, 'Open full size'),
      h('button', { class: 'btn', type: 'button', onclick: () => dialog.close() }, 'Close')));
  dialog.onclose = null;
  dialog.onclick = (e) => { if (e.target === dialog) dialog.close(); };
  dialog.showModal();
}

/** Run an async action with the button disabled and spinning; errors become toasts. */
export async function busy(button, action) {
  const label = Array.from(button.childNodes);
  button.disabled = true;
  button.replaceChildren(h('span', { class: 'spin' }), ...label);
  try {
    return await action();
  } catch (error) {
    toast(error.message || String(error), 'error');
    return undefined;
  } finally {
    button.disabled = false;
    button.replaceChildren(...label);
  }
}

// ------------------------------------------------------------------ formatting

export function formatBytes(n) {
  if (n === null || n === undefined) return '';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)} ${units[i]}`;
}

export function formatCount(n) {
  return n === null || n === undefined ? '' : Number(n).toLocaleString();
}

export function timeAgo(ts) {
  if (!ts) return '';
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 45) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 86400 * 14) return `${Math.round(s / 86400)} days ago`;
  return new Date(ts * 1000).toLocaleDateString();
}

export function formatDuration(sec) {
  if (sec === null || sec === undefined) return '';
  sec = Math.round(sec);
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  if (m < 60) return `${m}m ${String(sec % 60).padStart(2, '0')}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`;
}

export function formatTime(ts) {
  return ts ? new Date(ts * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : '';
}

const STATUS = {
  queued: ['', 'Queued'], running: ['gold running', 'Running'], done: ['green', 'Done'],
  failed: ['red', 'Failed'], cancelled: ['', 'Cancelled'],
};

export function statusPill(status) {
  const [cls, label] = STATUS[status] || ['', status];
  return h('span', { class: `pill ${cls}` }, label);
}

export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
