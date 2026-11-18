/* Client Platforms sidebar: the same navigation on every page of the site.
 *
 * Served by the portal at /static/nav.js; each page (portal and every tool) loads it with
 *   <script src="/static/nav.js" defer></script>
 * It asks the portal who is signed in (/api/me) and lists just the tools that person can use,
 * with the links set under Settings, and marks the page you're on. Below 900px wide it becomes
 * a strip of links above the page.
 */
(() => {
  if (window.__cpNav) return;
  window.__cpNav = true;

  const ICON = {
    home: '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/><path d="M10 21v-6h4v6"/>',
    bell: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
    shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/>',
    team: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8"/>',
    tasks: '<path d="m3 7 2 2 4-4M3 17l2 2 4-4M13 7h8M13 17h8"/>',
    projects: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M8 13h3M8 16h6"/>',
    restart: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l3 2"/>',
    settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
    chevron: '<path d="m6 9 6 6 6-6"/>',
    out: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>',
  };
  const svg = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[name] || ICON.home}</svg>`;
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const css = `
  .cp-side { position: fixed; top: 0; left: 0; bottom: 0; width: 232px; display: flex; flex-direction: column;
    background: #FDFFF8; border-right: 1px solid #E1EAF9; font: 14px/1.5 "Segoe UI", system-ui, -apple-system, sans-serif; color: #040E27; }
  .cp-side .cp-brand { display: flex; align-items: center; gap: 10px; height: 64px; padding: 0 20px; border-bottom: 1px solid #E1EAF9;
    color: inherit; text-decoration: none; font-weight: 600; flex-shrink: 0; }
  .cp-side .cp-brand img { width: 22px; height: 22px; }
  .cp-side .cp-list { padding: 14px 12px; display: flex; flex-direction: column; gap: 2px; overflow-y: auto; flex: 1; }
  .cp-side .cp-label { font-size: 11px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: #3B404E; padding: 14px 10px 6px; }
  .cp-side button.cp-label { display: flex; align-items: center; gap: 6px; width: 100%; border: 0; background: none; cursor: pointer;
    font-family: inherit; text-align: left; border-radius: 6px; margin-top: 8px; padding: 6px 10px; }
  .cp-side button.cp-label:hover { background: #F3F5FB; color: #040E27; }
  .cp-side button.cp-label:focus-visible { outline: none; box-shadow: 0 0 0 3px rgba(15,65,243,.3); }
  .cp-side button.cp-label svg { width: 14px; height: 14px; margin-left: auto; transition: transform .15s; }
  .cp-side button.cp-label[aria-expanded="false"] svg { transform: rotate(-90deg); }
  .cp-side .cp-items { display: flex; flex-direction: column; gap: 2px; }
  .cp-side .cp-items[hidden] { display: none; }
  .cp-side .cp-badge { margin-left: auto; min-width: 20px; height: 20px; padding: 0 6px; border-radius: 10px; background: #0F41F3; color: #fff;
    font-size: 11.5px; font-weight: 700; display: inline-grid; place-items: center; letter-spacing: 0; }
  .cp-side .cp-badge.late { background: #D92D20; }
  .cp-side button.cp-label > span:first-child { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
  .cp-side .cp-dot { width: 8px; height: 8px; border-radius: 50%; background: #0F41F3; flex-shrink: 0; }
  .cp-side .cp-dot.late { background: #D92D20; }
  .cp-side button.cp-label[aria-expanded="true"] .cp-dot { display: none; }
  .cp-side a.cp-item { display: flex; align-items: center; gap: 10px; padding: 9px 10px; border-radius: 8px; color: #3B404E; text-decoration: none; }
  .cp-side a.cp-item svg { width: 18px; height: 18px; flex-shrink: 0; }
  .cp-side a.cp-item:hover { background: #F3F5FB; color: #040E27; }
  .cp-side a.cp-item:focus-visible { outline: none; box-shadow: 0 0 0 3px rgba(15,65,243,.3); }
  .cp-side a.cp-item[aria-current="page"] { background: #E1EAF9; color: #0F41F3; font-weight: 600; }
  .cp-side .cp-foot { border-top: 1px solid #E1EAF9; padding: 12px; flex-shrink: 0; }
  .cp-side .cp-who { font-size: 12.5px; color: #3B404E; padding: 4px 10px 8px; overflow-wrap: anywhere; }
  body.cp-has-side { padding-left: 232px; }
  @media (max-width: 900px) {
    body.cp-has-side { padding-left: 0; }
    .cp-side { position: static; width: auto; flex-direction: row; align-items: center; height: auto; border-right: 0; border-bottom: 1px solid #E1EAF9; }
    .cp-side .cp-brand { height: 48px; padding: 0 12px; border-bottom: 0; }
    .cp-side .cp-brand span, .cp-side .cp-label, .cp-side .cp-who { display: none !important; }
    .cp-side .cp-items, .cp-side .cp-items[hidden] { display: contents; }
    .cp-side .cp-list { flex-direction: row; padding: 6px; overflow-x: auto; overflow-y: hidden; }
    .cp-side .cp-foot { border-top: 0; padding: 6px; display: flex; }
    .cp-side a.cp-item { white-space: nowrap; padding: 8px 10px; }
  }
  @media print { .cp-side { display: none; } body.cp-has-side { padding-left: 0; } }`;

  function current(href) {
    const here = location.pathname;
    try {
      const u = new URL(href, location.origin);
      if (u.origin !== location.origin) return false;
      return u.pathname === '/' ? here === '/' : here.startsWith(u.pathname);
    } catch { return false; }
  }

  function item(href, icon, label, external, tool) {
    const cur = current(href) ? ' aria-current="page"' : '';
    const ext = external ? ' target="_blank" rel="noopener"' : '';
    const id = tool ? ` data-tool="${esc(tool)}"` : '';
    return `<a class="cp-item" href="${esc(href)}"${cur}${ext}${id}>${svg(icon)}<span>${esc(label)}</span></a>`;
  }

  async function build() {
    let me;
    try {
      const r = await fetch('/api/me', { headers: { Accept: 'application/json' }, cache: 'no-store', credentials: 'same-origin' });
      if (!r.ok) return;   // not signed in at the portal (or a page outside the site): no sidebar
      me = await r.json();
    } catch { return; }

    const style = document.createElement('style');
    style.textContent = css;
    document.head.append(style);

    // Tools under their headings (Device Management, Project Management …), in the portal's order.
    const groups = [];
    for (const t of me.tools || []) {
      const name = t.group || 'Tools';
      let g = groups.find(x => x.name === name);
      if (!g) groups.push(g = { name, items: [] });
      const external = /^https?:\/\//i.test(t.url) && new URL(t.url).origin !== location.origin;
      g.items.push(item(t.url, t.icon, t.name, external, t.id));
    }
    // Each heading folds; closed unless it holds the page you're on or you opened it before.
    const opened = (() => { try { return JSON.parse(localStorage.getItem('cp-nav-open') || '{}'); } catch { return {}; } })();
    const tools = groups.map((g, i) => {
      const here = g.items.some(h => h.includes('aria-current'));
      const open = here || opened[g.name] === true;
      return `<button type="button" class="cp-label" aria-expanded="${open}" aria-controls="cp-g${i}" data-group="${esc(g.name)}">`
        + `<span>${esc(g.name)}</span>${svg('chevron')}</button>`
        + `<div class="cp-items" id="cp-g${i}" ${open ? '' : 'hidden'}>${g.items.join('')}</div>`;
    }).join('');
    const settings = me.admin ? item('/settings', 'settings', 'Settings')
      : me.passwords ? item('/settings', 'settings', 'Your account') : '';

    const side = document.createElement('aside');
    side.className = 'cp-side';
    side.setAttribute('aria-label', 'Client Platforms');
    side.innerHTML = `
      <a class="cp-brand" href="/"><img src="/static/nscale-icon.svg" alt=""><span>Client Platforms</span></a>
      <div class="cp-list" role="navigation" aria-label="Tools">
        ${item('/', 'home', 'Home')}
        ${tools}
      </div>
      <div class="cp-foot">
        <div class="cp-who">${esc(me.email)}</div>
        ${settings}
        ${item('/logout', 'out', 'Sign out')}
      </div>`;
    side.querySelectorAll('button.cp-label').forEach(b => b.addEventListener('click', () => {
      const open = b.getAttribute('aria-expanded') !== 'true';
      b.setAttribute('aria-expanded', String(open));
      document.getElementById(b.getAttribute('aria-controls')).hidden = !open;
      try { localStorage.setItem('cp-nav-open', JSON.stringify({ ...opened, [b.dataset.group]: open })); opened[b.dataset.group] = open; } catch {}
    }));
    document.body.prepend(side);
    if ((me.tools || []).some(t => t.id === 'tasks')) {
      taskBadge(side);
      window.cpNavRefreshTasks = () => taskBadge(side);   // the Tasks page calls this after changes
    }   // first in the page, so each tool's own panels and overlays sit above it
    document.body.classList.add('cp-has-side');
  }

  /** How many tasks are assigned to you and questions are waiting on you, beside Tasks (and its heading). */
  async function taskBadge(side) {
    try {
      const r = await fetch('/tasks/api/counts', { headers: { Accept: 'application/json' }, cache: 'no-store', credentials: 'same-origin' });
      if (!r.ok) return;
      const c = await r.json();
      side.querySelectorAll('.cp-badge, .cp-dot').forEach(b => b.remove());
      if (!c.badge) return;
      const title = `${c.mine} assigned to you${c.overdue ? `, ${c.overdue} overdue` : ''}${c.waiting ? `, ${c.waiting} question${c.waiting > 1 ? 's' : ''} waiting on you` : ''}`;
      const badge = () => { const b = document.createElement('span'); b.className = 'cp-badge' + (c.overdue || c.waiting ? ' late' : '');
        b.textContent = c.badge > 99 ? '99+' : String(c.badge); b.title = title; return b; };
      const link = side.querySelector('a[data-tool="tasks"]');
      if (!link) return;
      link.append(badge());
      link.setAttribute('aria-label', `Tasks: ${title}`);
      // A folded heading gets a dot, so the count isn't hidden away.
      const heading = side.querySelector(`button.cp-label[aria-controls="${link.parentElement.id}"]`);
      if (heading) {
        const dot = document.createElement('span');
        dot.className = 'cp-dot' + (c.overdue || c.waiting ? ' late' : '');
        dot.title = title;
        heading.querySelector('span').after(dot);
      }
    } catch { /* no badge */ }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', build);
  else build();
})();
