// Entry point: hash router (#/library, #/game/BSDE, #/checks, #/jobs, #/settings), the device
// panel and the running-jobs badge.
import { get } from './api.js';
import { DevicePanel } from './device.js';
import { clear } from './ui.js';
import * as checks from './views/checks.js';
import * as game from './views/game.js';
import * as jobs from './views/jobs.js';
import * as library from './views/library.js';
import * as settings from './views/settings.js';

const VIEWS = { library, game, checks, jobs, settings };
const TITLES = { library: 'Library', game: 'Game', checks: 'Checks', jobs: 'Jobs', settings: 'Settings' };
const view = document.getElementById('view');
let dispose = null;

function route() {
  const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
  const name = VIEWS[parts[0]] ? parts[0] : 'library';
  if (dispose) { try { dispose(); } catch { /* the old view is gone either way */ } }
  clear(view);
  view.scrollTop = 0;
  document.title = `${TITLES[name]} - Remaster Studio`;
  for (const a of document.querySelectorAll('[data-nav]')) {
    const active = a.dataset.nav === name || (name === 'game' && a.dataset.nav === 'library');
    a.classList.toggle('active', active);
    if (active) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  }
  dispose = VIEWS[name].render(view, parts.slice(1)) || null;
}

async function badgeLoop() {
  const badge = document.getElementById('jobs-badge');
  for (;;) {
    try {
      const res = await get('/api/jobs');
      const active = res.jobs.filter((j) => ['running', 'queued'].includes(j.status)).length;
      badge.hidden = active === 0;
      badge.textContent = String(active);
      badge.title = `${active} job${active === 1 ? '' : 's'} running or queued`;
    } catch { badge.hidden = true; }
    await new Promise((resolve) => setTimeout(resolve, 4000));
  }
}

window.addEventListener('hashchange', route);
new DevicePanel();
route();
badgeLoop();
