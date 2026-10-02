// Jobs: every job of this session (pipeline steps, setup, checks) with its output.
import { get } from '../api.js';
import { LogView } from '../logview.js';
import { clear, emptyState, formatDuration, formatTime, h, notice, statusPill } from '../ui.js';

export function render(root, params) {
  const inner = h('div', { class: 'view-inner' });
  root.append(inner);
  let alive = true;
  let timer = null;
  let selected = params[0] || null;
  const list = h('div', { class: 'job-list' });
  const log = new LogView({ emptyText: 'Pick a job to see its output.', onEnd: () => load() });

  inner.append(
    h('div', { class: 'page-head' }, h('div', {}, h('h1', {}, 'Jobs'),
      h('p', { class: 'lede' }, 'Everything the studio ran in this session. Pipeline steps run one at a time, and so do jobs that use the Thor; the rest wait in a queue.'))),
    h('div', { class: 'card' }, list),
    log.el);

  async function load() {
    clearTimeout(timer);
    let res;
    try {
      res = await get('/api/jobs');
    } catch (error) {
      if (alive) clear(list).append(notice('error', 'Could not list the jobs', error.message));
      return;
    }
    if (!alive) return;
    clear(list);
    if (!res.jobs.length) {
      list.append(emptyState('Nothing has run yet', 'Start a step on a game page, run Setup from the Library or run Checks.'));
    }
    if (!selected && res.jobs.length) selected = res.jobs[0].id;
    for (const j of res.jobs) {
      const row = h('button', { class: `job-row ${j.id === selected ? 'selected' : ''}`, type: 'button' },
        statusPill(j.status),
        h('div', {}, h('b', {}, j.title),
          h('div', { class: 'faint small' }, [j.game ? `game ${j.game}` : null, j.lane === 'device' ? 'uses the Thor' : null,
            j.duration ? formatDuration(j.duration) : null].filter(Boolean).join(' · '))),
        h('span', { class: 'when' }, formatTime(j.created)));
      row.addEventListener('click', () => { selected = j.id; history.replaceState(null, '', `#/jobs/${j.id}`); load(); });
      list.append(row);
    }
    const job = res.jobs.find((j) => j.id === selected);
    if (job) log.show(job);
    const running = res.jobs.some((j) => ['queued', 'running'].includes(j.status));
    timer = setTimeout(load, running ? 3000 : 10000);
  }

  load();
  return () => {
    alive = false;
    clearTimeout(timer);
    log.destroy();
  };
}
