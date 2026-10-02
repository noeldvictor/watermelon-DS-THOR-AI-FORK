// The AYN Thor side panel: connection, what's in front, FPS, live view of both screens, and the
// commands (launch, save/load state, texture packs, close). Anything that changes the device goes
// through guardedPost, which asks before touching a Thor that shows another app.
import { get, guardedPost, post } from './api.js';
import { busy, checkbox, clear, h, sleep, toast } from './ui.js';

const STATUS_MS_OPEN = 4000;
const STATUS_MS_CLOSED = 15000;
const SCREEN_MS = 1500;
const NARROW = '(max-width: 1100px)';

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch { /* private window: no memory, still works */ }
  return null;
}

export class DevicePanel {
  constructor() {
    this.root = document.getElementById('device-panel');
    this.layout = document.getElementById('layout');
    this.toggle = document.getElementById('device-toggle');
    this.dot = document.getElementById('device-dot');
    this.scrim = document.getElementById('scrim');
    this.status = null;
    this.open = false;
    this.screenRun = 0;
    this.urls = {};
    this.build();
    this.toggle.addEventListener('click', () => this.setOpen(!this.open));
    this.scrim.addEventListener('click', () => this.setOpen(false));
    document.addEventListener('visibilitychange', () => this.restartScreens());
    const saved = store('studio.panel');
    this.setOpen(saved ? saved === 'open' : !window.matchMedia(NARROW).matches, false);
    this.statusLoop();
  }

  // ---------------------------------------------------------------- layout

  build() {
    this.headSub = h('div', { class: 'dp-sub' }, 'Checking...');
    this.headDot = h('span', { class: 'dot' });
    this.message = h('div', { class: 'stack' });
    this.fgEl = h('div', { class: 'fg' });
    this.fpsEl = h('div', { class: 'fps', hidden: true });

    this.live = checkbox('Live view', store('studio.live') !== 'off');
    this.live.input.addEventListener('change', () => {
      store('studio.live', this.live.input.checked ? 'on' : 'off');
      this.restartScreens();
    });
    this.updated = h('span', { class: 'faint small' });
    this.screens = {
      top: this.screen('top', 'Top'),
      bottom: this.screen('bottom', 'Bottom (touch)'),
    };

    // launch
    this.romSelect = h('select', { 'aria-label': 'Game on the Thor' }, h('option', { value: '' }, 'Load the Thor\'s ROM list first'));
    this.romFile = h('input', { type: 'text', placeholder: 'or a file name, e.g. Lufia.7z', 'aria-label': 'ROM file name' });
    const loadRoms = h('button', { class: 'btn sm', type: 'button' }, 'Load list');
    loadRoms.addEventListener('click', () => busy(loadRoms, () => this.loadRoms()));
    const launch = h('button', { class: 'btn primary sm', type: 'button' }, 'Launch');
    launch.addEventListener('click', () => busy(launch, () => this.launch()));

    // states
    this.stateName = h('input', { type: 'text', placeholder: 'name, e.g. title_screen', 'aria-label': 'State name', maxlength: '64' });
    this.stateSelect = h('select', { 'aria-label': 'Saved state' }, h('option', { value: '' }, 'Load the list of states'));
    const save = h('button', { class: 'btn sm', type: 'button' }, 'Save');
    save.addEventListener('click', () => busy(save, () => this.saveState()));
    const listStates = h('button', { class: 'btn ghost sm', type: 'button', title: 'Refresh the list' }, 'Refresh');
    listStates.addEventListener('click', () => busy(listStates, () => this.loadStates()));
    const load = h('button', { class: 'btn sm', type: 'button' }, 'Load');
    load.addEventListener('click', () => busy(load, () => this.loadState()));

    // packs + close
    this.packHint = h('div', { class: 'faint small' }, 'Applies to the running game too.');
    this.packOn = h('button', { type: 'button' }, 'On');
    this.packOff = h('button', { type: 'button' }, 'Off');
    this.packOn.addEventListener('click', () => this.setPacks(true));
    this.packOff.addEventListener('click', () => this.setPacks(false));
    const close = h('button', { class: 'btn danger sm', type: 'button' }, 'Close emulator');
    close.addEventListener('click', () => busy(close, () => this.closeEmulator()));
    const hide = h('button', { class: 'btn ghost sm', type: 'button', 'aria-label': 'Hide the Thor panel' }, 'Hide');
    hide.addEventListener('click', () => this.setOpen(false));

    clear(this.root).append(h('div', { class: 'dp' },
      h('section', { class: 'dp-section' },
        h('div', { class: 'dp-head' }, this.headDot, h('div', { class: 'grow' }, h('h2', {}, 'AYN Thor'), this.headSub), hide),
        this.message,
        this.fgEl,
        this.fpsEl),
      h('section', { class: 'dp-section' },
        h('div', { class: 'row' }, this.live, h('span', { class: 'grow' }), this.updated),
        h('div', { class: 'screens' }, this.screens.top.el, this.screens.bottom.el)),
      h('section', { class: 'dp-section' },
        h('div', { class: 'section-title' }, 'Launch a game'),
        h('div', { class: 'row' }, this.romSelect, loadRoms),
        h('div', { class: 'row' }, this.romFile, launch)),
      h('section', { class: 'dp-section' },
        h('div', { class: 'section-title' }, 'Save states (private files)'),
        h('div', { class: 'row' }, this.stateName, save),
        h('div', { class: 'row' }, this.stateSelect, listStates, load),
        h('p', { class: 'faint small' }, 'Stored in the app\'s files, so the game\'s own slots stay untouched.')),
      h('section', { class: 'dp-section' },
        h('div', { class: 'row' },
          h('div', { class: 'grow' }, h('b', {}, 'Texture packs'), this.packHint),
          h('div', { class: 'segmented', role: 'group', 'aria-label': 'Texture packs' }, this.packOn, this.packOff)),
        h('div', { class: 'row' }, h('span', { class: 'grow faint small' }, 'Close the emulator when you are done: the Thor is shared.'), close))));
  }

