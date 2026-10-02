// Game page: ROM, the pipeline steps as buttons, live output, job history, recipe and pack info,
// and the 3D models section (models3d.js).
import { get, guardedPost, post } from '../api.js';
import { LogView } from '../logview.js';
import {
  busy, checkbox, clear, emptyState, field, formatBytes, formatCount, formatDuration, formatTime,
  h, kv, notice, select, statusPill, stepCard, timeAgo, toast,
} from '../ui.js';
import { ModelsSection } from './models3d.js';

// steps that change something on the Thor: the server may ask first (shared device)
const DEVICE_STEPS = ['push', 'models_push'];

export function render(root, params) {
  const code = (params[0] || '').toUpperCase();
  const inner = h('div', { class: 'view-inner' });
  root.append(inner);
  let data = null;
  let alive = true;
  let timer = null;

  const log = new LogView({
    emptyText: 'Run a step and its output appears here, live.',
    onEnd: (job) => {
      toast(`${job.title}: ${job.status === 'done' ? 'finished' : job.status}.`, job.status === 'done' ? 'ok' : job.status === 'failed' ? 'error' : 'warn');
      refresh();
      if ((job.kind || '').startsWith('models_')) models.refresh();
    },
  });

  // ------------------------------------------------------------ header
  const titleEl = h('h1', {}, code);
  const pills = h('div', { class: 'row' });
  const head = h('div', { class: 'page-head' },
    h('div', {}, h('div', { class: 'crumbs' }, h('a', { href: '#/library' }, 'Library'), ` / ${code}`),
      h('div', { class: 'title-row' }, titleEl, pills)));

  // ------------------------------------------------------------ ROM
  const romInfo = h('div', { class: 'stack' });
  const romInput = h('input', { type: 'text', placeholder: 'C:\\ROMs\\game.nds', 'aria-label': 'ROM file path' });
  const romSave = h('button', { class: 'btn', type: 'button' }, 'Use this ROM');
  romSave.addEventListener('click', () => busy(romSave, async () => {
    await post(`/api/games/${code}/rom`, { rom_path: romInput.value });
    toast('ROM saved for this game.', 'ok');
    romInput.value = '';
    refresh();
  }));
  romInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') romSave.click(); });
  const romCard = h('div', { class: 'card' },
    h('div', { class: 'card-head' }, h('div', {}, h('h2', {}, 'ROM'), h('p', {}, 'Extract and the all-in-one run read the ROM; the other steps work from the extraction.'))),
    romInfo,
    h('div', { class: 'row', style: { marginTop: '12px' } }, h('div', { class: 'grow' }, romInput), romSave));

  // ------------------------------------------------------------ steps
  const model = select([['', 'Recipe default']]);
  const scale = select([['', 'Recipe default'], ['4', '4x'], ['2', '2x']]);
  const force = checkbox('Redo images already upscaled');
  const redo = checkbox('Redo only images with transparency');
  const only = h('input', { type: 'text', placeholder: 'e.g. 2d/bustup/', 'aria-label': 'Only sources containing' });
  const upscaleOptions = () => ({ model: model.value, scale: scale.value, force: force.input.checked, redo_cutouts: redo.input.checked, only: only.value.trim() });
  const native = checkbox('Native 1x test pack (should look exactly like no pack)');
  const dumps = h('input', { type: 'text', placeholder: 'folder with manifest.jsonl', 'aria-label': 'Texture dump folder' });
  const sprites = h('input', { type: 'text', placeholder: 'optional', 'aria-label': 'Sprite dump folder' });
  const apply = checkbox('Add the pairings found to the recipe');

  const steps = {
    extract: stepCard(1, 'Extract', 'Decodes every 3D texture, sprite, background and font in the ROM, each named by the key the emulator looks it up by.'),
    upscale: stepCard(2, 'Upscale', 'AI-upscales every image on your GPU. Stopping is safe: the next run resumes where it left off.',
      [field('Model', model), field('Scale', scale), force, redo, field('Only sources containing', only, 'Redo a subset after changing a recipe override.')]),
    build: stepCard(3, 'Build', 'Assembles the pack folder (one image per key) from the upscaled images.', [native]),
    push: stepCard(4, 'Install on Thor', 'Copies the pack to the Thor. The game loads it the next time it starts; an older pack is kept as a backup.'),
  };
  const extra = {
    all: stepCard(null, 'All in one', 'Extract, upscale and build in one go, with the Upscale options above.'),
    verify: stepCard(null, 'Verify', 'Compares the extraction with textures dumped in the game, key by key and pixel by pixel.',
      [field('Texture dump folder', dumps), field('Sprite dump folder', sprites)], true),
    misses: stepCard(null, 'Find misses', 'After playing with the pack: reads the Thor\'s log and names the ROM file and palette of each sprite the pack missed.', [apply]),
  };
  const optionsFor = {
    extract: () => ({}), upscale: upscaleOptions, build: () => ({ native: native.input.checked }), push: () => ({}),
    all: upscaleOptions, verify: () => ({ dumps: dumps.value, sprites: sprites.value }), misses: () => ({ apply: apply.input.checked }),
  };
  for (const [name, card] of Object.entries({ ...steps, ...extra })) {
    card.run.addEventListener('click', () => busy(card.run, () => runStep(name, optionsFor[name]())));
  }
  extra.all.run.textContent = 'Run all';
  steps.push.run.textContent = 'Install';

  async function runStep(step, options) {
    const send = DEVICE_STEPS.includes(step) ? guardedPost : post;
    const res = await send(`/api/games/${code}/run`, { step, options });
    if (!res) return null;
    log.show(res.job);
    toast(`${res.job.title} ${res.job.status === 'queued' ? 'queued' : 'started'}.`, 'ok', 2500);
    log.el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    refresh();
    return res.job;
  }

  const models = new ModelsSection(code, { runStep });

  // ------------------------------------------------------------ jobs, recipe, pack
  const jobList = h('div', { class: 'job-list' });
  const recipeCard = h('div', { class: 'card' });
  const packCard = h('div', { class: 'card' });
  const status = h('div', { hidden: true });

  inner.append(
    head,
    status,
    romCard,
    h('div', {}, h('div', { class: 'section-title' }, 'Pipeline'), h('div', { class: 'steps' }, steps.extract.el, steps.upscale.el, steps.build.el, steps.push.el)),
    h('div', { class: 'extra-steps' }, extra.all.el, extra.verify.el, extra.misses.el),
    models.el,
    log.el,
    h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('div', {}, h('h2', {}, 'Job history'), h('p', {}, 'This session only: the list starts empty when the studio restarts.'))), jobList),
    h('div', { class: 'split' }, recipeCard, packCard));

  // ------------------------------------------------------------ rendering
  function update(d) {
    data = d;
    titleEl.textContent = d.title;
    document.title = `${d.title} - Remaster Studio`;
    clear(pills).append(h('span', { class: 'chip' }, d.code),
      h('span', { class: `pill ${/^finished/i.test(d.status) ? 'green' : /^in progress/i.test(d.status) ? 'gold' : ''}` }, d.status));

    // models list once
    if (model.options.length === 1 && d.models.length) {
      const def = d.recipe && d.recipe.models ? d.recipe.models.textures : '4x-UltraSharp';
      model.options[0].textContent = `Recipe default (${def || '4x-UltraSharp'})`;
      for (const m of d.models) model.append(h('option', { value: m }, m));
      const rs = d.recipe && d.recipe.scale;
      scale.options[0].textContent = `Recipe default (${rs || 4}x)`;
    }

    // ROM
    clear(romInfo);
    const r = d.rom;
    for (const path of r.suggestions || []) {
      const use = h('button', { class: 'btn sm', type: 'button' }, 'Use it');
      use.addEventListener('click', () => busy(use, async () => {
        await post(`/api/games/${code}/rom`, { rom_path: path });
        toast('ROM saved for this game.', 'ok');
        refresh();
      }));
      romInfo.append(notice('ok', `Found ${d.code} in your ROM folders`, path, use));
    }
    if (!r.path) {
      // a found ROM (above) already says what to do
      if (!(r.suggestions || []).length) {
        romInfo.append(notice('warn', 'No ROM file set for this game', 'Paste the path of its .nds file below. Upscale, Build and Install work without it once the game is extracted.'));
      }
    } else if (!r.exists) {
      romInfo.append(notice('error', 'The ROM file is missing', `${r.path} isn't there any more. Paste its new location below.`));
    } else if (r.error || r.code_matches === false) {
      romInfo.append(notice('error', 'This ROM is a different game', r.error || `${r.path} is ${r.code}, not ${d.code}.`));
    } else {
      const match = d.work.rom_match
        ? `Matches the recipe: ${d.work.rom_match}.`
        : d.recipe && d.work.rom_sha256 ? 'Not a ROM this recipe was verified on (another revision or a patch): it still runs, results may differ.' : '';
      romInfo.append(kv([['File', r.path], ['Header', `${r.code} · ${r.title || 'untitled'}`], ['Recipe check', match]]));
    }

    // steps
    const st = d.stages;
    const setState = (card, done, text) => { card.state.textContent = text; card.state.className = `state ${done ? 'done' : ''}`; card.el.classList.toggle('done', !!done); };
    setState(steps.extract, st.extracted, st.extracted ? `✓ Extracted ${timeAgo(st.extracted)}` : (r.exists ? 'Not run yet' : 'Needs the ROM file'));
    const up = d.work.upscale;
    setState(steps.upscale, st.upscaled, st.upscaled ? `✓ Upscaled ${timeAgo(st.upscaled)}${up && up.scale ? ` · ${up.scale}x` : ''}` : (st.extracted ? 'Ready to run' : 'Needs Extract first'));
    const p = d.pack;
    const size = p.size_bytes !== null && p.size_bytes !== undefined ? formatBytes(p.size_bytes) : (p.built ? 'measuring size...' : '');
    setState(steps.build, st.built, st.built ? `✓ Built ${timeAgo(st.built)} · ${formatCount(p.images)} images${size ? ` · ${size}` : ''}` : (st.upscaled ? 'Ready to run' : 'Needs Upscale (or a native test build)'));
    setState(steps.push, false, st.built ? `Pack ready to install${size ? ` (${size})` : ''}` : 'Needs Build first');
    setState(extra.all, false, r.exists ? 'Takes from minutes to an hour, depending on the game' : 'Needs the ROM file');
    setState(extra.verify, false, st.extracted ? 'Needs a dump folder' : 'Needs Extract first');
    setState(extra.misses, false, st.extracted ? 'Reads the device log (no changes on the Thor)' : 'Needs Extract first');
    // only the next step is highlighted, so the page reads as "do this now"
    const next = !st.extracted ? 'extract' : !st.upscaled ? 'upscale' : !st.built ? 'build' : 'push';
    for (const [name, card] of Object.entries(steps)) card.run.className = `btn ${name === next ? 'primary' : ''}`;
    for (const card of Object.values(extra)) card.run.className = 'btn';

    // jobs
    clear(jobList);
    if (!d.jobs.length) jobList.append(h('p', { class: 'faint small' }, 'No jobs for this game yet in this session.'));
    for (const j of d.jobs) {
      const row = h('button', { class: `job-row ${log.job && log.job.id === j.id ? 'selected' : ''}`, type: 'button' },
        statusPill(j.status),
        h('div', {}, h('b', {}, j.title), h('div', { class: 'faint small' }, j.duration ? `took ${formatDuration(j.duration)}` : '')),
        h('span', { class: 'when' }, formatTime(j.created)));
      row.addEventListener('click', () => { log.show(j); update(data); });
      jobList.append(row);
    }
    if (!log.job && d.jobs.length) {
      log.show(d.jobs.find((j) => ['running', 'queued'].includes(j.status)) || d.jobs[0]);
    }

    renderRecipe(d);
    renderPack(d);
    models.setGame(d);
  }

  function renderRecipe(d) {
    const rc = d.recipe;
    clear(recipeCard).append(h('div', { class: 'card-head' }, h('div', {}, h('h2', {}, 'Recipe'),
      h('p', {}, rc ? `tools/hd_remaster/games/${d.code}/recipe.json (read-only here)` : 'No recipe yet'))));
    if (!rc) {
      recipeCard.append(h('p', { class: 'muted' }, `The pipeline runs with the defaults: 4x, 4x-UltraSharp for everything. Once the pack works, a recipe (games/${d.code}/recipe.json) records the ROM checksum, models, baseline counts and game-specific rules, so anyone with the same ROM gets the same pack. See games/README.md.`));
      return;
    }
    const models = rc.models ? Object.entries(rc.models).map(([k, v]) => `${k}: ${v}`).join(' · ') : '';
    const base = rc.baseline ? Object.entries(rc.baseline).map(([k, v]) => `${k} ${formatCount(v)}`).join(' · ') : '';
    recipeCard.append(kv([
      ['Title', rc.title],
      ['Verified ROMs', (rc.roms || []).map((x) => `${x.name} (${(x.sha256 || '').slice(0, 12)}...)`).join('; ')],
      ['Scale', rc.scale ? `${rc.scale}x` : ''],
      ['Models', models],
      ['Overrides', (rc.overrides || []).map((o) => `${o.match} -> ${o.model || ''}`).join('; ')],
      ['Baseline keys', base],
      ['2D rules', rc.twod && Object.keys(rc.twod).length ? Object.keys(rc.twod).join(', ') : ''],
    ]));
    if (rc.verified) {
      recipeCard.append(h('div', { class: 'section-title', style: { marginTop: '16px' } }, 'Verified'),
        kv(Object.entries(rc.verified).map(([k, v]) => [k.replace(/_/g, ' '), typeof v === 'string' ? v : JSON.stringify(v)])));
    }
    if (rc.notes && rc.notes.length) {
      recipeCard.append(h('div', { class: 'section-title', style: { marginTop: '16px' } }, 'Notes'),
        h('ul', { class: 'notes' }, rc.notes.map((n) => h('li', {}, n))));
    }
    recipeCard.append(h('details', { class: 'raw', style: { marginTop: '14px' } }, h('summary', {}, 'Full recipe.json'),
      h('pre', {}, JSON.stringify(rc, null, 2))));
  }

  function renderPack(d) {
    const p = d.pack;
    const w = d.work;
    clear(packCard).append(h('div', { class: 'card-head' }, h('div', {}, h('h2', {}, 'Pack and work folders'), h('p', {}, 'On this PC. Packs and work folders hold game assets: they are never committed.'))));
    if (p.built) {
      const models = p.upscale && p.upscale.models ? [...new Set(Object.values(p.upscale.models))].join(', ') : '';
      packCard.append(kv([
        ['Pack folder', p.path],
        ['Images', formatCount(p.images)],
        ['Scale', p.scale ? `${p.scale}x` : ''],
        ['Size', p.size_bytes !== null && p.size_bytes !== undefined ? `${formatBytes(p.size_bytes)} in ${formatCount(p.files)} files` : 'measuring...'],
        ['Built', formatTime(p.built)],
        ['Made from', p.source === 'native' ? 'native images (1x test pack)' : 'upscaled images'],
        ['Models', models],
      ]));
    } else {
      packCard.append(h('p', { class: 'muted' }, 'No pack built yet.'));
    }
    packCard.append(h('div', { class: 'section-title', style: { marginTop: '16px' } }, 'Work folder'),
      kv([
        ['Folder', w.exists ? w.path : `${w.path} (not created yet)`],
        ['Contents', [w.has_native && 'native images', w.upscale && 'upscaled images', w.has_redrawn && 'AI redraws'].filter(Boolean).join(', ') || 'empty'],
        ['ROM SHA-256', w.rom_sha256],
      ]));
  }

  async function refresh() {
    clearTimeout(timer);
    try {
      const d = await get(`/api/games/${code}`);
      if (!alive) return;
      clear(status).hidden = true;
      update(d);
      const busyNow = d.jobs.some((j) => ['running', 'queued'].includes(j.status)) || (d.pack && d.pack.size_pending);
      timer = setTimeout(refresh, busyNow ? 4000 : 20000);
    } catch (error) {
      if (!alive) return;
      if (error.status === 404) {
        clear(inner).append(head, emptyState(`No game ${code}`, 'It isn\'t in the library: no recipe, work folder, pack or ROM for this code.',
          h('a', { class: 'btn', href: '#/library' }, 'Back to the library')));
        return;
      }
      clear(status).append(notice('error', 'Could not load this game', error.message));
      status.hidden = false;
      timer = setTimeout(refresh, 5000);
    }
  }

  refresh();
  models.refresh();
  return () => {
    alive = false;
    clearTimeout(timer);
    log.destroy();
    models.destroy();
  };
}
