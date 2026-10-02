// Game page, 3D models: the models a ROM holds (hd_remaster.py models extract), one model's
// shapes and pictures, fitting a mesh, AI models (Tripo or Meshy: paid, so it asks first),
// building the replacements and installing them on the Thor. Jobs go through the game page's
// runStep, so their output shows in its log and its job history.
import { get } from '../api.js';
import {
  busy, checkbox, clear, confirmDialog, emptyState, field, formatCount, h, kv, lightbox, notice,
  select, stepCard, timeAgo,
} from '../ui.js';

const PAGE = 120;           // rows shown before "Show more": a ROM can hold over a thousand models
const PROVIDERS = { tripo: 'Tripo', meshy: 'Meshy' };
const KEYS = { tripo: 'TRIPO_API_KEY', meshy: 'MESHY_API_KEY' };

/** A picture that opens large on click (the same tiles as the checks strips). */
function picture(src, caption, label) {
  const btn = h('button', { class: 'strip', type: 'button', title: 'Open large' },
    h('img', { src, alt: caption, loading: 'lazy' }),
    h('div', { class: 'cap' }, h('b', {}, label)));
  btn.addEventListener('click', () => lightbox(src, caption));
  return btn;
}

const plural = (n, word) => `${formatCount(n)} ${word}${n === 1 ? '' : 's'}`;

export class ModelsSection {
  /** runStep(step, options) posts a step for this game and shows its job (game.js). */
  constructor(code, { runStep }) {
    this.code = code;
    this.runStep = runStep;
    this.data = null;       // GET /models3d: the list, built counts, AI ledger
    this.game = null;       // the game page's data (ROM state)
    this.selected = null;   // model id shown in the detail panel
    this.shown = PAGE;
    this.alive = true;
    // what the user typed in the detail panel survives a refresh (a job ending re-renders it)
    this.form = { mesh: '', strength: '0.6', provider: null, polycount: '6000', budget: '100' };

    // ---------------------------------------------------------- game-level steps
    this.trace = h('input', { type: 'text', placeholder: 'optional: C:\\...\\dl_trace.json', 'aria-label': 'dl_trace file' });
    this.cpuWords = h('input', { type: 'text', placeholder: 'optional: C:\\...\\gx_cpu_words.bin', 'aria-label': 'CPU words file' });
    this.previews = checkbox('Previews of every model (slower; else only the ones a trace saw)');
    this.smooth = h('input', { type: 'number', min: '0', max: '1', step: '0.1', placeholder: 'empty: edited models only', 'aria-label': 'Smooth strength' });
    this.only = h('input', { type: 'text', placeholder: 'e.g. Link', 'aria-label': 'Only models containing' });
    this.seen = checkbox('Only models a trace saw');
    this.steps = {
      extract: stepCard(null, 'Extract models', 'Exports every 3D model in the ROM for editing: OBJ with one group per shape, textures, shape keys and previews.',
        [field('dl_trace file', this.trace, 'A .json from tools/re dl_trace: the shapes a scene uses come first and get previews.'),
          field('CPU words file', this.cpuWords, 'gx_cpu_words.bin from the same trace: short lists the CPU sends count as seen.'),
          this.previews]),
      build: stepCard(null, 'Build models', `Turns every edited.obj into replacement display lists and copies them into the pack (when packs\\${code} exists).`,
        [field('Smooth strength (0-1)', this.smooth, 'PN-smooths original shapes too, no new art needed. 0.6 looks faithful.'),
          field('Only models containing', this.only, 'With a smooth strength: which models to smooth.'),
          this.seen]),
      push: stepCard(null, 'Install models on Thor', 'Replaces only the models folder of the game\'s pack on the Thor; its textures stay. The models load the next time the game starts.'),
    };
    this.steps.push.run.textContent = 'Install';
    this.steps.extract.run.addEventListener('click', () => busy(this.steps.extract.run, () => this.runStep('models_extract', {
      trace: this.trace.value.trim(), cpu_words: this.cpuWords.value.trim(), previews: this.previews.input.checked,
    })));
    this.steps.build.run.addEventListener('click', () => busy(this.steps.build.run, () => this.runStep('models_build', {
      smooth: this.smooth.value.trim(), only: this.only.value.trim(), seen: this.seen.input.checked,
    })));
    this.steps.push.run.addEventListener('click', () => busy(this.steps.push.run, () => this.runStep('models_push', {})));

    // ---------------------------------------------------------- list + detail
    this.filter = h('input', { type: 'text', placeholder: 'Filter by name or source', 'aria-label': 'Filter models' });
    this.seenOnly = checkbox('Seen in a trace');
    this.countEl = h('span', { class: 'faint small' });
    this.list = h('div', { class: 'model-list' });
    this.more = h('button', { class: 'btn ghost sm', type: 'button', hidden: true }, 'Show more');
    this.detailEl = h('div', { class: 'model-detail' });
    this.filter.addEventListener('input', () => { this.shown = PAGE; this.renderList(); });
    this.seenOnly.input.addEventListener('change', () => { this.shown = PAGE; this.renderList(); });
    this.more.addEventListener('click', () => { this.shown += PAGE; this.renderList(); });

    this.summaryEl = h('div', { class: 'row' });
    this.status = h('div', { class: 'stack' });
    this.browser = h('div', { class: 'models-split', hidden: true },
      h('div', { class: 'stack' },
        h('div', { class: 'row' }, h('div', { class: 'grow' }, this.filter), this.seenOnly),
        this.countEl, this.list, this.more),
      this.detailEl);
    this.el = h('div', { class: 'card' },
      h('div', { class: 'card-head' },
        h('div', {}, h('h2', {}, '3D models'),
          h('p', {}, 'Replace the game\'s 3D models (Vulkan renderer): fit a mesh from a 3D tool or an AI model onto a model, or smooth the originals, then build and install. The new meshes wear the game\'s textures (or the pack\'s HD ones) and follow its animation.')),
        this.summaryEl),
      h('div', { class: 'stack' },
        h('div', { class: 'models-steps' }, this.steps.extract.el, this.steps.build.el, this.steps.push.el),
        this.status,
        this.browser));
  }