  screen(which, label) {
    const img = h('img', { alt: `${label} screen of the Thor`, hidden: true });
    const msg = h('div', { class: 'msg' }, 'No picture yet');
    return { img, msg, el: h('div', { class: `screen ${which}` }, img, msg, h('span', { class: 'lbl' }, label)) };
  }

  setOpen(open, remember = true) {
    this.open = open;
    this.layout.classList.toggle('panel-open', open);
    this.toggle.setAttribute('aria-expanded', String(open));
    this.scrim.hidden = !(open && window.matchMedia(NARROW).matches);
    if (remember) store('studio.panel', open ? 'open' : 'closed');
    if (open) this.refreshStatus();
    this.restartScreens();
  }

  // ---------------------------------------------------------------- status

  async statusLoop() {
    for (;;) {
      await this.refreshStatus();
      await sleep(this.open ? STATUS_MS_OPEN : STATUS_MS_CLOSED);
    }
  }

  async refreshStatus() {
    let s;
    try {
      s = await get('/api/device/status');
    } catch (error) {
      s = { connected: false, adb: true, message: error.message };
    }
    this.status = s;
    this.renderStatus(s);
  }

  renderStatus(s) {
    const fg = s.foreground;
    const state = !s.connected ? 'bad' : (fg && fg.kind === 'other') ? 'warn' : 'ok';
    for (const dot of [this.dot, this.headDot]) dot.className = `dot ${state}`;
    this.toggle.title = s.connected ? `${s.model || 'Device'} ${s.serial} - ${fg ? fg.label : ''}` : (s.message || 'Not connected');
    this.headSub.textContent = s.connected ? `${s.model || 'Android device'} · ${s.serial}` : (s.serial ? `${s.serial} · not connected` : 'Not connected');

    clear(this.message);
    if (!s.connected) {
      const box = h('div', { class: 'notice error' }, h('div', { class: 'grow' },
        h('p', {}, h('b', {}, s.adb === false ? 'adb is missing' : 'The Thor is not connected')),
        h('p', { class: 'muted small' }, s.message || 'Connect it with USB debugging on.')));
      if (s.suggested_serial) {
        const use = h('button', { class: 'btn sm', type: 'button' }, `Use ${s.suggested_serial}`);
        use.addEventListener('click', () => busy(use, async () => {
          await post('/api/settings', { settings: { serial: s.suggested_serial } });
          toast(`The studio now talks to ${s.suggested_serial}.`, 'ok');
          await this.refreshStatus();
        }));
        box.append(use);
      }
      this.message.append(box);
    } else if (s.awake === false) {
      this.message.append(h('div', { class: 'notice warn' }, h('p', { class: 'small' }, 'The Thor\'s screen is off: screenshots come out black. Wake it up first.')));
    }

    clear(this.fgEl);
    this.fgEl.className = `fg ${fg && fg.kind === 'other' ? 'other' : ''}`;
    if (s.connected && fg) {
      this.fgEl.append(h('span', { class: 'faint small' }, 'In front'), h('b', {}, fg.label));
      if (fg.kind === 'other') {
        this.fgEl.append(h('span', { class: 'small' }, 'Someone else may be using the Thor. Commands will ask before they run.'));
      }
    }
    this.fpsEl.hidden = !(s.connected && s.app_running && s.fps !== null && s.fps !== undefined);
    if (!this.fpsEl.hidden) {
      this.fpsEl.replaceChildren(h('b', {}, Math.round(s.fps)), h('span', { class: 'muted' }, 'fps'));
    }
    const packs = s.texture_packs;
    this.packOn.className = packs === true ? 'on good' : '';
    this.packOff.className = packs === false ? 'on' : '';
    this.packHint.textContent = packs === null || packs === undefined
      ? 'The current setting shows while the app runs.' : 'Applies to the running game too.';
  }

