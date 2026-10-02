// A job's live output: streamed over Server-Sent Events, with status, elapsed time and Cancel.
import { post } from './api.js';
import { busy, formatDuration, h, statusPill, toast } from './ui.js';

const MAX_NODES = 6000;   // lines kept on screen; the server keeps more

function lineClass(text) {
  if (text.startsWith('studio:')) return 'l-studio';
  if (/\b(FAILED|Traceback|DIFFERENT|WORSE|[Ee]rror)\b|(?<!\b0 )\bfailed\b/.test(text)) return 'l-err';
  if (/\b(warning|note|FLAGGED)\b|isn't one it was verified/i.test(text)) return 'l-warn';
  if (/\b(done in|installed|pack ready|no flagged frames|same as the baseline)\b|^ready:/i.test(text)) return 'l-ok';
  return '';
}

export class LogView {
  /** onEnd(job) runs when the job finishes while shown. */
  constructor({ emptyText = 'Nothing has run yet.', onEnd = null, height } = {}) {
    this.emptyText = emptyText;
    this.onEnd = onEnd;
    this.job = null;
    this.source = null;
    this.next = 0;
    this.titleEl = h('b', {}, 'Output');
    this.cmdEl = h('span', { class: 'cmd' });
    this.timeEl = h('span', { class: 'faint small' });
    this.pillEl = h('span');
    this.cancelBtn = h('button', { class: 'btn danger sm', type: 'button', hidden: true, onclick: () => this.cancel() }, 'Cancel');
    this.body = h('pre', { class: 'log-body', tabindex: '0', 'aria-label': 'Job output', style: height ? { height } : null });
    this.el = h('div', { class: 'logview' },
      h('div', { class: 'log-head' }, h('div', { class: 'grow' }, this.titleEl, this.cmdEl), this.timeEl, this.pillEl, this.cancelBtn),
      this.body);
    this.showEmpty();
    this.timer = setInterval(() => this.tick(), 1000);
  }

  showEmpty(text) {
    this.body.replaceChildren(h('span', { class: 'log-empty' }, text || this.emptyText));
  }

  /** Show a job (its summary object) and stream its output from the start. */
  show(job) {
    if (this.job && job && this.job.id === job.id && (this.source || this.next > 0)) {
      this.renderHead(job);
      return;
    }
    this.detach();
    this.job = job;
    this.next = 0;
    this.body.replaceChildren();
    this.renderHead(job);
    if (!job) { this.showEmpty(); return; }
    const source = new EventSource(`/api/jobs/${job.id}/events`);
    this.source = source;
    source.addEventListener('lines', (e) => this.addLines(JSON.parse(e.data)));
    source.addEventListener('status', (e) => this.renderHead(JSON.parse(e.data)));
    source.addEventListener('end', (e) => {
      const summary = JSON.parse(e.data);
      this.renderHead(summary);
      this.detach();
      if (!this.body.childNodes.length) this.showEmpty('This job printed nothing.');
      if (this.onEnd) this.onEnd(summary);
    });
    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED && this.source === source) {
        this.detach();
        this.addLines({ from: this.next, lines: ['studio: lost the connection to this job (was the studio restarted?)'] });
      }
    };
  }

  addLines({ from, lines }) {
    const stick = this.body.scrollHeight - this.body.scrollTop - this.body.clientHeight < 40;
    const frag = document.createDocumentFragment();
    if (from > this.next) frag.append(h('span', { class: 'l-studio' }, `... ${from - this.next} earlier lines not kept\n`));
    for (const text of lines) {
      const cls = lineClass(text);
      frag.append(cls ? h('span', { class: cls }, `${text}\n`) : `${text}\n`);
    }
    this.next = from + lines.length;
    if (this.body.firstChild && this.body.firstChild.classList && this.body.firstChild.classList.contains('log-empty')) {
      this.body.replaceChildren();
    }
    this.body.append(frag);
    let extra = this.body.childNodes.length - MAX_NODES;
    while (extra-- > 0) this.body.firstChild.remove();
    if (stick) this.body.scrollTop = this.body.scrollHeight;
  }

  renderHead(job) {
    if (job && this.job && job.id === this.job.id) this.job = job;
    this.titleEl.textContent = job ? job.title : 'Output';
    this.cmdEl.textContent = job ? job.command : '';
    this.cmdEl.title = job ? job.command : '';
    this.pillEl.replaceChildren(job ? statusPill(job.status) : '');
    this.cancelBtn.hidden = !job || !['queued', 'running'].includes(job.status);
    this.tick();
  }

  tick() {
    const job = this.job;
    if (!job || !job.started) { this.timeEl.textContent = ''; return; }
    const end = job.ended || Date.now() / 1000;
    this.timeEl.textContent = formatDuration(end - job.started);
  }

  async cancel() {
    if (!this.job) return;
    await busy(this.cancelBtn, async () => {
      const res = await post(`/api/jobs/${this.job.id}/cancel`);
      toast(res.job.status === 'cancelled' ? 'Cancelled.' : 'Stopping... (a check restores the Thor\'s settings first)', 'warn');
    });
  }

  detach() {
    if (this.source) { this.source.close(); this.source = null; }
  }

  destroy() {
    this.detach();
    clearInterval(this.timer);
  }
}
