/* @mention picker for comment boxes, shared by the site's tools.
 *
 *   <script src="/static/mention.js"></script>
 *   const m = cpMention.attach(textarea, () => ({ involved: [{email, name, hint}], everyone: [{email, name}] }));
 *   ... m.picked()  → emails of people picked whose @tag is still in the text (send them with the comment)
 *   ... m.reset()   → after sending
 *
 * Typing @ opens a list: the people involved first, then everyone else. Typing narrows it by name
 * or email; ↑ ↓ move, Enter or Tab picks, Escape closes. Picking writes @first.last (the email's
 * local part) into the text.
 */
(() => {
  if (window.cpMention) return;
  const css = `
  .cp-mention { position: absolute; z-index: 1000; width: 300px; max-height: 280px; overflow: auto; background: #FDFFF8; border: 1px solid #C1CFF7;
    border-radius: 10px; box-shadow: 0 12px 32px rgba(4,14,39,.18); padding: 4px; font: 14px/1.4 "Segoe UI", system-ui, -apple-system, sans-serif; color: #040E27; }
  .cp-mention .g { font-size: 11px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; color: #3B404E; padding: 8px 8px 4px; }
  .cp-mention .o { display: flex; gap: 10px; align-items: center; padding: 6px 8px; border-radius: 8px; cursor: pointer; }
  .cp-mention .o[aria-selected="true"] { background: #E1EAF9; }
  .cp-mention .av { width: 28px; height: 28px; border-radius: 50%; background: #ECE8FD; color: #4A3AB0; display: grid; place-items: center; font-size: 11px; font-weight: 700; flex-shrink: 0; }
  .cp-mention .n { font-weight: 600; }
  .cp-mention .s { font-size: 12px; color: #6B7080; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .cp-mention .none { padding: 10px; color: #6B7080; font-size: 13px; }`;
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const initials = n => String(n || '?').split(/[\s.@]+/).filter(Boolean).slice(0, 2).map(w => w[0].toUpperCase()).join('');
  const tagOf = email => email.split('@')[0].toLowerCase();
  let styled = false;

  /** Where the caret is in a textarea, in page coordinates (a mirror div with the same text). */
  function caretXY(ta) {
    const st = getComputedStyle(ta), div = document.createElement('div');
    for (const p of ['boxSizing', 'width', 'fontFamily', 'fontSize', 'fontWeight', 'lineHeight', 'letterSpacing', 'paddingTop', 'paddingRight',
                     'paddingBottom', 'paddingLeft', 'borderTopWidth', 'borderRightWidth', 'borderBottomWidth', 'borderLeftWidth', 'whiteSpace', 'wordWrap'])
      div.style[p] = st[p];
    Object.assign(div.style, { position: 'absolute', visibility: 'hidden', whiteSpace: 'pre-wrap', overflowWrap: 'break-word', top: '0', left: '-9999px' });
    div.textContent = ta.value.slice(0, ta.selectionStart);
    const mark = document.createElement('span'); mark.textContent = '​'; div.append(mark);
    document.body.append(div);
    const r = ta.getBoundingClientRect(), x = mark.offsetLeft - ta.scrollLeft, y = mark.offsetTop - ta.scrollTop + parseFloat(st.lineHeight || 20);
    div.remove();
    return { left: r.left + window.scrollX + Math.min(x, r.width - 40), top: r.top + window.scrollY + Math.min(y, r.height) + 4 };
  }

  function attach(ta, source) {
    if (!styled) { const s = document.createElement('style'); s.textContent = css; document.head.append(s); styled = true; }
    const box = document.createElement('div');
    box.className = 'cp-mention'; box.setAttribute('role', 'listbox'); box.hidden = true;
    document.body.append(box);
    ta.setAttribute('aria-autocomplete', 'list');
    let items = [], sel = 0, start = -1;
    const chosen = new Map();   // tag → email

    const query = () => {
      const upto = ta.value.slice(0, ta.selectionStart), m = upto.match(/(^|[\s(])@([\w.+-]*)$/);
      return m ? { at: upto.length - m[2].length - 1, text: m[2].toLowerCase() } : null;
    };
    const close = () => { box.hidden = true; start = -1; ta.removeAttribute('aria-activedescendant'); };
    const draw = () => {
      const q = query();
      if (!q) return close();
      start = q.at;
      const src = source() || {}, seen = new Set();
      const match = p => !q.text || p.name.toLowerCase().split(/\s+/).some(w => w.startsWith(q.text)) || p.name.toLowerCase().startsWith(q.text) || p.email.toLowerCase().startsWith(q.text);
      const take = list => (list || []).filter(p => p && p.email && !seen.has(p.email) && match(p) && seen.add(p.email));
      const involved = take(src.involved), everyone = take(src.everyone).slice(0, 30);
      items = [...involved, ...everyone];
      sel = Math.min(sel, Math.max(0, items.length - 1));
      const row = (p, i) => `<div class="o" role="option" id="cpm-${i}" data-i="${i}" aria-selected="${i === sel}"><span class="av">${esc(initials(p.name))}</span>
        <div style="min-width:0"><div class="n">${esc(p.name)}</div><div class="s">${esc(p.hint || p.email)}</div></div></div>`;
      box.innerHTML = items.length
        ? (involved.length ? `<div class="g">Involved</div>${involved.map(row).join('')}` : '') +
          (everyone.length ? `<div class="g">${involved.length ? 'Everyone else' : 'People'}</div>${everyone.map((p, i) => row(p, i + involved.length)).join('')}` : '')
        : '<div class="none">Nobody matches.</div>';
      const xy = caretXY(ta);
      box.style.left = Math.min(xy.left, window.scrollX + document.documentElement.clientWidth - 316) + 'px';
      box.style.top = xy.top + 'px';
      box.hidden = false;
      ta.setAttribute('aria-activedescendant', `cpm-${sel}`);
      box.querySelectorAll('.o').forEach(o => {
        o.onmousedown = e => { e.preventDefault(); pick(Number(o.dataset.i)); };
        o.onmousemove = () => { if (sel !== Number(o.dataset.i)) { sel = Number(o.dataset.i); mark(); } };
      });
      box.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' });
    };
    const mark = () => { box.querySelectorAll('.o').forEach(o => o.setAttribute('aria-selected', String(Number(o.dataset.i) === sel)));
      ta.setAttribute('aria-activedescendant', `cpm-${sel}`); box.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' }); };
    const pick = i => {
      const p = items[i];
      if (!p || start < 0) return;
      const tag = tagOf(p.email), before = ta.value.slice(0, start), after = ta.value.slice(ta.selectionStart);
      ta.value = `${before}@${tag} ${after.replace(/^\s/, '')}`;
      const pos = before.length + tag.length + 2;
      ta.setSelectionRange(pos, pos); ta.focus();
      chosen.set(tag, p.email);
      close();
      ta.dispatchEvent(new Event('input', { bubbles: true }));
    };

    ta.addEventListener('input', () => { sel = 0; draw(); });
    ta.addEventListener('click', draw);
    ta.addEventListener('keydown', e => {
      if (box.hidden || !items.length) { if (e.key === 'Escape' && !box.hidden) { e.preventDefault(); close(); } return; }
      if (e.key === 'ArrowDown') { e.preventDefault(); sel = (sel + 1) % items.length; mark(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); sel = (sel - 1 + items.length) % items.length; mark(); }
      else if ((e.key === 'Enter' && !e.metaKey && !e.ctrlKey) || e.key === 'Tab') { e.preventDefault(); e.stopPropagation(); pick(sel); }
      else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); }
    }, true);
    ta.addEventListener('blur', () => setTimeout(close, 150));
    window.addEventListener('scroll', () => { if (!box.hidden) draw(); }, true);

    return {
      picked: () => [...chosen].filter(([tag]) => new RegExp(`(^|[\\s(])@${tag.replace(/[.+-]/g, '\\$&')}\\b`, 'i').test(ta.value)).map(([, e]) => e),
      reset: () => { chosen.clear(); close(); },
      destroy: () => box.remove(),
    };
  }

  window.cpMention = { attach };
})();