  /** The game page's data changed (ROM set or moved): only the step states depend on it. */
  setGame(game) {
    this.game = game;
    this.renderSteps();
  }

  async refresh() {
    let d;
    try {
      d = await get(`/api/games/${this.code}/models3d`);
    } catch (error) {
      if (!this.alive) return;
      clear(this.status).append(notice('error', 'Could not load the 3D models', error.message));
      return;
    }
    if (!this.alive) return;
    this.data = d;
    clear(this.status);
    this.renderSummary();
    this.renderSteps();
    if (!d.extracted) {
      this.browser.hidden = true;
      this.status.append(emptyState('No 3D models extracted yet',
        'Extract models exports every model in the ROM (Phantom Hourglass: 1285 models in about 90 s). With a dl_trace from the Thor, the models a scene uses come first.'));
      return;
    }
    if (!d.rom_exists) {
      this.status.append(notice('warn', 'The ROM these models came from is gone',
        `${d.rom || 'models/rom.txt is missing'}. Fit mesh and the AI buttons read it: run Extract models again with the ROM's current location.`));
    }
    this.browser.hidden = false;
    if (!this.selected || !d.models.some((m) => m.id === this.selected)) {
      this.selected = d.models.length ? d.models[0].id : null;
    }
    this.renderList();
    if (this.selected) this.loadDetail(this.selected);
    else clear(this.detailEl);
  }

  destroy() {
    this.alive = false;
  }

  // ---------------------------------------------------------- rendering

  renderSummary() {
    const d = this.data;
    clear(this.summaryEl);
    if (!d || !d.extracted) return;
    const c = d.counts;
    this.summaryEl.append(
      h('span', { class: 'pill' }, plural(c.models, 'model')),
      c.seen ? h('span', { class: 'pill gold' }, `${formatCount(c.seen)} seen`) : null,
      c.edited ? h('span', { class: 'pill green' }, `${formatCount(c.edited)} edited`) : null,
      h('span', { class: 'pill', title: d.built.path }, `${plural(d.built.count, 'replacement')} built`),
      h('span', { class: 'pill', title: `AI ledger: ${d.ai.ledger}` }, `AI ledger: ${formatCount(d.ai.ledger_credits)} credits`));
  }

