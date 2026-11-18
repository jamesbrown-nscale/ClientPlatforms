/* Client Platforms dialogs: the branded replacement for the browser's confirm(), prompt() and alert().
 *
 * Served by the portal at /static/dialog.js; pages load it with
 *   <script src="/static/dialog.js"></script>
 * and use, all returning promises:
 *   await cp.confirm({ title, message, confirm: 'Delete', danger: true })        → true / false
 *   await cp.prompt({ title, message, label, value, type: 'date', match: name })   → the text, or null
 *   await cp.alert({ title, message })  or  cp.alert('Something went wrong')      → when closed
 * `match` makes the person type that exact text before the button works (for deleting big things).
 * Escape or the backdrop cancels; Enter confirms.
 */
(() => {
  if (window.cp && window.cp.confirm) return;

  const css = `
  dialog.cp-dlg { border: 0; padding: 0; border-radius: 14px; width: min(440px, calc(100vw - 32px)); background: #FDFFF8; color: #040E27;
    box-shadow: 0 24px 64px rgba(4,14,39,.28), 0 2px 8px rgba(4,14,39,.12); overflow: hidden;
    font: 14px/1.5 "Segoe UI", system-ui, -apple-system, sans-serif; }
  dialog.cp-dlg::backdrop { background: rgba(4,14,39,.45); backdrop-filter: blur(2px); }
  dialog.cp-dlg[open] { animation: cp-in .14s ease-out; }
  @keyframes cp-in { from { transform: translateY(6px) scale(.98); opacity: 0; } to { transform: none; opacity: 1; } }
  .cp-dlg .cp-strip { height: 4px; background: #0F41F3; }
  .cp-dlg.danger .cp-strip { background: #D92D20; }
  .cp-dlg .cp-in { padding: 22px 24px 20px; display: grid; grid-template-columns: 40px 1fr; gap: 4px 14px; }
  .cp-dlg .cp-ic { width: 40px; height: 40px; border-radius: 10px; display: grid; place-items: center; background: #E1EAF9; color: #0F41F3; grid-row: span 2; }
  .cp-dlg.danger .cp-ic { background: #FDECEC; color: #B42318; }
  .cp-dlg .cp-ic svg { width: 22px; height: 22px; }
  .cp-dlg h2 { margin: 0; font-size: 17px; font-weight: 600; line-height: 1.35; align-self: center; overflow-wrap: anywhere;
    text-transform: none; letter-spacing: normal; color: #040E27; display: block; }
  .cp-dlg .cp-msg { font-size: 14px; text-transform: none; letter-spacing: normal; margin: 2px 0 0; color: #3B404E; white-space: pre-line; overflow-wrap: anywhere; }
  .cp-dlg .cp-field { grid-column: 2; margin-top: 12px; }
  .cp-dlg .cp-field label { display: block; font-size: 12.5px; font-weight: 600; color: #3B404E; margin-bottom: 6px; }
  .cp-dlg .cp-field input { width: 100%; height: 38px; border: 1px solid #C1CFF7; border-radius: 8px; padding: 0 10px; font: inherit; background: #fff; outline: none; box-sizing: border-box; }
  .cp-dlg .cp-field input:focus { border-color: #0F41F3; box-shadow: 0 0 0 3px rgba(15,65,243,.15); }
  .cp-dlg .cp-field .cp-hint { font-size: 12px; color: #6B7080; margin-top: 4px; }
  .cp-dlg .cp-foot { display: flex; justify-content: flex-end; gap: 10px; padding: 14px 24px; background: #F3F5FB; border-top: 1px solid #E1EAF9; }
  .cp-dlg button { height: 38px; min-width: 88px; border-radius: 8px; padding: 0 16px; font: inherit; font-weight: 600; cursor: pointer; border: 1px solid #C1CFF7; background: #fff; color: #040E27; }
  .cp-dlg button:hover { background: #E1EAF9; }
  .cp-dlg button:focus-visible { outline: none; box-shadow: 0 0 0 3px rgba(15,65,243,.3); }
  .cp-dlg button.cp-ok { background: #0F41F3; border-color: #0F41F3; color: #FDFFF8; }
  .cp-dlg button.cp-ok:hover { background: #0B34C8; }
  .cp-dlg.danger button.cp-ok { background: #D92D20; border-color: #D92D20; }
  .cp-dlg.danger button.cp-ok:hover { background: #B42318; }
  .cp-dlg button.cp-ok:disabled { opacity: .45; cursor: not-allowed; }`;

  const ICON = {
    info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',
    question: '<circle cx="12" cy="12" r="10"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3M12 17h.01"/>',
    danger: '<path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6M10 11v6M14 11v6"/>',
    warn: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0zM12 9v4M12 17h.01"/>',
    edit: '<path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
  };
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  let styled = false;

  function open(kind, o) {
    if (typeof o === 'string') o = { message: o };
    o = o || {};
    if (!styled) { const st = document.createElement('style'); st.textContent = css; document.head.append(st); styled = true; }
    const danger = !!o.danger;
    const icon = o.icon || (danger ? 'danger' : kind === 'prompt' ? 'edit' : kind === 'alert' ? (o.error ? 'warn' : 'info') : 'question');
    const d = document.createElement('dialog');
    d.className = 'cp-dlg' + (danger || o.error ? ' danger' : '');
    d.setAttribute('aria-labelledby', 'cp-dlg-t');
    const title = o.title || (kind === 'alert' ? (o.error ? 'Something went wrong' : 'Note') : kind === 'prompt' ? 'Enter a value' : 'Are you sure?');
    const ok = o.confirm || (kind === 'alert' ? 'OK' : danger ? 'Delete' : 'Continue');
    d.innerHTML = `<div class="cp-strip"></div>
      <form method="dialog" class="cp-form">
        <div class="cp-in">
          <div class="cp-ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[icon] || ICON.info}</svg></div>
          <h2 id="cp-dlg-t">${esc(title)}</h2>
          ${o.message ? `<p class="cp-msg">${esc(o.message)}</p>` : '<span></span>'}
          ${kind === 'prompt' ? `<div class="cp-field">
              ${o.label ? `<label for="cp-dlg-in">${esc(o.label)}</label>` : ''}
              <input id="cp-dlg-in" type="${esc(o.type || 'text')}" value="${esc(o.value ?? '')}" placeholder="${esc(o.placeholder || '')}" autocomplete="off" ${o.label ? '' : `aria-label="${esc(title)}"`}>
              ${o.match ? `<div class="cp-hint">Type <b>${esc(o.match)}</b> to confirm.</div>` : o.hint ? `<div class="cp-hint">${esc(o.hint)}</div>` : ''}
            </div>` : ''}
        </div>
        <div class="cp-foot">
          ${kind === 'alert' ? '' : `<button type="button" class="cp-cancel">${esc(o.cancel || 'Cancel')}</button>`}
          <button type="submit" class="cp-ok" value="ok">${esc(ok)}</button>
        </div>
      </form>`;
    document.body.append(d);
    const input = d.querySelector('#cp-dlg-in');
    const okBtn = d.querySelector('.cp-ok');
    const check = () => { if (input && o.match) okBtn.disabled = input.value.trim() !== String(o.match).trim(); };
    if (input) { input.addEventListener('input', check); check(); }
    return new Promise(resolve => {
      let result = kind === 'confirm' ? false : kind === 'prompt' ? null : undefined;
      d.querySelector('.cp-form').addEventListener('submit', e => {
        if (okBtn.disabled) { e.preventDefault(); return; }
        result = kind === 'confirm' ? true : kind === 'prompt' ? input.value : undefined;
      });
      d.querySelector('.cp-cancel')?.addEventListener('click', () => d.close());
      d.addEventListener('click', e => { if (e.target === d) d.close(); });   // the backdrop
      d.addEventListener('close', () => { d.remove(); resolve(result); });
      d.showModal();
      (input || (kind === 'alert' || !danger ? okBtn : d.querySelector('.cp-cancel'))).focus();
      if (input && input.type === 'text') input.select();
    });
  }

  window.cp = Object.assign(window.cp || {}, {
    confirm: o => open('confirm', o),
    prompt: o => open('prompt', o),
    alert: o => open('alert', typeof o === 'string' ? { message: o, error: true } : o),
  });
})();
