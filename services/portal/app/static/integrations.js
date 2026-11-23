/* Settings → Connections → Integrations: Jira. The portal keeps it (/api/integrations/jira), for people
 * with the "Manage integrations" site ability: window.cpIntegrations.mount(element).
 * The API token goes one way: the page can set or replace it, never read it back.
 */
(() => {
  const API = '/api/integrations';
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const when = s => s ? new Date(s).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : '';
  const LOGO = { jira: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 3 12l9 9 9-9-9-9Z"/><path d="m12 8-4 4 4 4 4-4-4-4Z"/></svg>' };
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
  .cx h3.cxh { font-size: 13px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; color: #3B404E; margin: 6px 0 10px; }
  .cx h3.cxh ~ h3.cxh { margin-top: 30px; }
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

  let ROOT, JIRA = null;
  const NOTE = {};

  function jiraCard(j) {
    const st = !j.connected ? '<span class="st">Not connected</span>' : j.lastError ? '<span class="st err">Not working</span>'
      : j.lastOk ? '<span class="st ok">Connected</span>' : '<span class="st">Not checked yet</span>';
    const status = !j.connected || NOTE.jira ? '' : j.lastError ? `<div class="status err"><b>Last check ${esc(when(j.lastCheck))}:</b> ${esc(j.lastError)}</div>`
      : j.lastOk ? `<div class="status">Checked ${esc(when(j.lastOk))}. ${esc(j.lastNote)}</div>` : '';
    return `<section class="card" data-int="jira">
      <div class="top"><span class="logo">${LOGO.jira}</span><div style="flex:1;min-width:0">
        <h2>Jira ${st}</h2><p class="sub">Jira Cloud, signed in with an Atlassian account and an API token.</p></div></div>
      ${status}
      <form autocomplete="off">
        <div class="grid">
          <div><label class="l" for="cx-jira-url">Jira address</label><input id="cx-jira-url" data-k="url" type="text" placeholder="https://yourcompany.atlassian.net" value="${esc(j.url)}" autocomplete="off" spellcheck="false"></div>
          <div><label class="l" for="cx-jira-project">Project key <span style="font-weight:400;color:#3B404E">(optional)</span></label><input id="cx-jira-project" data-k="project" type="text" placeholder="e.g. CP" value="${esc(j.project)}" autocomplete="off" spellcheck="false" style="text-transform:uppercase">
            <div class="hint">The project to work with, e.g. the Client Platform queue.</div></div>
          <div><label class="l" for="cx-jira-email">Account email</label><input id="cx-jira-email" data-k="email" type="text" placeholder="it-automation@nscale.com" value="${esc(j.email)}" autocomplete="off" spellcheck="false"></div>
          <div><label class="l" for="cx-jira-token">API token</label>
            <input id="cx-jira-token" data-k="token" type="password" autocomplete="new-password" placeholder="${j.hasToken ? 'Saved: leave blank to keep it' : ''}">
            <div class="hint">${j.hasToken ? 'Kept encrypted. Type a new one only to replace it.' : 'Kept encrypted, and never shown again.'}</div></div>
        </div>
        <div class="row">
          <button class="btn primary" type="submit">${j.connected ? 'Save' : 'Connect'}</button>
          ${j.connected ? '<button class="btn" type="button" data-act="check">Check connection</button>' : ''}
          <span class="sp"></span>
          ${j.connected ? '<button class="btn danger" type="button" data-act="remove">Disconnect</button>' : ''}
        </div>
        ${NOTE.jira ? `<div class="note ${NOTE.jira.ok ? 'ok' : 'err'}" role="status">${esc(NOTE.jira.text)}</div>` : ''}
      </form>
      <details${j.connected ? '' : ' open'}><summary>How to set this up</summary><ol>
        <li>Sign in to Atlassian as the account Client Platforms should use (ideally a service account with access to the project).</li>
        <li>Go to <b>id.atlassian.com → Security → Create and manage API tokens</b>, and <b>Create API token</b> (e.g. "Client Platforms").</li>
        <li>Paste your Jira address, that account's email and the token here. Add the project key if you'll work with one project (e.g. <b>CP</b>).</li>
        <li>The token can do what that account can do in Jira, so give the account only the access it needs.</li></ol></details>
    </section>`;
  }

  function wireJira() {
    const sec = ROOT.querySelector('section[data-int="jira"]');
    if (!sec) return;
    const form = sec.querySelector('form'), val = k => form.querySelector(`[data-k="${k}"]`).value;
    const done = r => { JIRA = r.jira; if ('ok' in r) NOTE.jira = r.ok ? { ok: true, text: `Connected. ${r.note}` } : { ok: false, text: r.error }; draw(); };
    form.onsubmit = async e => {
      e.preventDefault();
      const btn = form.querySelector('button[type=submit]'); btn.disabled = true;
      NOTE.jira = null;
      try { done(await api(`${API}/jira`, { method: 'PUT', body: JSON.stringify({ url: val('url'), email: val('email'), project: val('project'), token: val('token') || null }) })); }
      catch (err) { NOTE.jira = { ok: false, text: err.message }; draw(); }
    };
    sec.querySelector('[data-act=check]')?.addEventListener('click', async () => {
      NOTE.jira = { ok: true, text: 'Signing in to Jira…' }; draw();
      try { done(await api(`${API}/jira/test`, { method: 'POST' })); } catch (err) { NOTE.jira = { ok: false, text: err.message }; draw(); }
    });
    sec.querySelector('[data-act=remove]')?.addEventListener('click', async () => {
      const ok = await (window.cp?.confirm ? cp.confirm({ title: 'Disconnect Jira?', message: 'Its saved address, email and API token are deleted.', confirm: 'Disconnect', danger: true })
        : Promise.resolve(confirm('Disconnect Jira?')));
      if (!ok) return;
      try { JIRA = (await api(`${API}/jira`, { method: 'DELETE' })).jira; NOTE.jira = { ok: true, text: 'Disconnected.' }; } catch (err) { NOTE.jira = { ok: false, text: err.message }; }
      draw();
    });
  }

  function draw() {
    ROOT.innerHTML = `<div class="cx"><h3 class="cxh">Integrations</h3><p class="intro">Other systems Client Platforms works with.</p>${jiraCard(JIRA)}</div>`;
    wireJira();
  }

  async function mount(el) {
    ROOT = el;
    if (!document.getElementById('cxi-css')) {
      const st = document.createElement('style'); st.id = 'cxi-css'; st.textContent = css; document.head.append(st);
    }
    try { JIRA = (await api(API)).jira; draw(); }
    catch (err) { el.innerHTML = `<section class="card"><h2>Integrations</h2><p class="sub">${esc(err.message)}</p></section>`; }
  }

  window.cpIntegrations = { mount, refresh: () => ROOT && mount(ROOT) };
})();