  renderSteps() {
    const d = this.data;
    const rom = this.game && this.game.rom;
    const romOk = rom && rom.exists && rom.code_matches !== false;
    const setState = (card, done, text) => { card.state.textContent = text; card.state.className = `state ${done ? 'done' : ''}`; card.el.classList.toggle('done', !!done); };
    const extracted = d && d.extracted;
    setState(this.steps.extract, extracted,
      extracted ? `✓ Extracted ${timeAgo(d.extracted)} · ${plural(d.counts.models, 'model')}` : (romOk ? 'Not run yet' : 'Needs the ROM file (see ROM above)'));
    const built = d ? d.built.count : 0;
    const edited = d ? d.counts.edited : 0;
    setState(this.steps.build, built,
      !extracted ? 'Needs Extract models first'
        : built ? `✓ ${plural(built, 'replacement')} in work\\${this.code}\\models_built`
          : edited ? `${plural(edited, 'edited model')} ready to build` : 'Needs an edited model, or a smooth strength');
    const pack = d ? d.pack : null;
    setState(this.steps.push, false,
      pack && pack.count ? `${plural(pack.count, 'replacement')} in packs\\${this.code}\\models, ready to install`
        : built && pack && !pack.pack_exists ? `Build the texture pack first: models go into packs\\${this.code}`
          : 'Needs Build models first');
    // one highlighted button, as in the pipeline above: the next thing to do
    const next = !extracted ? 'extract' : edited && !built ? 'build' : pack && pack.count ? 'push' : null;
    for (const [name, card] of Object.entries(this.steps)) card.run.className = `btn ${name === next ? 'primary' : ''}`;
  }

  fileUrl(id, rel) {
    // the extract time keeps thumbnails fresh after extracting again
    return `/api/games/${this.code}/models3d/${encodeURIComponent(id)}/file/${rel}?v=${Math.round(this.data.extracted || 0)}`;
  }

  filtered() {
    const q = this.filter.value.trim().toLowerCase();
    const seenOnly = this.seenOnly.input.checked;
    return this.data.models.filter((m) => (!seenOnly || m.seen)
      && (!q || `${m.id} ${m.name || ''} ${m.source || ''}`.toLowerCase().includes(q)));
  }

  renderList() {
    if (!this.data) return;
    const all = this.filtered();
    clear(this.list);
    if (!all.length) this.list.append(h('p', { class: 'faint small' }, 'No model matches.'));
    for (const m of all.slice(0, this.shown)) this.list.append(this.row(m));
    this.countEl.textContent = all.length > this.shown
      ? `Showing ${formatCount(this.shown)} of ${formatCount(all.length)} (most seen first)`
      : `${plural(all.length, 'model')} (most seen first)`;
    this.more.hidden = all.length <= this.shown;
  }

  row(m) {
    const thumb = h('span', { class: 'model-thumb' },
      m.has_preview ? h('img', { src: this.fileUrl(m.id, 'preview.png'), alt: '', loading: 'lazy' }) : h('span', { class: 'none' }, 'no preview'));
    const badges = h('span', { class: 'badges' },
      m.seen ? h('span', { class: 'pill gold' }, `seen ${formatCount(m.seen)}`) : null,
      m.has_edited ? h('span', { class: 'pill green' }, 'edited') : null,
      m.ai_refs.length ? h('span', { class: 'pill' }, 'AI refs') : null,
      m.ai_meshes ? h('span', { class: 'pill' }, `${m.ai_meshes} AI mesh${m.ai_meshes === 1 ? '' : 'es'}`) : null);
    const row = h('button', { class: `model-row ${m.id === this.selected ? 'selected' : ''}`, type: 'button', title: m.id, dataset: { id: m.id } },
      thumb,
      h('span', { class: 'model-info' },
        h('b', {}, m.name || m.id),
        h('span', { class: 'faint small' }, m.source || ''),
        h('span', { class: 'faint small' }, `${plural(m.shapes || 0, 'shape')} · ${formatCount(m.triangles)} triangles`),
        badges));
    row.addEventListener('click', () => this.select(m.id));
    return row;
  }

  select(id) {
    this.selected = id;
    for (const row of this.list.querySelectorAll('.model-row')) row.classList.toggle('selected', row.dataset.id === id);
    this.loadDetail(id);
  }

  async loadDetail(id) {
    let d;
    try {
      d = await get(`/api/games/${this.code}/models3d/${encodeURIComponent(id)}`);
    } catch (error) {
      if (this.alive && id === this.selected) clear(this.detailEl).append(notice('error', 'Could not load that model', error.message));
      return;
    }
    if (!this.alive || id !== this.selected) return;
    this.renderDetail(d);
  }

