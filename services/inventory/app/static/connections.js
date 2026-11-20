/* Settings → Connections: Jamf Pro and Microsoft Intune, where Devices reads its devices from.
 *
 * Served by the inventory service at /devices/static/connections.js and shown in the portal's
 * Settings → Connections tab:  window.cpConnections.mount(element)
 * Reads and saves /devices/api/connections (Devices admins only). Secrets go one way: the page can
 * set or replace one, never read it back.
 */
(() => {
  const API = '/devices/api/connections';
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const when = s => s ? new Date(s).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : '';
  const LOGO = {
    jamf: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="11" rx="1.5"/><path d="M2 19h20"/></svg>',
    intune: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="8" height="8" rx="1"/><rect x="13" y="3" width="8" height="8" rx="1"/><rect x="3" y="13" width="8" height="8" rx="1"/><rect x="13" y="13" width="8" height="8" rx="1"/></svg>',
  };
  const INFO = {
    jamf: {
      title: 'Jamf Pro', reads: 'Macs, and iPhones and iPads',
      fields: [['url', 'Jamf Pro address', 'https://yourcompany.jamfcloud.com', 'text'], ['clientId', 'Client ID', '', 'text']],
      help: `<ol>
        <li>In Jamf Pro, go to <b>Settings → System → API roles and clients</b>.</li>
        <li>Under <b>API Roles</b>, make a role (e.g. "Client Platforms read only") with <b>Read Computers</b> and <b>Read Mobile Devices</b>.</li>
        <li>Under <b>API Clients</b>, make a client with that role, turn it on, then <b>Generate client secret</b>.</li>
        <li>Paste the client ID and secret here. Nothing is ever changed in Jamf: it's only read.</li></ol>`,
    },
    intune: {
      title: 'Microsoft Intune', reads: 'Windows PCs, Macs and phones managed by Intune',
      fields: [['tenantId', 'Directory (tenant) ID', '00000000-0000-0000-0000-000000000000', 'text'], ['clientId', 'Application (client) ID', '', 'text']],
      help: `<ol>
        <li>In the <b>Microsoft Entra admin centre</b>, go to <b>App registrations → New registration</b> (e.g. "Client Platforms").</li>
        <li>Under <b>API permissions</b>, add <b>Microsoft Graph → Application permissions → DeviceManagementManagedDevices.Read.All</b>, then <b>Grant admin consent</b>.</li>
        <li>Under <b>Certificates &amp; secrets</b>, make a <b>New client secret</b> and copy its <b>Value</b>.</li>
        <li>Paste the tenant ID and application ID (from the app's Overview page) and the secret here. Devices are only read.</li></ol>`,
    },
  };
  const css = `
  .cx .card { background: #FDFFF8; border: 1px solid #E1EAF9; border-radius: 14px; padding: 22px 24px; margin-bottom: 20px; }
  .cx .top { display: flex; gap: 14px; align-items: flex-start; }
  .cx .logo { width: 40px; height: 40px; border-radius: 10px; background: #E1EAF9; color: #0F41F3; display: grid; place-items: center; flex-shrink: 0; }
  .cx .logo svg { width: 22px; height: 22px; }
  .cx h2 { font-size: 17px; font-weight: 600; margin: 0; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .cx .sub { color: #3B404E; margin: 2px 0 0; }
  .cx .st { display: inline-block; font-size: 12px; font-weight: 600; padding: 2px 10px; border-radius: 11px; background: #ECEEF3; color: #3B404E; }
  .cx .st.ok { background: #D6FAEE; color: #006B4A; } .cx .st.err { background: #FDECEC; color: #B42318; } .cx .st.off { background: #FFF1D6; color: #8A5300; }
  .cx .status { margin: 14px 0 0; padding: 10px 12px; border-radius: 8px; background: #F3F5FB; color: #3B404E; font-size: 13px; }
  .cx .status.err { background: #FDECEC; color: #B42318; }
  .cx .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 16px; max-width: 760px; }
  .cx label.l { display: block; font-weight: 600; margin: 14px 0 6px; }
  .cx input[type=text], .cx input[type=url], .cx input[type=password] { width: 100%; height: 40px; background: #fff; border: 1px solid #C1CFF7; border-radius: 8px; padding: 0 12px; outline: none; font: inherit; }
  .cx input:focus { border-color: #0F41F3; box-shadow: 0 0 0 3px rgba(15,65,243,.12); }
  .cx .hint { color: #3B404E; font-size: 12.5px; margin-top: 4px; }
  .cx .check { display: flex; gap: 8px; align-items: center; margin-top: 14px; cursor: pointer; }
  .cx .row { display: flex; gap: 10px; align-items: center; margin-top: 18px; flex-wrap: wrap; }
  .cx .row .sp { flex: 1; }
  .cx details { margin-top: 16px; border-top: 1px solid #E1EAF9; padding-top: 12px; color: #3B404E; }
  .cx summary { cursor: pointer; font-weight: 600; color: #0F41F3; }
  .cx details ol { margin: 10px 0 0; padding-left: 20px; } .cx details li { margin: 4px 0; }
  .cx .note { border-radius: 8px; padding: 10px 12px; margin-top: 14px; }
  .cx .note.ok { background: #D6FAEE; color: #006B4A; } .cx .note.err { background: #FDECEC; color: #B42318; }
  .cx .intro { color: #3B404E; margin: -4px 0 18px; }
  @media (max-width: 700px) { .cx .grid { grid-template-columns: 1fr; } }`;

  async function api(path, opts = {}) {
    const res = await fetch(path, { ...opts, cache: 'no-store', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'nscale-admin', ...(opts.headers || {}) } });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const d = data.detail;
      throw new Error(Array.isArray(d) ? d.map(x => (x.msg || '').replace(/^Value error, /, '')).join('; ') : (d || res.statusText));
    }
    return data;
  }

  let ROOT, ITEMS = [], EVERY = 60;
  const NOTE = {};

  function badge(c) {
    if (!c.connected) return '<span class="st">Not connected</span>';
    if (c.syncing) return '<span class="st">Syncing…</span>';
    if (!c.enabled) return '<span class="st off">Paused</span>';
    if (c.lastError) return '<span class="st err">Not working</span>';
    return c.lastOk ? '<span class="st ok">Connected</span>' : '<span class="st">Not synced yet</span>';
  }

  function status(c) {
    if (!c.connected) return '';
    if (c.lastError) return `<div class="status err"><b>Last try ${esc(when(c.lastSync))}:</b> ${esc(c.lastError)}</div>`;
    if (c.lastOk) return `<div class="status">Last synced ${esc(when(c.lastOk))}: <b>${c.count ?? 0}</b> device${c.count === 1 ? '' : 's'}.${c.lastNote ? ' ' + esc(c.lastNote) : ''}</div>`;
    return '';
  }

  function card(c) {
    const info = INFO[c.kind];
    const fields = info.fields.map(([k, label, ph, type]) => `<div><label class="l" for="cx-${c.kind}-${k}">${label}</label>
      <input id="cx-${c.kind}-${k}" data-k="${k}" type="${type}" placeholder="${esc(ph)}" value="${esc(c[k] || '')}" autocomplete="off" spellcheck="false"></div>`).join('');
    return `<section class="card" data-kind="${c.kind}">
      <div class="top"><span class="logo">${LOGO[c.kind]}</span><div style="flex:1;min-width:0">
        <h2>${info.title} ${badge(c)}</h2><p class="sub">Reads ${info.reads}.</p></div></div>
      ${NOTE[c.kind] && !NOTE[c.kind].ok ? '' : status(c)}
      <form autocomplete="off">
        <div class="grid">${fields}
          <div><label class="l" for="cx-${c.kind}-secret">Client secret</label>
            <input id="cx-${c.kind}-secret" data-k="secret" type="password" autocomplete="new-password" placeholder="${c.hasSecret ? 'Saved: leave blank to keep it' : ''}">
            <div class="hint">${c.hasSecret ? 'Kept encrypted. Type a new one only to replace it.' : 'Kept encrypted, and never shown again.'}</div></div>
        </div>
        <label class="check"><input type="checkbox" data-k="enabled" ${!c.connected || c.enabled ? 'checked' : ''}> ${EVERY <= 0 ? 'Include in Sync now' : 'Sync automatically, every ' + (EVERY >= 60 && EVERY % 60 === 0 ? (EVERY === 60 ? 'hour' : EVERY / 60 + ' hours') : EVERY + ' minutes')}</label>
        <div class="row">
          <button class="btn primary" type="submit">${c.connected ? 'Save' : 'Connect'}</button>
          ${c.connected ? '<button class="btn" type="button" data-act="test">Sync now</button>' : ''}
          <span class="sp"></span>
          ${c.connected ? '<button class="btn danger" type="button" data-act="remove">Disconnect</button>' : ''}
        </div>
        ${NOTE[c.kind] ? `<div class="note ${NOTE[c.kind].ok ? 'ok' : 'err'}" role="status">${esc(NOTE[c.kind].text)}</div>` : ''}
      </form>
      <details${c.connected ? '' : ' open'}><summary>How to set this up</summary>${info.help}</details>
    </section>`;
  }

  function draw() {
    ROOT.innerHTML = `<div class="cx"><p class="intro">Devices reads every device from these, read only. A user's primary device is picked from them on the Users page.</p>${ITEMS.map(card).join('')}</div>`;
    ROOT.querySelectorAll('section[data-kind]').forEach(sec => {
      const kind = sec.dataset.kind;
      const form = sec.querySelector('form');
      const val = k => form.querySelector(`[data-k="${k}"]`);
      form.onsubmit = async e => {
        e.preventDefault();
        const btn = form.querySelector('button[type=submit]');
        const first = !ITEMS.find(c => c.kind === kind).connected;
        const body = { clientId: val('clientId').value, enabled: val('enabled').checked, secret: val('secret').value || null };
        if (val('url')) body.url = val('url').value;
        if (val('tenantId')) body.tenantId = val('tenantId').value;
        btn.disabled = true;
        try {
          ITEMS = (await api(`${API}/${kind}`, { method: 'PUT', body: JSON.stringify(body) })).items;
          NOTE[kind] = { ok: true, text: 'Saved.' };
          if (first || body.secret) return test(kind);   // check it works straight away
        } catch (err) { NOTE[kind] = { ok: false, text: err.message }; }
        draw();
      };
      sec.querySelector('[data-act=test]')?.addEventListener('click', () => test(kind));
      sec.querySelector('[data-act=remove]')?.addEventListener('click', async () => {
        const ok = await (window.cp?.confirm ? cp.confirm({ title: `Disconnect ${INFO[kind].title}?`,
          message: `Its saved details and secret are deleted, and its devices are taken off the Devices page. Users keep their tags, but a primary device from ${INFO[kind].title} will show as gone.`,
          confirm: 'Disconnect', danger: true }) : Promise.resolve(confirm('Disconnect?')));
        if (!ok) return;
        try {
          const r = await api(`${API}/${kind}`, { method: 'DELETE' });
          ITEMS = r.items; NOTE[kind] = { ok: true, text: `Disconnected. ${r.removed} device${r.removed === 1 ? '' : 's'} removed.` };
        } catch (err) { NOTE[kind] = { ok: false, text: err.message }; }
        draw();
      });
    });
  }

  async function test(kind) {
    ITEMS = ITEMS.map(c => c.kind === kind ? { ...c, syncing: true } : c);
    NOTE[kind] = { ok: true, text: `Signing in to ${INFO[kind].title} and reading devices…` };
    draw();
    try {
      const r = await api(`${API}/${kind}/test`, { method: 'POST' });
      ITEMS = r.items;
      NOTE[kind] = r.ok ? { ok: true, text: `Connected: ${r.count} device${r.count === 1 ? '' : 's'} read.${r.note ? ' ' + r.note : ''}` } : { ok: false, text: r.error };
    } catch (err) {
      ITEMS = ITEMS.map(c => c.kind === kind ? { ...c, syncing: false } : c);
      NOTE[kind] = { ok: false, text: err.message };
    }
    draw();
  }

  async function mount(el) {
    ROOT = el;
    if (!document.getElementById('cx-css')) {
      const st = document.createElement('style'); st.id = 'cx-css'; st.textContent = css; document.head.append(st);
    }
    el.innerHTML = '<section class="card"><p class="sub">Loading…</p></section>';
    try {
      const r = await api(API);
      ITEMS = r.items; EVERY = r.syncMinutes || 60;
      draw();
    } catch (err) {
      el.innerHTML = `<section class="card"><h2>Connections</h2><p class="sub">${esc(err.message)}</p></section>`;
    }
  }

  window.cpConnections = { mount };
})();
