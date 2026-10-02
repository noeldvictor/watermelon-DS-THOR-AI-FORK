// Settings: the device and folders the studio uses (stored in tools/studio/studio_settings.json).
import { get, post } from '../api.js';
import { busy, checkbox, clear, field, h, notice, toast } from '../ui.js';

export function render(root) {
  const inner = h('div', { class: 'view-inner' });
  root.append(inner);
  let alive = true;
  const body = h('div', { class: 'stack' }, h('div', { class: 'skeleton', style: { height: '320px' } }));
  inner.append(
    h('div', { class: 'page-head' }, h('div', {}, h('h1', {}, 'Settings'),
      h('p', { class: 'lede' }, 'Which device to drive and where the studio finds ROMs and results. API keys are not stored here: they stay in tools/hd_remaster/.env.'))),
    body);

  async function load() {
    let res;
    try {
      res = await get('/api/settings');
    } catch (error) {
      clear(body).append(notice('error', 'Could not read the settings', error.message));
      return;
    }
    if (!alive) return;
    const s = res.settings;
    const input = (value, placeholder) => h('input', { type: 'text', value: value ?? '', placeholder: placeholder || '' });
    const lines = (list) => h('textarea', { rows: '3' }, (list || []).join('\n'));
    const f = {
      serial: input(s.serial, 'blank: the attached AYN Thor'),
      package: input(s.package),
      bottom_display: input(s.bottom_display),
      rom_tree_uri: input(s.rom_tree_uri),
      rom_dirs: lines(s.rom_dirs),
      hd_remaster_dir: input(s.hd_remaster_dir, res.hd_remaster_dir),
      python: input(s.python, res.python),
      checks_out_dir: input(s.checks_out_dir, res.checks_out_dir),
      checks_baseline_roots: lines(s.checks_baseline_roots),
      port: h('input', { type: 'number', value: s.port, min: '1024', max: '65535' }),
    };
    const openBrowser = checkbox('Open the browser when the studio starts', s.open_browser !== false);
    const save = h('button', { class: 'btn primary', type: 'button' }, 'Save settings');
    save.addEventListener('click', () => busy(save, async () => {
      const patch = { open_browser: openBrowser.input.checked };
      for (const [key, el] of Object.entries(f)) patch[key] = el.tagName === 'TEXTAREA' ? el.value.split('\n') : el.value;
      await post('/api/settings', { settings: patch });
      toast(Number(f.port.value) !== res.settings.port ? 'Saved. The new port applies when the studio restarts.' : 'Saved.', 'ok');
      load();
    }));

    clear(body).append(
      h('div', { class: 'card' },
        h('div', { class: 'card-head' }, h('div', {}, h('h2', {}, 'Device'), h('p', {}, 'The AYN Thor, reached through adb.'))),
        h('div', { class: 'form-grid' },
          field('adb serial', f.serial, 'From "adb devices". Wireless adb uses ip:port, which changes.'),
          field('App package', f.package, 'The debug build: app.watermelonthor.dev'),
          field('Bottom display id', f.bottom_display, 'For screenshots of the touch screen')),
        h('div', { style: { marginTop: '14px' } }, field('ROM folder on the device (tree URI)', f.rom_tree_uri, 'Launching by file name appends the file name to this.'))),
      h('div', { class: 'card' },
        h('div', { class: 'card-head' }, h('div', {}, h('h2', {}, 'Pipeline'), h('p', {}, 'Leave a field blank for the default shown in grey.'))),
        h('div', { class: 'stack' },
          field('ROM folders on this PC (one per line)', f.rom_dirs, '"New game" lists the .nds files in these folders.'),
          h('div', { class: 'form-grid' },
            field('hd_remaster folder', f.hd_remaster_dir),
            field('Python for jobs', f.python, 'Default: the hd_remaster .venv')))),
      h('div', { class: 'card' },
        h('div', { class: 'card-head' }, h('div', {}, h('h2', {}, 'Checks'), h('p', {}, 'Where frame_compare writes runs, and folders with earlier runs to use as baselines.'))),
        h('div', { class: 'stack' },
          field('Output folder', f.checks_out_dir),
          field('Baseline folders (one per line)', f.checks_baseline_roots, 'Each subfolder is a run, e.g. ...\\frame_compare_baselines'))),
      h('div', { class: 'card' },
        h('div', { class: 'card-head' }, h('div', {}, h('h2', {}, 'Studio'), h('p', {}, `Settings file: ${res.file}`))),
        h('div', { class: 'row' }, h('div', { style: { width: '160px' } }, field('Port', f.port)), openBrowser)),
      h('div', { class: 'row' }, h('span', { class: 'grow' }), save));
  }

  load();
  return () => { alive = false; };
}