  renderDetail(d) {
    const name = d.name || d.id;
    const img = d.images;
    clear(this.detailEl).append(
      h('div', { class: 'row' }, h('h3', {}, name),
        d.seen ? h('span', { class: 'pill gold' }, `seen ${formatCount(d.seen)}`) : null,
        d.edited ? h('span', { class: 'pill green' }, 'edited') : null),
      kv([
        ['Id', d.id],
        ['Source', d.source],
        ['Shapes', `${d.shapes.length} · ${formatCount(d.triangles)} triangles`],
        ['Folder', d.folder],
        ['Edited mesh', d.edited || 'none yet (edit model.obj in a 3D tool and save it as edited.obj, or fit a mesh)'],
        ['Hidden shapes', d.hidden.length ? d.hidden.join(', ') : ''],
      ]));

    // pictures
    const pics = h('div', { class: 'model-pics' });
    pics.append(img.preview
      ? picture(img.preview, `${name}: original, front / three-quarter / side / back`, 'Original')
      : h('p', { class: 'faint small' }, 'No preview: extract with "Previews of every model", or with a trace that saw this model.'));
    if (img.edited_preview) pics.append(picture(img.edited_preview, `${name}: fitted mesh, front / three-quarter / side / back`, 'Edited (fitted mesh)'));
    if (img.ai_refs.length) {
      pics.append(h('div', { class: 'section-title' }, 'AI reference pictures'),
        h('div', { class: 'ref-grid' }, img.ai_refs.map((r) => picture(r.url, `${name}: AI reference, ${r.view}`, r.view))));
    }
    if (img.textures.length) {
      pics.append(h('div', { class: 'section-title' }, 'Textures'),
        h('div', { class: 'tex-grid' }, img.textures.slice(0, 24).map((t) => picture(t.url, `${name}: texture ${t.name}`, t.name))));
    }
    this.detailEl.append(pics);

    // shapes
    const table = h('table', { class: 'cases-table shapes-table' },
      h('thead', {}, h('tr', {}, ['Shape', 'Material', 'Texture', 'Lit', 'Vertices', 'Triangles', 'Seen', 'Built'].map((t) => h('th', {}, t)))),
      h('tbody', {}, d.shapes.map((s) => h('tr', {},
        h('td', {}, h('div', {}, s.name), h('div', { class: 'mono faint small' }, s.key)),
        h('td', {}, s.material || '-'),
        h('td', {}, s.texture_size && s.texture_size[0] ? `${s.texture_size[0]}x${s.texture_size[1]}` : '-'),
        h('td', {}, s.lit ? 'yes' : 'no'),
        h('td', {}, formatCount(s.vertices)),
        h('td', {}, formatCount(s.triangles)),
        h('td', {}, formatCount(s.seen)),
        h('td', {}, s.built ? h('span', { class: 'pill green' }, 'built') : '-')))));
    this.detailEl.append(h('div', { class: 'section-title' }, 'Shapes'), h('div', { class: 'table-wrap' }, table));
    this.detailEl.append(this.actions(d, name));
  }