  // ---------------------------------------------------------------- screens

  restartScreens() {
    this.screenRun++;
    const run = this.screenRun;
    const active = () => run === this.screenRun && this.open && this.live.input.checked && document.visibilityState === 'visible';
    if (!active()) return;
    (async () => {
      while (active()) {
        if (this.status && !this.status.connected) {
          for (const s of Object.values(this.screens)) this.showScreenMessage(s, 'Not connected');
          await sleep(3000);
          continue;
        }
        const okTop = await this.refreshScreen('top', active);
        const okBottom = await this.refreshScreen('bottom', active);
        if (okTop || okBottom) this.updated.textContent = `updated ${new Date().toLocaleTimeString()}`;
        await sleep(okTop || okBottom ? SCREEN_MS : 4000);
      }
    })();
  }

  showScreenMessage(s, text) {
    s.msg.textContent = text;
    s.msg.hidden = false;
    s.img.hidden = true;
  }

  async refreshScreen(which, active) {
    const s = this.screens[which];
    try {
      const res = await fetch(`/api/device/screen/${which}?w=720&t=${Date.now()}`);
      if (!res.ok) {
        let text = 'Could not capture this screen';
        try { text = (await res.json()).error || text; } catch { /* keep the default */ }
        this.showScreenMessage(s, text);
        return false;
      }
      const blob = await res.blob();
      if (!active()) return false;
      const url = URL.createObjectURL(blob);
      const old = this.urls[which];
      s.img.onload = () => { if (old) URL.revokeObjectURL(old); };
      s.img.src = url;
      this.urls[which] = url;
      s.img.hidden = false;
      s.msg.hidden = true;
      return true;
    } catch {
      this.showScreenMessage(s, 'The studio is not answering');
      return false;
    }
  }

  // ---------------------------------------------------------------- commands

  async loadRoms() {
    const res = await get('/api/device/roms');
    const roms = res.roms || [];
    clear(this.romSelect);
    if (!roms.length) {
      this.romSelect.append(h('option', { value: '' }, 'The Thor\'s library is empty'));
      return;
    }
    this.romSelect.append(h('option', { value: '' }, `Pick one of ${roms.length} games`),
      ...roms.map((r) => h('option', { value: r.uri, title: r.file }, r.name)));
    toast(`${roms.length} games on the Thor.`, 'ok', 2500);
  }

  async launch() {
    const uri = this.romSelect.value;
    const file = this.romFile.value.trim();
    if (!uri && !file) { toast('Pick a game from the list (Load list) or type its file name.', 'warn'); return; }
    const res = await guardedPost('/api/device/launch', uri ? { uri } : { file });
    if (!res) return;
    toast(`Launched. In front now: ${res.foreground ? res.foreground.label : 'unknown'}`, 'ok');
    this.refreshStatus();
  }

  stateNameFrom(input) {
    const name = input.trim().replace(/\.ml$/, '');
    if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(name)) {
      toast('Use a name of letters, digits, "-", "_" or "." (no spaces).', 'warn');
      return null;
    }
    return name;
  }

  async saveState() {
    const name = this.stateNameFrom(this.stateName.value);
    if (!name) return;
    const res = await guardedPost('/api/device/save_state', { name });
    if (!res) return;
    toast(`Saved state "${name}".`, 'ok');
    this.loadStates().catch(() => {});
  }

  async loadStates() {
    const res = await get('/api/device/states');
    const states = res.states || [];
    const keep = this.stateSelect.value;
    clear(this.stateSelect);
    this.stateSelect.append(h('option', { value: '' }, states.length ? `Pick one of ${states.length} states` : 'No private states yet'),
      ...states.map((s) => h('option', { value: s.replace(/\.ml$/, '') }, s)));
    if (keep) this.stateSelect.value = keep;
  }

  async loadState() {
    const name = this.stateSelect.value || this.stateName.value.trim();
    if (!name) { toast('Pick a state (Refresh lists them) or type its name.', 'warn'); return; }
    const clean = this.stateNameFrom(name);
    if (!clean) return;
    const res = await guardedPost('/api/device/load_state', { name: clean });
    if (!res) return;
    toast(/success=1/.test(res.reply || '') || !res.reply ? `Loaded "${clean}".` : `Load answered: ${res.reply}`, 'ok');
  }

  async setPacks(on) {
    const button = on ? this.packOn : this.packOff;
    await busy(button, async () => {
      const res = await guardedPost('/api/device/texture_packs', { on });
      if (!res) return;
      toast(`Texture packs ${on ? 'on' : 'off'}${res.appliedToRunningGame ? ' (applied to the running game)' : ''}.`, 'ok');
      await this.refreshStatus();
    });
  }

  async closeEmulator() {
    const res = await guardedPost('/api/device/close', {});
    if (!res) return;
    toast('Emulator closed.', 'ok');
    this.refreshStatus();
  }
}
