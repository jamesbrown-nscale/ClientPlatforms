/* Column filters on the Users pages (All Users, Users - FTE, Users - CT): the funnel next to the Type and
 * Platform headings opens the column's values to tick. Nothing ticked = everything.
 *
 *   const CF = colFilters({ rows: () => ALL, matches: (u, skipCol) => ..., onChange: draw });
 *   CF.passes(u, skipCol)   in the page's matches(): false if a ticked column filter rules u out
 *   CF.drawButtons()        in the page's draw(): shows which funnels are on
 *   CF.active()             true while anything is ticked
 *
 * A heading gets a funnel with <button class="flt" data-col="type|platform"> after its sort button.
 * The counts in a list are within every other filter, so they add up to what the table would show. */
(function () {
  const FUNNEL = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 5h18l-7 8v6l-4 2v-8z"/></svg>';
  const NAMES = { type: 'Type', platform: 'Platform' };
  const ORDER = { type: ['VIP', 'Standard'], platform: ['Mac', 'Windows', 'Linux', ''] };
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  window.colFilters = function ({ rows, matches, onChange }) {
    const COLF = { type: new Set(), platform: new Set() };
    const val = (u, col) => col === 'type' ? (u.type || '') : (u.platform || '');
    const label = (col, v) => col === 'platform' && !v ? 'None' : v;
    let POP = null;   // {col, el, btn} while a list is open

    function passes(u, skipCol) {
      for (const col in COLF) if (col !== skipCol && COLF[col].size && !COLF[col].has(val(u, col))) return false;
      return true;
    }
    function drawButtons() {
      document.querySelectorAll('th button.flt').forEach(b => {
        const col = b.dataset.col, n = COLF[col].size;
        b.classList.toggle('on', n > 0);
        b.title = n ? `Filtered: ${[...COLF[col]].map(v => label(col, v)).join(', ')}` : `Filter by ${col}`;
      });
    }
    function close() {
      if (!POP) return;
      POP.el.remove(); POP.btn.setAttribute('aria-expanded', 'false'); POP = null;
    }
    function body(col) {
      const counts = new Map();
      for (const u of rows()) if (matches(u, col)) counts.set(val(u, col), (counts.get(val(u, col)) || 0) + 1);
      for (const v of COLF[col]) if (!counts.has(v)) counts.set(v, 0);
      const order = ORDER[col];
      const vals = [...counts.keys()].sort((a, b) => {
        const ia = order.indexOf(a), ib = order.indexOf(b);
        return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b);
      });
      return `<div class="ft"><span>${NAMES[col]}</span><button type="button" data-clear>${COLF[col].size ? 'Clear' : ''}</button></div>`
        + (vals.length ? vals.map(v => `<label><input type="checkbox" value="${esc(v)}" ${COLF[col].has(v) ? 'checked' : ''}> ${esc(label(col, v))}<span class="c">${(counts.get(v) || 0).toLocaleString()}</span></label>`).join('')
                       : '<div class="muted" style="padding:6px">Nobody matches the other filters.</div>');
    }
    function open(btn) {
      const col = btn.dataset.col;
      if (POP && POP.col === col) return close();
      close();
      const el = document.createElement('div');
      el.className = 'fpop'; el.setAttribute('role', 'dialog'); el.setAttribute('aria-label', `Filter by ${col}`);
      el.innerHTML = body(col);
      document.body.appendChild(el);
      const r = btn.getBoundingClientRect();
      el.style.top = (r.bottom + scrollY + 6) + 'px';
      el.style.left = Math.max(8, Math.min(r.left + scrollX - 8, scrollX + innerWidth - el.offsetWidth - 8)) + 'px';
      btn.setAttribute('aria-expanded', 'true');
      POP = { col, el, btn };
      el.addEventListener('change', e => {
        const cb = e.target.closest('input[type=checkbox]'); if (!cb) return;
        if (cb.checked) COLF[col].add(cb.value); else COLF[col].delete(cb.value);
        onChange(); el.querySelector('[data-clear]').textContent = COLF[col].size ? 'Clear' : '';
      });
      el.addEventListener('click', e => {
        if (!e.target.closest('[data-clear]')) return;
        COLF[col].clear(); onChange(); el.innerHTML = body(col);
      });
      el.querySelector('input')?.focus();
    }

    document.querySelectorAll('th button.flt').forEach(b => {
      if (!b.innerHTML.trim()) b.innerHTML = FUNNEL;
      b.setAttribute('aria-expanded', 'false');
      b.setAttribute('aria-label', `Filter by ${b.dataset.col}`);
      b.addEventListener('click', e => { e.stopPropagation(); open(b); });
    });
    document.addEventListener('click', e => { if (POP && !POP.el.contains(e.target) && e.target !== POP.btn) close(); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && POP) { const b = POP.btn; close(); b.focus(); } });
    window.addEventListener('resize', close);

    return { passes, drawButtons, close, active: () => Object.values(COLF).some(s => s.size > 0) };
  };
})();
