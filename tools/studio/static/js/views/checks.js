// Checks: run tools/frame_compare on the Thor and browse the flagged strips of every run.
import { get, guardedPost } from '../api.js';
import { LogView } from '../logview.js';
import { busy, checkbox, clear, emptyState, field, formatTime, h, lightbox, notice, select, toast } from '../ui.js';

const pct = (x) => (x === null || x === undefined ? '' : `${(x * 100).toFixed(x < 0.1 ? 2 : 1)}%`);

export function render(root, params) {
  const inner = h('div', { class: 'view-inner' });
  root.append(inner);
  let alive = true;
  let selectedRun = params[0] ? decodeURIComponent(params[0]) : null;

  const log = new LogView({
    emptyText: 'No check has run in this session yet.',
    onEnd: (job) => {
      toast(`${job.title}: ${job.status}.`, job.status === 'done' ? 'ok' : 'warn');
      selectedRun = null;
      loadRuns(true);
    },
  });

  // ------------------------------------------------------------ run form
  const casesSelect = h('select', { 'aria-label': 'Cases file' });
  const customCases = h('input', { type: 'text', placeholder: 'C:\\path\\to\\cases.txt', 'aria-label': 'Custom cases file', hidden: true });
  const casesPreview = h('div', { class: 'table-wrap' });
  const baseline = h('select', { 'aria-label': 'Baseline run' }, h('option', { value: '' }, 'None (software only)'));
  const ir = select([['1', '1x (cleanest comparison)'], ['2', '2x'], ['3', '3x'], ['4', '4x']], '1');
  const renderers = select([['vulkan,software', 'Vulkan vs software'], ['vulkan,vulkan', 'Vulkan vs Vulkan (determinism check)']], 'vulkan,software');
  const packs = checkbox('Keep texture packs on (they change pixels on purpose)');
  const run = h('button', { class: 'btn primary', type: 'button' }, 'Run checks on the Thor');
  let caseFiles = [];

  function renderPreview() {
    customCases.hidden = casesSelect.value !== '__custom';
    const file = caseFiles.find((f) => f.path === casesSelect.value);
    clear(casesPreview);
    if (!file) return;
    if (!file.cases.length) { casesPreview.append(h('p', { class: 'faint small' }, 'This file has no cases.')); return; }
    casesPreview.append(h('table', { class: 'cases-table' },
      h('thead', {}, h('tr', {}, h('th', {}, 'ROM on the Thor'), h('th', {}, 'State'), h('th', {}, 'Frames'))),
      h('tbody', {}, file.cases.map((c) => h('tr', {}, h('td', {}, c.rom), h('td', {}, c.state), h('td', {}, c.frames || '120'))))));
  }
  casesSelect.addEventListener('change', renderPreview);

  run.addEventListener('click', () => busy(run, async () => {
    const cases = casesSelect.value === '__custom' ? customCases.value.trim() : casesSelect.value;
    if (!cases) { toast('Pick a cases file first.', 'warn'); return; }
    const res = await guardedPost('/api/checks/run', {
      options: { cases, baseline: baseline.value, ir: ir.value, renderers: renderers.value, packs: packs.input.checked },
    });
    if (!res) return;
    log.show(res.job);
    toast(`Checks ${res.job.status === 'queued' ? 'queued' : 'started'}. Leave the Thor alone until they finish.`, 'ok');
  }));

  const formCard = h('div', { class: 'card' },
    h('div', { class: 'card-head' }, h('div', {}, h('h2', {}, 'Run checks'),
      h('p', {}, 'Each case launches the game on the Thor twice (Vulkan, then the software renderer as the reference), loads the save state paused and dumps the same frames. About a minute per case.'))),
    h('div', { class: 'form-grid' },
      field('Cases file', casesSelect, 'One "rom | state | frames" per line'),
      field('Baseline run', baseline, 'A known-good earlier run: catches small regressions'),
      field('Internal resolution', ir),
      field('Renderers', renderers)),
    customCases,
    h('div', { style: { marginTop: '12px' } }, casesPreview),
    h('div', { class: 'row', style: { marginTop: '14px' } }, packs, h('span', { class: 'grow' }), run),
    h('p', { class: 'faint small', style: { marginTop: '10px' } },
      'The run switches the renderer, internal resolution, texture packs and renderer debug tools, and puts them back afterwards (also when you cancel). It refuses to start while the emulator is already in front.'));

  // ------------------------------------------------------------ results
  const runsList = h('div', { class: 'runs' });
  const detail = h('div', { class: 'stack' });
  const resultsCard = h('div', { class: 'card' },
    h('div', { class: 'card-head' }, h('div', {}, h('h2', {}, 'Results'), h('p', {}, 'Runs from the studio\'s output folder and your baseline folders. Pick one to see its flagged frames.'))),
    h('div', { class: 'split' }, h('div', {}, h('div', { class: 'section-title' }, 'Runs'), runsList), h('div', {}, detail)));
  const caseDetail = h('div', { class: 'stack' });

  inner.append(
    h('div', { class: 'page-head' }, h('div', {}, h('h1', {}, 'Checks'),
      h('p', { class: 'lede' }, 'Frame-exact comparison of the Vulkan renderer against the software renderer, on the Thor, from save states. Flagged frames come back as image strips.'))),
    formCard, log.el, resultsCard, caseDetail);

  async function loadCases() {
    try {
      const res = await get('/api/checks/cases');
      caseFiles = res.files;
      clear(casesSelect).append(...caseFiles.map((f) => h('option', { value: f.path }, `${f.name} (${f.cases.length} cases)`)),
        h('option', { value: '__custom' }, 'Another file...'));
      renderPreview();
    } catch (error) {
      clear(casesPreview).append(notice('error', 'Could not read the cases files', error.message));
    }
  }

  async function loadRuns(pickNewest = false) {
    let res;
    try {
      res = await get('/api/checks/runs');
    } catch (error) {
      clear(runsList).append(notice('error', 'Could not list the runs', error.message));
      return;
    }
    if (!alive) return;
    const keep = baseline.value;
    clear(baseline).append(h('option', { value: '' }, 'None (software only)'),
      ...res.runs.map((r) => h('option', { value: r.id }, `${r.name} (${r.cases} cases${r.is_output ? '' : ', baseline folder'})`)));
    baseline.value = keep;
    clear(runsList);
    if (!res.runs.length) {
      runsList.append(emptyState('No runs yet', `Results are written to ${res.out_dir}. Add folders with older runs as baseline folders in Settings.`));
      clear(detail);
      return;
    }
    if (pickNewest || !selectedRun || !res.runs.some((r) => r.id === selectedRun)) selectedRun = res.runs[0].id;
    for (const r of res.runs) {
      const row = h('button', { class: `job-row ${r.id === selectedRun ? 'selected' : ''}`, type: 'button' },
        h('span', { class: `pill ${r.strips ? 'gold' : 'green'}` }, r.strips ? `${r.strips} flagged` : 'clean'),
        h('div', {}, h('b', {}, r.name), h('div', { class: 'faint small' }, `${r.cases} cases · ${r.is_output ? 'studio output' : r.root}`)),
        h('span', { class: 'when' }, formatTime(r.modified)));
      row.addEventListener('click', () => { selectedRun = r.id; loadRuns(); });
      runsList.append(row);
    }
    loadDetail(selectedRun);
  }

  async function loadDetail(id) {
    let d;
    try {
      d = await get(`/api/checks/run?id=${encodeURIComponent(id)}`);
    } catch (error) {
      clear(detail).append(notice('error', 'Could not read that run', error.message));
      return;
    }
    if (!alive || id !== selectedRun) return;
    clear(detail).append(h('div', { class: 'section-title' }, 'Cases'));
    clear(caseDetail);
    if (!d.cases.length) detail.append(h('p', { class: 'faint small' }, 'This run has no finished cases (cancelled early?).'));
    const table = h('table', { class: 'cases-table' }, h('thead', {}, h('tr', {}, h('th', {}, 'Case'), h('th', {}, 'Flagged'), h('th', {}, 'Baseline'))));
    const tbody = h('tbody');
    table.append(tbody);
    for (const c of d.cases) {
      const baselineText = c.baseline_changes === null ? '-' : c.baseline_changes === 0 ? 'same' : `${c.baseline_changes} changed${c.worse ? `, ${c.worse} worse` : ''}`;
      tbody.append(h('tr', {}, h('td', {}, c.name), h('td', {}, c.has_report ? String(c.flagged) : 'no report'), h('td', {}, baselineText)));
    }
    if (d.cases.length) detail.append(h('div', { class: 'table-wrap' }, table));

    // the strips, one block per case that has any
    const withStrips = d.cases.filter((c) => c.strips.length);
    if (!withStrips.length && d.cases.length) {
      caseDetail.append(h('div', { class: 'card' }, emptyState('No flagged frames in this run', 'Every case matched the software renderer within the usual differences.')));
      return;
    }
    const card = h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('div', {}, h('h2', {}, `Flagged frames: ${d.name}`),
        h('p', {}, 'Strips: Vulkan | software frame i | software frame i+1 | differing pixels (magenta). Baseline strips: new | baseline | software | yellow changed, magenta newly wrong, green newly right.'))));
    for (const c of withStrips) {
      const block = h('div', { class: 'case-block' },
        h('div', { class: 'row' }, h('h3', {}, c.rom || c.name), h('span', { class: 'chip' }, c.state || ''),
          h('span', { class: 'faint small' }, `usual difference: top ${pct(c.usual_share.top)}, bottom ${pct(c.usual_share.bottom)}`)));
      const grid = h('div', { class: 'strips' });
      for (const s of c.strips) {
        const info = s.info || {};
        const src = `/api/checks/file?id=${encodeURIComponent(s.file)}`;
        const facts = s.baseline
          ? `${info.verdict || 'changed'} · ${info.changed ?? '?'} px changed, ${info.newlyWrong ?? '?'} newly wrong`
          : `${pct(info.share)} differ${info.flicker ? ` · ${info.flicker} px flicker` : ''}`;
        const caption = `${c.name}: frame ${s.frame}, ${s.screen} screen${s.baseline ? ' (vs baseline)' : ''}. ${facts}`;
        const btn = h('button', { class: 'strip', type: 'button', title: 'Open large' },
          h('img', { src, alt: caption, loading: 'lazy' }),
          h('div', { class: 'cap' }, h('b', {}, `Frame ${s.frame}`), h('span', { class: 'chip' }, s.screen),
            s.baseline ? h('span', { class: `pill ${info.verdict === 'WORSE' ? 'red' : info.verdict === 'better' ? 'green' : ''}` }, info.verdict || 'changed') : null,
            h('span', { class: 'faint' }, facts)));
        btn.addEventListener('click', () => lightbox(src, caption));
        grid.append(btn);
      }
      block.append(grid);
      card.append(block);
    }
    caseDetail.append(card);
  }

  async function loadLastJob() {
    try {
      const res = await get('/api/jobs?kind=checks');
      if (alive && res.jobs.length) log.show(res.jobs[0]);
    } catch { /* the log stays empty */ }
  }

  loadCases();
  loadRuns();
  loadLastJob();
  return () => {
    alive = false;
    log.destroy();
  };
}
