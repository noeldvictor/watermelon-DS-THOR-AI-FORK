// Library: every game the pipeline knows (recipes, work folders, packs), setup state, new projects.
import { get, post } from '../api.js';
import { LogView } from '../logview.js';
import { busy, clear, emptyState, formatBytes, formatCount, h, notice, timeAgo, toast } from '../ui.js';

function statusClass(status) {
  const s = (status || '').toLowerCase();
  if (s.startsWith('finished')) return 'green';
  if (s.startsWith('in progress')) return 'gold';
  return '';
}

function packLine(pack) {
  if (!pack || !pack.built) return null;
  const parts = [];
  if (pack.size_bytes !== null && pack.size_bytes !== undefined) parts.push(formatBytes(pack.size_bytes));
  else parts.push('measuring...');
  if (pack.images) parts.push(`${formatCount(pack.images)} images`);
  if (pack.scale) parts.push(`${pack.scale}x`);
  return h('p', { class: 'small muted' }, `Pack: ${parts.join(' · ')}`);
}

function gameCard(g) {
  const thumb = h('div', { class: 'game-thumb' },
    g.image ? h('img', { src: g.image, alt: `${g.title}: original vs HD pack`, loading: 'lazy' }) : h('div', { class: 'placeholder' }, g.code));
  const stage = (label, ts) => h('span', { class: `stage ${ts ? 'done' : ''}`, title: ts ? `${label} ${timeAgo(ts)}` : `Not ${label.toLowerCase()} yet` },
    `${ts ? '✓ ' : ''}${label}`);
  return h('a', { class: 'game-card', href: `#/game/${g.code}` },
    thumb,
    h('div', { class: 'game-body' },
      h('div', { class: 'row' }, h('span', { class: 'chip' }, g.code), h('span', { class: `pill ${statusClass(g.status)}` }, g.status)),
      h('h3', {}, g.title),
      packLine(g.pack),
      h('div', { class: 'stages' }, stage('Extracted', g.stages.extracted), stage('Upscaled', g.stages.upscaled), stage('Built', g.stages.built))));
}

function setupItem(ok, title, text, optional = false) {
  return h('div', { class: 'setup-item' },
    h('span', { class: `dot ${ok ? 'ok' : optional ? '' : 'warn'}` }),
    h('div', {}, h('b', {}, title), h('p', { class: 'small muted' }, text)));
}