  actions(d, name) {
    const form = this.form;
    const remember = (input, key) => input.addEventListener('input', () => { form[key] = input.value; });

    // fit a mesh (from a 3D tool, or an AI result already in ai/)
    const mesh = h('input', { type: 'text', placeholder: 'C:\\models\\new.glb', 'aria-label': 'Mesh file (.glb or .obj)', value: form.mesh });
    remember(mesh, 'mesh');
    const fit = h('button', { class: 'btn primary', type: 'button' }, 'Fit');
    const fitForm = h('div', { class: 'options' },
      field('Mesh file (.glb or .obj)', mesh, 'Any scale, position or facing: it is fitted onto the original, takes its bones and wears its textures.'),
      d.ai_meshes.length ? h('div', { class: 'stack' }, h('span', { class: 'faint small' }, 'AI results for this model:'),
        d.ai_meshes.map((p) => {
          const use = h('button', { class: 'btn sm', type: 'button' }, 'Use');
          use.addEventListener('click', () => { mesh.value = p; form.mesh = p; });
          return h('div', { class: 'row' }, h('span', { class: 'mono small grow' }, p), use);
        })) : null,
      h('div', { class: 'btn-row' }, fit));
    const fitToggle = h('button', { class: 'btn', type: 'button', 'aria-expanded': 'false' }, 'Fit mesh...');
    fitToggle.addEventListener('click', () => {
      const open = !fitForm.classList.contains('open');
      fitForm.classList.toggle('open', open);
      fitToggle.setAttribute('aria-expanded', String(open));
      if (open) mesh.focus();
    });
    fit.addEventListener('click', () => busy(fit, async () => {
      if (d.edited && !await confirmDialog({
        title: 'Replace edited.obj?',
        message: `${name} already has an edited.obj. Fitting writes a new one over it (and hidden.txt and edited_preview.png).`,
        detail: d.edited,
        confirm: 'Fit and replace',
      })) return;
      await this.runStep('models_fit', { model: d.id, mesh: mesh.value });
    }));

    // smooth: models build --smooth S --only <id>
    const strength = h('input', { type: 'number', min: '0', max: '1', step: '0.1', value: form.strength, 'aria-label': 'Smooth strength', style: { width: '90px' } });
    remember(strength, 'strength');
    const smooth = h('button', { class: 'btn', type: 'button' }, 'Smooth this model');
    smooth.addEventListener('click', () => busy(smooth, () => this.runStep('models_build', { smooth: strength.value, only: d.id })));

    // AI: a dry run is free; generate spends credits and asks first
    const keys = d.ai.keys;
    const provider = select(Object.entries(PROVIDERS).map(([v, label]) => [v, `${label}${keys[v] ? '' : ' (no key)'}`]),
      form.provider || (keys.tripo || !keys.meshy ? 'tripo' : 'meshy'));
    provider.addEventListener('change', () => { form.provider = provider.value; sync(); });
    const polycount = h('input', { type: 'number', min: '100', max: '100000', step: '500', value: form.polycount, 'aria-label': 'Polygon count' });
    remember(polycount, 'polycount');
    const budget = h('input', { type: 'number', min: '0', step: '20', value: form.budget, 'aria-label': 'Budget (credits)' });
    remember(budget, 'budget');
    const dry = h('button', { class: 'btn', type: 'button' }, 'AI dry run');
    const gen = h('button', { class: 'btn danger', type: 'button' }, 'AI generate...');
    const aiNote = h('p', { class: 'faint small' });
    const sync = () => {
      const p = provider.value;
      gen.disabled = !keys[p];
      gen.title = keys[p] ? '' : `No ${KEYS[p]} is set`;
      aiNote.textContent = keys[p]
        ? `${PROVIDERS[p]}: ${d.ai.credits_per_call} credits per call. ${this.code}'s ledger: ${formatCount(d.ai.ledger_credits)} credits in ${plural(d.ai.ledger_calls, 'call')}.`
        : `No ${KEYS[p]} in tools\\hd_remaster\\.env (or the environment): add it to generate. A dry run needs no key.`;
    };
    sync();
    const aiOptions = () => ({ model: d.id, provider: provider.value, polycount: polycount.value, budget: budget.value });
    dry.addEventListener('click', () => busy(dry, () => this.runStep('models_ai', { ...aiOptions(), dry_run: true })));
    gen.addEventListener('click', () => busy(gen, async () => {
      // the ledger as it is now (another job may have spent since the page loaded)
      const fresh = await get(`/api/games/${this.code}/models3d/${encodeURIComponent(d.id)}`);
      const ai = fresh.ai;
      const p = provider.value;
      const per = ai.credits_per_call;
      const ok = await confirmDialog({
        title: 'Spend credits on an AI model?',
        message: `AI generate sends four pictures of ${name} to ${PROVIDERS[p]} and spends ${per} credits per call`
          + `${p === 'tripo' ? ' (about $0.20; $1 = 100 credits)' : ''}. ${this.code}'s ledger has ${formatCount(ai.ledger_credits)} credits `
          + `so far (${plural(ai.ledger_calls, 'call')}); the call is refused if it would pass the budget of ${budget.value || 100} credits. `
          + 'The result is fitted onto the model as edited.obj.',
        detail: `${PROVIDERS[p]} · ${per} credits per call · ledger ${formatCount(ai.ledger_credits)} credits`,
        confirm: `Spend ${per} credits`,
        danger: true,
      });
      if (!ok) return;
      await this.runStep('models_ai', { ...aiOptions(), dry_run: false, confirm_spend: true });
    }));

    return h('div', { class: 'model-actions' },
      h('div', { class: 'stack' },
        h('div', { class: 'section-title' }, 'Replace with a mesh'),
        h('p', { class: 'faint small' }, 'Writes edited.obj (groups = shapes), hidden.txt and a preview; then Build models.'),
        h('div', { class: 'btn-row' }, fitToggle), fitForm),
      h('div', { class: 'stack' },
        h('div', { class: 'section-title' }, 'Smooth'),
        h('p', { class: 'faint small' }, 'PN-smoothed replacements, no new art (Build models with --only this model; edited models are rebuilt too, and keep their edit).'),
        h('div', { class: 'row', style: { alignItems: 'flex-end' } }, field('Strength (0-1)', strength), smooth)),
      h('div', { class: 'stack' },
        h('div', { class: 'section-title' }, 'AI model'),
        h('p', { class: 'faint small' }, 'The dry run renders the four reference pictures (free). Generate sends them to the provider for new geometry and fits it onto this model.'),
        h('div', { class: 'form-grid' }, field('Provider', provider), field('Polygon count', polycount), field('Budget (credits, whole ledger)', budget)),
        aiNote,
        h('div', { class: 'btn-row' }, dry, gen)));
  }
}
