/* Download CSV on the Users pages: everyone, or just the current view (the filters and order on screen).
 *
 *   csvDownload(button, { total: () => ALL.length, view: () => VIEW })
 *
 * With no filter narrowing the list it downloads everyone straight away; otherwise it asks which.
 * Everyone is GET api/users.csv; the view is POST api/users.csv with the view's emails, in order. */
(function () {
  const n = x => x.toLocaleString();

  async function save(rows) {
    const res = await fetch('api/users.csv', { method: 'POST', cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'nscale-admin' },
      body: JSON.stringify({ emails: rows.map(u => u.email) }) });
    if (res.status === 401) { location.reload(); return; }
    if (!res.ok) { const j = await res.json().catch(() => ({})); throw new Error(j.detail || `Error ${res.status}`); }
    const name = (/filename="([^"]+)"/.exec(res.headers.get('content-disposition') || '') || [])[1] || 'users.csv';
    const a = Object.assign(document.createElement('a'), { href: URL.createObjectURL(await res.blob()), download: name });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  }
  function everyone() {
    const a = Object.assign(document.createElement('a'), { href: 'api/users.csv', download: '' });
    document.body.append(a); a.click(); a.remove();
  }

  window.csvDownload = function (btn, { total, view }) {
    btn.addEventListener('click', e => {
      e.preventDefault();
      const all = total(), rows = view();
      if (rows.length === all) return everyone();
      document.querySelector('dialog.csvdlg')?.remove();
      const d = document.createElement('dialog');
      d.className = 'imp csvdlg';
      d.style.width = 'min(440px, calc(100vw - 32px))';
      d.innerHTML = `<form method="dialog"><div class="in">
          <h2>Download CSV</h2>
          <p class="sub" style="margin:0 0 14px">The list is filtered. Download everyone, or just who's showing, in the order shown?</p>
          <label class="addnew"><input type="radio" name="which" value="view" checked> <span><b>Current view</b> · ${n(rows.length)} ${rows.length === 1 ? 'user' : 'users'}</span></label>
          <label class="addnew"><input type="radio" name="which" value="all"> <span><b>All users</b> · ${n(all)}</span></label>
          <div class="notice" hidden style="margin:12px 0 0"></div>
        </div>
        <div class="foot"><button class="btn" value="cancel" formnovalidate>Cancel</button><button class="btn primary" value="go">Download</button></div></form>`;
      document.body.append(d);
      d.querySelector('form').addEventListener('submit', async ev => {
        if (ev.submitter?.value !== 'go') return;
        ev.preventDefault();
        const go = ev.submitter;
        if (d.querySelector('input[name=which]:checked').value === 'all') { everyone(); d.close(); return; }
        go.disabled = true; go.textContent = 'Preparing…';
        try { await save(rows); d.close(); }
        catch (err) { const nb = d.querySelector('.notice'); nb.hidden = false; nb.textContent = err.message; go.disabled = false; go.textContent = 'Download'; }
      });
      d.addEventListener('close', () => d.remove());
      d.showModal();
    });
  };
})();