export function render(root) {
  const inner = h('div', { class: 'view-inner' });
  root.append(inner);
  let timer = null;
  let setupLog = null;
  let alive = true;

  const newButton = h('button', { class: 'btn primary', type: 'button' }, '+ New game');
  const newCard = h('div', { class: 'card', hidden: true });
  const setupCard = h('div', { class: 'card' }, h('div', { class: 'skeleton', style: { height: '90px' } }));
  const grid = h('div', { class: 'games' }, [1, 2, 3].map(() => h('div', { class: 'skeleton', style: { height: '260px' } })));

  inner.append(
    h('div', { class: 'page-head' },
      h('div', {},
        h('h1', {}, 'Library'),
        h('p', { class: 'lede' }, 'Each game turns its ROM into an HD texture pack: extract every texture, upscale them on your GPU, build the pack and install it on the Thor.')),
      h('div', { class: 'actions' }, newButton)),
    newCard,
    setupCard,
    grid);

  // ------------------------------------------------------------ new game
  const pathInput = h('input', { type: 'text', placeholder: 'C:\\ROMs\\game.nds', 'aria-label': 'ROM file path' });
  const start = h('button', { class: 'btn primary', type: 'button' }, 'Create and extract');
  const found = h('div', { class: 'rom-pick' });
  const createFrom = async (path) => {
    const res = await post('/api/games/new', { rom_path: path });
    if (res.error) toast(`${res.code} added. ${res.error}`, 'warn', 9000);
    else toast(`${res.code} (${res.title || 'game'}) added; extracting now.`, 'ok');
    location.hash = `#/game/${res.code}`;
  };
  start.addEventListener('click', () => busy(start, () => createFrom(pathInput.value)));
  pathInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') start.click(); });
  newCard.append(
    h('div', { class: 'card-head' },
      h('div', {}, h('h2', {}, 'New game from a ROM'),
        h('p', {}, 'Paste the full path of an unzipped .nds file (in Explorer: Shift + right-click the file, "Copy as path"). The studio reads its game code and starts the extraction, which takes a few minutes.'))),
    h('div', { class: 'row' }, h('div', { class: 'grow' }, pathInput), start),
    h('div', { class: 'stack', style: { marginTop: '14px' } }, h('div', { class: 'section-title' }, 'Found in your ROM folders'), found));
  newButton.addEventListener('click', async () => {
    newCard.hidden = !newCard.hidden;
    if (newCard.hidden) return;
    pathInput.focus();
    clear(found).append(h('p', { class: 'faint small' }, 'Looking...'));
    try {
      const res = await get('/api/roms');
      clear(found);
      if (!res.roms.length) {
        found.append(h('p', { class: 'faint small' }, res.rom_dirs.length
          ? `No .nds files in ${res.rom_dirs.join(', ')}.`
          : 'No ROM folders set. Add one in Settings to pick ROMs from a list.'));
      }
      for (const r of res.roms) {
        const b = h('button', { type: 'button', title: r.path }, h('span', { class: 'chip' }, r.code), h('span', { class: 'grow' }, r.file), h('span', { class: 'faint small' }, formatBytes(r.size)));
        b.addEventListener('click', () => { pathInput.value = r.path; start.focus(); });
        found.append(b);
      }
    } catch (error) {
      clear(found).append(h('p', { class: 'small' }, error.message));
    }
  });

  // ------------------------------------------------------------ data
  async function load() {
    clearTimeout(timer);
    let games, setup;
    try {
      [games, setup] = await Promise.all([get('/api/games'), get('/api/setup')]);
    } catch (error) {
      if (!alive) return;
      clear(grid).append(notice('error', 'The library could not be loaded', error.message));
      timer = setTimeout(load, 5000);
      return;
    }
    if (!alive) return;
    renderSetup(setup);
    clear(grid);
    if (!games.games.length) {
      grid.append(emptyState('No games yet',
        `Start one from a ROM file with "New game". Recipes, work folders and packs are read from ${games.hd_remaster_dir}.`,
        h('button', { class: 'btn primary', type: 'button', onclick: () => newButton.click() }, 'New game')));
    }
    for (const g of games.games) grid.append(gameCard(g));
    const pending = games.games.some((g) => g.pack && g.pack.size_pending) || (setup.job && setup.job.status === 'running');
    timer = setTimeout(load, pending ? 3000 : 15000);
  }

  function renderSetup(s) {
    const ready = s.tool_found && s.venv_ready && s.model_ready;
    const keys3d = s.ai3d_keys || {};
    const run = h('button', { class: `btn ${ready ? '' : 'primary'}`, type: 'button' }, ready ? 'Run setup again' : 'Run setup');
    run.addEventListener('click', () => busy(run, async () => {
      const res = await post('/api/setup/run');
      showSetupLog(res.job);
    }));
    clear(setupCard).append(
      h('div', { class: 'card-head' },
        h('div', {},
          h('h2', {}, ready ? 'Pipeline ready' : 'Set up the pipeline first'),
          h('p', {}, ready
            ? 'Python, PyTorch and the upscale model are installed.'
            : 'Setup installs Python packages with CUDA PyTorch (about 3 GB) and downloads the 4x-UltraSharp model. Needs an NVIDIA GPU and runs once.')),
        run),
      h('div', { class: 'setup-list' },
        setupItem(s.tool_found, 'Pipeline tool', s.tool_found ? 'hd_remaster.py found' : `Not found in ${s.hd_remaster_dir}`),
        setupItem(s.venv_ready, 'Python environment', s.venv_ready ? 'tools/hd_remaster/.venv' : 'Not set up yet: run setup'),
        setupItem(s.model_ready, 'Upscale model', s.model_ready ? '4x-UltraSharp downloaded' : 'Downloads during setup'),
        setupItem(s.adb_found, 'adb', s.adb_found ? 'Found: installing on the Thor works' : 'Not on PATH: install Android platform-tools'),
        setupItem(s.openrouter_key_set, 'AI redraw key', s.openrouter_key_set ? 'OpenRouter key is set (.env)' : 'Optional: only for AI portrait redraws', true),
        setupItem(keys3d.tripo || keys3d.meshy, 'AI 3D model keys', keys3d.tripo || keys3d.meshy
          ? `Tripo: ${keys3d.tripo ? 'set' : 'not set'} · Meshy: ${keys3d.meshy ? 'set' : 'not set'} (.env)`
          : 'Optional: only for AI 3D models (TRIPO_API_KEY or MESHY_API_KEY)', true)));
    // once everything is installed the card is reference material: it moves below the games
    if (ready && !s.job) inner.append(setupCard);
    else inner.insertBefore(setupCard, grid);
    if (s.job) showSetupLog(s.job);
    else if (setupLog) setupCard.append(setupLog.el);
  }

  function showSetupLog(job) {
    if (!setupLog) setupLog = new LogView({ height: '240px', onEnd: () => load() });
    setupLog.el.style.marginTop = '16px';
    setupCard.append(setupLog.el);
    setupLog.show(job);
  }

  load();
  return () => {
    alive = false;
    clearTimeout(timer);
    if (setupLog) setupLog.destroy();
  };
}
