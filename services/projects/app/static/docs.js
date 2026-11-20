/* Files in the Document Library: the file panel in a document's form, and opening files to view or edit.
 *
 *   CSV / TSV        a grid (or as text); saved back as CSV
 *   text, Markdown …  a text box
 *   Word (.docx)     paragraphs and tables, with headings, lists, bold, italic and underline;
 *                    only what you change is rewritten (the server keeps pictures, layout and the rest)
 *   Excel (.xlsx)    a grid per sheet; only the cells you change are written
 *   PDF, pictures    shown; anything else downloads
 *
 * Every save is a new version (Versions lists them, to download or restore). Uses index.html's globals:
 * $, esc, api, P, OPEN, ITEMS, canEdit, fmtWhen, drawList, MOD.
 */
// PENDING (a file picked for a new document) and ED (the open editor) are declared in index.html.

const sizeOf = n => n < 1024 ? `${n} B` : n < 1048576 ? `${Math.round(n / 1024)} KB` : `${(n / 1048576).toFixed(1)} MB`;
const extOf = n => (String(n).includes('.') ? String(n).split('.').pop() : '').toLowerCase();
const EXT_COLOUR = { docx: '#2B579A', doc: '#2B579A', xlsx: '#217346', xls: '#217346', csv: '#217346', tsv: '#217346', pptx: '#C43E1C', ppt: '#C43E1C', pdf: '#B42318' };
const badge = name => { const e = extOf(name) || 'file'; return `<span class="fext" style="background:${EXT_COLOUR[e] || '#5B6275'}">${esc(e.slice(0, 4).toUpperCase())}</span>`; };
const fileUrl = (mod, item, rest = '') => `projects/${P.id}/items/${mod.key}/${item.id}/file${rest}`;
const EDITS = new Set(['text', 'docx', 'xlsx']), VIEWS = new Set(['pdf', 'image']);
const MAX_MB = 25;

/** The list's File column: the file's name, which opens it. */
function fileChip(f, item) {
  if (!f) return '<span class="muted">—</span>';
  const open = EDITS.has(f.kind) || VIEWS.has(f.kind);
  return `<button type="button" class="fchip" ${open ? `data-fileopen="${esc(item.id)}"` : `data-filedl="${esc(item.id)}"`} title="${open ? 'Open' : 'Download'} ${esc(f.name)}">${badge(f.name)}<span>${esc(f.name)}</span></button>`;
}
document.addEventListener('click', e => {
  const o = e.target.closest('[data-fileopen],[data-filedl]');
  if (!o || !OPEN_MOD()) return;
  e.stopPropagation();
  const item = ITEMS.find(i => i.id === (o.dataset.fileopen || o.dataset.filedl));
  if (!item) return;
  if (o.dataset.filedl) location.href = 'api/' + fileUrl(OPEN_MOD(), item);
  else openFile(OPEN_MOD(), item);
}, true);
const OPEN_MOD = () => OPEN?.mod || MOD(location.hash.split('/')[3] || '');

/* ---------- The panel in the form ---------- */
function drawFileBox() {
  const box = document.querySelector('[data-filebox]');
  if (!box || !OPEN) return;
  const { mod, item } = OPEN, f = item?.data.file, ro = !canEdit();
  if (f) {
    const open = EDITS.has(f.kind) || VIEWS.has(f.kind);
    box.innerHTML = `<div class="fcard">${badge(f.name)}
        <div class="fmeta"><b>${esc(f.name)}</b><span>${esc(sizeOf(f.size))} · version ${f.version} · ${esc(fmtWhen(f.updatedAt))} by ${esc(f.updatedBy)}</span></div></div>
      <div class="facts">
        ${open ? `<button type="button" class="btn small primary" data-fopen>${!ro && EDITS.has(f.kind) ? 'Open and edit' : 'View'}</button>` : ''}
        <a class="btn small" href="api/${esc(fileUrl(mod, item))}">Download</a>
        ${ro ? '' : '<button type="button" class="btn small" data-freplace>Upload a new version</button><input type="file" hidden data-finput>'}
        <button type="button" class="btn small" data-fversions aria-expanded="false">Versions</button>
      </div>
      ${!open ? `<div class="hint">${esc(extOf(f.name).toUpperCase() || 'This')} files can't be opened here: download it, change it, then upload it as a new version.</div>` : ''}
      <div data-fvlist hidden></div>`;
  } else if (ro) box.innerHTML = '<span class="muted">No file uploaded.</span>';
  else box.innerHTML = `<label class="fdrop" data-fdrop><input type="file" hidden data-finput>
      ${PENDING ? `${badge(PENDING.name)}<span><b>${esc(PENDING.name)}</b> <span class="muted">${esc(sizeOf(PENDING.size))} · uploaded when you save</span></span><button type="button" class="x" data-fclear title="Remove">×</button>`
               : `<span><b>Drop a file here</b> or <u>choose one</u><br><span class="muted">Word, Excel, CSV, PDF, PowerPoint, text … up to ${MAX_MB} MB</span></span>`}</label>`;

  const input = box.querySelector('[data-finput]');
  const take = file => {
    if (!file) return;
    if (file.size > MAX_MB * 1048576) return cp.alert(`Files can be at most ${MAX_MB} MB.`);
    if (item) return upload(mod, item, file);
    PENDING = file;
    const title = $('dForm').querySelector('[data-k="title"]');
    if (title && !title.value.trim()) title.value = file.name.replace(/\.[^.]+$/, '');
    drawFileBox();
  };
  if (input) input.onchange = () => take(input.files[0]);
  box.querySelector('[data-freplace]')?.addEventListener('click', () => input.click());
  box.querySelector('[data-fclear]')?.addEventListener('click', e => { e.preventDefault(); PENDING = null; drawFileBox(); });
  box.querySelector('[data-fopen]')?.addEventListener('click', () => openFile(mod, item));
  box.querySelector('[data-fversions]')?.addEventListener('click', e => toggleVersions(mod, item, e.currentTarget));
  const drop = box.querySelector('[data-fdrop]') || box;
  if (!ro) {
    drop.ondragover = e => { e.preventDefault(); drop.classList.add('over'); };
    drop.ondragleave = () => drop.classList.remove('over');
    drop.ondrop = e => { e.preventDefault(); drop.classList.remove('over'); take(e.dataTransfer.files[0]); };
  }
}

/** Upload a file as the record's next version. `base` is the version an edit started from. */
async function upload(mod, item, file, base, name) {
  const headers = { 'Content-Type': file.type || 'application/octet-stream', 'X-Filename': encodeURIComponent(name || file.name) };
  if (base) headers['X-Base-Version'] = String(base);
  const box = document.querySelector('[data-filebox]');
  if (box && !base) box.insertAdjacentHTML('beforeend', '<div class="hint" data-fbusy>Uploading…</div>');
  try {
    const saved = await api(fileUrl(mod, item), { method: 'POST', body: file, headers });
    gotItem(saved);
    return saved;
  } catch (e) { if (base) throw e; cp.alert(e.message); }
  finally { box?.querySelector('[data-fbusy]')?.remove(); }
}

/** A record came back changed: keep it everywhere it's shown. */
function gotItem(saved) {
  const i = ITEMS.findIndex(x => x.id === saved.id);
  if (i >= 0) ITEMS[i] = saved;
  if (OPEN?.item?.id === saved.id) { OPEN.item = saved; drawFileBox(); $('dMeta').innerHTML = `<dt>Created</dt><dd>${esc(fmtWhen(saved.createdAt))} by ${esc(saved.createdBy)}</dd><dt>Last changed</dt><dd>${esc(fmtWhen(saved.updatedAt))} by ${esc(saved.updatedBy)}</dd>`; }
  if (ED?.item.id === saved.id) ED.item = saved;
  if ($('list') && OPEN_MOD()) drawList(OPEN_MOD());
}

/** After a new document is saved: upload the file picked for it. */
async function uploadPending(mod, item) {
  if (!PENDING || !item) return;
  const file = PENDING; PENDING = null;
  await upload(mod, item, file);
}

async function toggleVersions(mod, item, btn) {
  const box = document.querySelector('[data-fvlist]');
  if (!box.hidden) { box.hidden = true; btn.setAttribute('aria-expanded', 'false'); return; }
  box.hidden = false; btn.setAttribute('aria-expanded', 'true');
  box.innerHTML = '<div class="hint">Loading…</div>';
  try {
    const { versions, keep } = await api(fileUrl(mod, item, '/versions'));
    const latest = versions[0]?.version;
    box.innerHTML = `<table class="vtable"><tbody>${versions.map(v => `<tr>
        <td><b>v${v.version}</b>${v.version === latest ? ' <span class="chip blue">Current</span>' : ''}</td>
        <td>${esc(fmtWhen(v.createdAt))}<div class="muted">${esc(v.createdBy)}${v.note ? ' · ' + esc(v.note) : ''}</div></td>
        <td class="muted">${esc(sizeOf(v.size))}${v.name !== item.data.file?.name ? '<br>' + esc(v.name) : ''}</td>
        <td style="text-align:right;white-space:nowrap"><a class="btn small" href="api/${esc(fileUrl(mod, item))}?v=${v.version}">Download</a>
          ${canEdit() && v.version !== latest ? `<button type="button" class="btn small" data-restore="${v.version}">Restore</button>` : ''}</td></tr>`).join('')}</tbody></table>
      <div class="hint">The newest ${keep} versions are kept. Restoring makes a copy of that version the newest one, so nothing is lost.</div>`;
    box.querySelectorAll('[data-restore]').forEach(b => b.onclick = async () => {
      if (!(await cp.confirm({ title: `Restore version ${b.dataset.restore}?`, message: 'It becomes the newest version. The current one stays in Versions.', confirm: 'Restore' }))) return;
      try { gotItem(await post(fileUrl(mod, item, '/restore'), { version: Number(b.dataset.restore) })); } catch (e) { cp.alert(e.message); }
    });
  } catch (e) { box.innerHTML = `<div class="notice">${esc(e.message)}</div>`; }
}

/* ---------- The editor: a full window over the page ---------- */
document.body.insertAdjacentHTML('beforeend', `
<div class="ed" id="ed" role="dialog" aria-modal="true" aria-labelledby="edName" hidden>
  <div class="edhd"><span id="edBadge"></span><div class="edt"><b id="edName"></b></div>
    <div class="edtools" id="edTools"></div><span class="sp"></span>
    <span id="edState" class="muted" aria-live="polite"></span><a class="btn small" id="edDl">Download</a><button type="button" class="btn small primary" id="edSave">Save</button><button type="button" class="btn small" id="edClose">Close</button></div>
  <div id="edNote" class="notice" hidden></div>
  <div class="edbody" id="edBody"></div>
</div>`);

async function openFile(mod, item) {
  const f = item.data.file;
  GRIDSTATE = null;
  ED = { mod, item, kind: f.kind, ro: !canEdit() || !EDITS.has(f.kind), dirty: false, save: null, version: f.version };
  $('edBadge').innerHTML = badge(f.name); $('edName').textContent = f.name;
  $('edDl').href = 'api/' + fileUrl(mod, item);
  $('edTools').innerHTML = ''; $('edNote').hidden = true; $('edBody').innerHTML = '<div class="empty">Opening…</div>';
  $('ed').hidden = false; document.body.style.overflow = 'hidden';
  setState();
  try {
    if (f.kind === 'pdf') $('edBody').innerHTML = `<iframe class="edframe" title="${esc(f.name)}" src="api/${esc(fileUrl(mod, item))}?view=1&v=${f.version}"></iframe>`;
    else if (f.kind === 'image') $('edBody').innerHTML = `<div class="edimg"><img alt="${esc(f.name)}" src="api/${esc(fileUrl(mod, item))}?view=1&v=${f.version}"></div>`;
    else if (f.kind === 'text') await openText();
    else if (f.kind === 'docx') await openWord();
    else if (f.kind === 'xlsx') await openExcel();
  } catch (e) { $('edBody').innerHTML = `<div class="empty">${esc(e.message)}</div>`; ED.ro = true; }
  setState();
}

function setState(text) {
  if (!ED) return;
  $('edSave').hidden = ED.ro;
  $('edSave').disabled = !ED.dirty;
  $('edState').textContent = text || (ED.ro ? 'View only' : ED.dirty ? 'Unsaved changes' : `Version ${ED.version}`);
}
const changed = () => { if (ED && !ED.ro && !ED.dirty) { ED.dirty = true; setState(); } };
function note(msg, bad) { $('edNote').hidden = !msg; $('edNote').textContent = msg || ''; $('edNote').classList.toggle('ok', !bad); }

async function closeEditor() {
  if (ED?.dirty && !(await cp.confirm({ title: 'Discard your changes?', message: "You've changed this file but not saved it.", confirm: 'Discard', cancel: 'Keep editing', danger: true }))) return;
  ED = null; $('ed').hidden = true; $('edBody').innerHTML = ''; document.body.style.overflow = '';
}
async function saveEditor() {
  if (!ED?.save || !ED.dirty) return;
  $('edSave').disabled = true; setState('Saving…');
  note('');
  try { await ED.save(); ED.dirty = false; }
  catch (e) { note(e.message, true); }
  setState();
}
$('edClose').onclick = closeEditor;
$('edSave').onclick = saveEditor;
window.addEventListener('keydown', e => {
  if (!ED || document.querySelector('dialog[open]')) return;
  if (e.key === 'Escape') { e.preventDefault(); e.stopImmediatePropagation(); if (!gridEscape()) closeEditor(); }
  else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') { e.preventDefault(); saveEditor(); }
}, true);
window.addEventListener('beforeunload', e => { if (ED?.dirty) { e.preventDefault(); e.returnValue = ''; } });

async function raw(url) {
  const res = await fetch('api/' + url, { cache: 'no-store' });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return new Uint8Array(await res.arrayBuffer());
}

/* ---------- Text and CSV ---------- */
async function openText() {
  const { mod, item } = ED, name = item.data.file.name, e = extOf(name);
  const bytes = await raw(fileUrl(mod, item) + `?v=${ED.version}`);
  const bom = bytes[0] === 0xEF && bytes[1] === 0xBB && bytes[2] === 0xBF;
  let text = new TextDecoder('utf-8').decode(bom ? bytes.subarray(3) : bytes);
  const crlf = /\r\n/.test(text);
  const isCsv = e === 'csv' || e === 'tsv';
  const delim = e === 'tsv' ? '\t' : (text.split('\n')[0].split(';').length > text.split('\n')[0].split(',').length ? ';' : ',');
  let rows = isCsv ? parseCsv(text, delim) : null, mode = isCsv && rows.length <= 5000 ? 'grid' : 'text';
  const encode = () => {
    const out = mode === 'grid' ? toCsv(rows, delim, crlf ? '\r\n' : '\n') : $('edText').value;
    const b = new TextEncoder().encode(out);
    return bom ? new Blob([new Uint8Array([0xEF, 0xBB, 0xBF]), b]) : new Blob([b]);
  };
  ED.save = async () => {
    const saved = await upload(mod, item, encode(), ED.version, name);
    ED.version = saved.data.file.version;
    if (mode === 'text') text = $('edText').value;
  };
  const draw = () => {
    if (mode === 'grid') {
      $('edTools').innerHTML = (ED.ro ? '' : `<button type="button" class="btn small" data-g="row">+ Row</button><button type="button" class="btn small" data-g="col">+ Column</button>
        <button type="button" class="btn small" data-g="delrow">Delete row</button><button type="button" class="btn small" data-g="delcol">Delete column</button>`)
        + (isCsv ? '<button type="button" class="btn small" data-g="astext">Edit as text</button>' : '');
      grid($('edBody'), rows, { ro: ED.ro, key: 'csv', onEdit: (r, c, v) => { while (rows.length <= r) rows.push([]); while (rows[r].length <= c) rows[r].push(''); rows[r][c] = v; changed(); } });
      $('edTools').querySelectorAll('[data-g]').forEach(b => b.onclick = () => {
        const at = GRIDAT || { r: rows.length - 1, c: 0 }, S = GRIDSEL;
        const width = Math.max(1, ...rows.map(r => r.length));
        if (b.dataset.g === 'astext') { text = toCsv(rows, delim, crlf ? '\r\n' : '\n'); mode = 'text'; return draw(); }
        if (b.dataset.g === 'row') rows.splice(at.r + 1, 0, Array(width).fill(''));
        if (b.dataset.g === 'col') rows.forEach(r => { while (r.length < width) r.push(''); r.splice(at.c + 1, 0, ''); });
        // Delete row / Delete column take the selected rows or columns, or else the one with the cursor.
        const gone = b.dataset.g === 'delrow' ? (S?.kind === 'row' ? S.rows : [at.r]) : b.dataset.g === 'delcol' ? (S?.kind === 'col' ? S.cols : [at.c]) : [];
        if (b.dataset.g === 'delrow') { gone.filter(r => r < rows.length).sort((x, y) => y - x).forEach(r => rows.splice(r, 1)); if (!rows.length) rows.push(['']); }
        if (b.dataset.g === 'delcol') { rows.forEach(r => [...gone].sort((x, y) => y - x).forEach(c => r.splice(c, 1))); if (!rows.some(r => r.length)) rows[0] = ['']; }
        changed(); draw();
      });
    } else {
      $('edTools').innerHTML = isCsv && !ED.dirty ? '<button type="button" class="btn small" data-g="asgrid">Edit as a grid</button>' : '';
      $('edBody').innerHTML = `<textarea id="edText" class="edtext" spellcheck="false" ${ED.ro ? 'readonly' : ''}></textarea>`;
      $('edText').value = text;
      $('edText').oninput = changed;
      $('edTools').querySelector('[data-g="asgrid"]')?.addEventListener('click', () => { rows = parseCsv($('edText').value, delim); mode = 'grid'; draw(); });
      if (!ED.ro) $('edText').focus();
    }
  };
  draw();
}

function parseCsv(text, d) {
  const rows = []; let row = [], cell = '', q = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (q) {
      if (ch === '"') { if (text[i + 1] === '"') { cell += '"'; i++; } else q = false; }
      else cell += ch;
    } else if (ch === '"' && cell === '') q = true;
    else if (ch === d) { row.push(cell); cell = ''; }
    else if (ch === '\n' || ch === '\r') { if (ch === '\r' && text[i + 1] === '\n') i++; row.push(cell); rows.push(row); row = []; cell = ''; }
    else cell += ch;
  }
  if (cell !== '' || row.length) { row.push(cell); rows.push(row); }
  return rows.length ? rows : [['']];
}
function toCsv(rows, d, nl) {
  const width = Math.max(1, ...rows.map(r => r.length));
  const q = v => /[",\r\n;\t]|^\s|\s$/.test(v) || v.includes(d) ? `"${v.replace(/"/g, '""')}"` : v;
  // Trailing empty rows go; every row gets the same number of cells.
  const out = rows.map(r => Array.from({ length: width }, (_, i) => q(String(r[i] ?? ''))).join(d));
  while (out.length > 1 && /^[,;\t]*$/.test(out[out.length - 1])) out.pop();
  return out.join(nl) + nl;
}

/* ---------- The grid (CSV and Excel) ----------
 * Search (any cell) and a filter under each column hide the rows that don't match; the first row is the
 * header, kept in view. Click a row number or column letter to select the whole row or column (shift-click
 * or drag for several): Delete clears them, ⌘C copies them, and Delete row / Delete column remove them.
 */
let GRIDAT = null;     // the cell last focused: { r, c }
let GRIDSEL = null;    // whole rows or columns selected: { kind: 'row' | 'col', a, b, rows: [...], cols: [...] }
let GRIDSTATE = null;  // search and filters, kept while the same file (and sheet) is redrawn: { key, q, f: { col: text } }
let GRIDCTX = null;    // the grid on screen: { t, rows, onEdit, ro, header }
const colName = c => { let s = ''; c++; while (c) { const m = (c - 1) % 26; s = String.fromCharCode(65 + m) + s; c = Math.floor((c - 1) / 26); } return s; };

function grid(el, rows, { ro, onEdit, spare = 0, key = '', header = true }) {
  const width = Math.max(1, ...rows.map(r => r.length)) + spare, height = rows.length + spare;
  GRIDAT = null; GRIDSEL = null;
  if (GRIDSTATE?.key !== key) GRIDSTATE = { key, q: '', f: {} };
  const st = GRIDSTATE;
  let html = `<div class="gtop"><input type="search" class="gq" placeholder="Search" aria-label="Search the cells" value="${esc(st.q)}">
      <span class="gcount muted"></span><button type="button" class="btn small" data-gclear hidden>Clear filters</button><span class="sp"></span><span class="gsel muted"></span></div>
    <div class="gridwrap" tabindex="-1"><table class="sheet"><thead>
      <tr class="gl"><th class="rn corner" title="Select everything"></th>${Array.from({ length: width }, (_, c) => `<th data-col="${c}" title="Select column ${colName(c)}">${colName(c)}</th>`).join('')}</tr>
      <tr class="gf"><th class="rn"><svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M3 5h18l-7 8v6l-4 2v-8z"/></svg></th>${Array.from({ length: width }, (_, c) =>
        `<th><input data-f="${c}" placeholder="Filter" aria-label="Filter column ${colName(c)}${header && rows[0]?.[c] ? ' (' + esc(rows[0][c]) + ')' : ''}" value="${esc(st.f[c] || '')}"></th>`).join('')}</tr>
    </thead><tbody>`;
  for (let r = 0; r < height; r++) {
    html += `<tr data-row="${r}" class="${header && r === 0 ? 'hdr' : ''}"><th class="rn" data-rown="${r}" title="Select row ${r + 1}">${r + 1}</th>`;
    for (let c = 0; c < width; c++) html += `<td ${ro ? '' : 'contenteditable="plaintext-only"'} data-r="${r}" data-c="${c}" class="${/^=/.test(rows[r]?.[c] || '') ? 'fx' : ''}">${esc(rows[r]?.[c] ?? '')}</td>`;
    html += '</tr>';
  }
  el.innerHTML = html + '</tbody></table></div>';
  const t = el.querySelector('table'), wrap = el.querySelector('.gridwrap');
  t.style.setProperty('--hdr-top', t.tHead.offsetHeight + 'px');
  GRIDCTX = { t, rows, onEdit, ro, header };
  const cellAt = (r, c) => t.querySelector(`td[data-r="${r}"][data-c="${c}"]`);
  const go = (r, c) => { const n = cellAt(r, c); if (n) n.focus(); };

  // ----- Search and filters -----
  const apply = () => {
    const q = st.q.trim().toLowerCase(), fs = Object.entries(st.f).filter(([, v]) => v.trim()).map(([c, v]) => [+c, v.trim().toLowerCase()]);
    let shown = 0, total = 0;
    for (const tr of t.tBodies[0].rows) {
      const r = +tr.dataset.row;
      if (header && r === 0) continue;
      const row = rows[r] || [];
      const empty = !row.some(v => String(v ?? '').trim());
      if (!empty) total++;
      const ok = (!q || row.some(v => String(v ?? '').toLowerCase().includes(q))) && fs.every(([c, v]) => String(row[c] ?? '').toLowerCase().includes(v))
        && (!(q || fs.length) || !empty);
      tr.hidden = !ok;
      if (ok && !empty) shown++;
      if (q) for (const td of tr.cells) if (td.dataset.c) td.classList.toggle('hit', ok && String(row[+td.dataset.c] ?? '').toLowerCase().includes(q));
    }
    if (!q) t.querySelectorAll('td.hit').forEach(td => td.classList.remove('hit'));
    const filtering = q || fs.length;
    el.querySelector('.gcount').textContent = filtering ? `${shown} of ${total} rows` : `${total} row${total === 1 ? '' : 's'}`;
    el.querySelector('[data-gclear]').hidden = !filtering;
    t.querySelectorAll('[data-f]').forEach(i => i.classList.toggle('on', !!(st.f[i.dataset.f] || '').trim()));
    if (GRIDSEL) select(GRIDSEL.kind, GRIDSEL.a, GRIDSEL.b);
  };
  let timer = 0;
  const later = () => { clearTimeout(timer); timer = setTimeout(apply, 120); };
  el.querySelector('.gq').oninput = e => { st.q = e.target.value; later(); };
  t.querySelectorAll('[data-f]').forEach(i => i.oninput = () => { st.f[i.dataset.f] = i.value; later(); });
  el.querySelector('[data-gclear]').onclick = () => { st.q = ''; st.f = {}; el.querySelector('.gq').value = ''; t.querySelectorAll('[data-f]').forEach(i => i.value = ''); apply(); };
  apply();

  // ----- Whole rows and columns -----
  const visibleRows = (a, b) => [...t.tBodies[0].rows].filter(tr => !tr.hidden && +tr.dataset.row >= Math.min(a, b) && +tr.dataset.row <= Math.max(a, b)).map(tr => +tr.dataset.row);
  function select(kind, a, b) {
    t.querySelectorAll('.rsel, .csel').forEach(n => n.classList.remove('rsel', 'csel'));
    if (!kind) { GRIDSEL = null; el.querySelector('.gsel').textContent = ''; return; }
    const lo = Math.min(a, b), hi = Math.max(a, b);
    const rowsSel = kind === 'row' ? visibleRows(a, b) : [], cols = kind === 'col' ? Array.from({ length: hi - lo + 1 }, (_, i) => lo + i) : [];
    GRIDSEL = { kind, a, b, rows: rowsSel, cols };
    if (kind === 'row') rowsSel.forEach(r => t.querySelector(`tr[data-row="${r}"]`).classList.add('rsel'));
    else {
      cols.forEach(c => { t.querySelector(`th[data-col="${c}"]`)?.classList.add('csel'); t.querySelectorAll(`td[data-c="${c}"]`).forEach(td => td.classList.add('csel')); });
    }
    const n = kind === 'row' ? rowsSel.length : cols.length;
    el.querySelector('.gsel').textContent = `${n} ${kind === 'row' ? 'row' : 'column'}${n === 1 ? '' : 's'} selected · ${ro ? '⌘C copies' : 'Delete clears · ⌘C copies'}`;
  }
  let drag = null;
  t.addEventListener('mousedown', e => {
    const rn = e.target.closest('th[data-rown]'), cn = e.target.closest('th[data-col]'), corner = e.target.closest('th.corner');
    if (!rn && !cn && !corner) return;
    e.preventDefault();
    if (document.activeElement?.closest?.('td')) document.activeElement.blur();
    wrap.focus({ preventScroll: true });
    if (corner) return select('row', 0, height - 1);
    const kind = rn ? 'row' : 'col', i = +(rn ? rn.dataset.rown : cn.dataset.col);
    const a = e.shiftKey && GRIDSEL?.kind === kind ? GRIDSEL.a : i;
    select(kind, a, i);
    drag = { kind, a };
    window.addEventListener('mouseup', () => { drag = null; }, { once: true });
  });
  t.addEventListener('mouseover', e => {
    if (!drag) return;
    const n = e.target.closest(drag.kind === 'row' ? 'th[data-rown], td' : 'th[data-col], td');
    if (n) select(drag.kind, drag.a, +(drag.kind === 'row' ? (n.dataset.rown ?? n.dataset.r) : (n.dataset.col ?? n.dataset.c)));
  });
  const selectedText = () => {
    const S = GRIDSEL, out = [];
    const rs = S.kind === 'row' ? S.rows : [...t.tBodies[0].rows].filter(tr => !tr.hidden).map(tr => +tr.dataset.row).filter(r => r < rows.length);
    const cs = S.kind === 'col' ? S.cols : Array.from({ length: Math.max(1, ...rows.map(r => r.length)) }, (_, i) => i);
    for (const r of rs) out.push(cs.map(c => String(rows[r]?.[c] ?? '')).join('\t'));
    return out.join('\n');
  };
  wrap.addEventListener('keydown', e => {
    if (!GRIDSEL || e.target !== wrap) return;
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'c') { e.preventDefault(); navigator.clipboard?.writeText(selectedText()); el.querySelector('.gsel').textContent = 'Copied.'; }
    else if ((e.key === 'Delete' || e.key === 'Backspace') && !ro) {
      e.preventDefault();
      const S = GRIDSEL;
      const rs = S.kind === 'row' ? S.rows : [...t.tBodies[0].rows].filter(tr => !tr.hidden).map(tr => +tr.dataset.row);
      for (const r of rs) for (const td of t.querySelector(`tr[data-row="${r}"]`).cells) {
        if (!td.dataset.c || (S.kind === 'col' && !S.cols.includes(+td.dataset.c)) || !td.textContent) continue;
        td.textContent = ''; td.classList.remove('fx'); onEdit(r, +td.dataset.c, '');
      }
    }
  });
  t.addEventListener('focusin', e => { const td = e.target.closest('td'); if (td) { GRIDAT = { r: +td.dataset.r, c: +td.dataset.c }; if (GRIDSEL) select(null); } });
  if (ro) return;

  // ----- Typing in cells -----
  // Like a spreadsheet: going into a cell selects what's in it, so typing replaces it; click again to place the cursor.
  t.addEventListener('focusin', e => { const td = e.target.closest('td'); if (td) getSelection().selectAllChildren(td); });
  t.addEventListener('mousedown', e => { const td = e.target.closest('td'); if (td && document.activeElement !== td && e.button === 0) { e.preventDefault(); td.focus(); } });
  t.addEventListener('input', e => {
    const td = e.target.closest('td'); if (!td) return;
    onEdit(+td.dataset.r, +td.dataset.c, td.innerText.replace(/\n$/, '')); td.classList.toggle('fx', td.innerText.startsWith('='));
  });
  t.addEventListener('keydown', e => {
    const td = e.target.closest('td'); if (!td) return;
    const r = +td.dataset.r, c = +td.dataset.c;
    const step = d => { let tr = td.parentElement; do tr = d > 0 ? tr.nextElementSibling : tr.previousElementSibling; while (tr && tr.hidden); if (tr) go(+tr.dataset.row, c); };
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'a') { e.preventDefault(); getSelection().selectAllChildren(td); }
    else if (e.key === 'Enter' && !e.altKey) { e.preventDefault(); step(e.shiftKey ? -1 : 1); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); step(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); step(-1); }
  });
  // Pasting several cells (from Excel or Sheets: tabs and new lines) fills the grid from here.
  t.addEventListener('paste', e => {
    const td = e.target.closest('td'); if (!td) return;
    const text = e.clipboardData.getData('text/plain');
    if (!/[\t\n]/.test(text.replace(/\n$/, ''))) return;
    e.preventDefault();
    const r0 = +td.dataset.r, c0 = +td.dataset.c;
    text.replace(/\r/g, '').replace(/\n$/, '').split('\n').forEach((line, i) => line.split('\t').forEach((v, j) => {
      onEdit(r0 + i, c0 + j, v); const n = cellAt(r0 + i, c0 + j); if (n) n.textContent = v;
    }));
    if (!cellAt(r0 + text.split('\n').length - 1, c0)) grid(el, rows, { ro, onEdit, spare, key, header });   // grew: draw again
  });
}
/** Escape in the editor: first lets go of a row or column selection. */
function gridEscape() {
  if (!GRIDSEL || !GRIDCTX) return false;
  GRIDCTX.t.querySelectorAll('.rsel, .csel').forEach(n => n.classList.remove('rsel', 'csel'));
  GRIDSEL = null; GRIDCTX.t.closest('.gridwrap').parentElement.querySelector('.gsel').textContent = '';
  return true;
}

/* ---------- Excel ---------- */
async function openExcel() {
  const { mod, item } = ED;
  const book = await api(fileUrl(mod, item, '/xlsx'));
  ED.version = book.version;
  if (book.readOnly) { ED.ro = true; note(book.why, true); }
  let at = 0;
  const edits = new Map();   // "sheet|r|c" → value
  ED.save = async () => {
    const saved = await api(fileUrl(mod, item, '/xlsx'), { method: 'PUT', body: JSON.stringify({ version: ED.version,
      edits: [...edits].map(([k, v]) => { const [sheet, r, c] = k.split('|'); return { sheet, r: +r, c: +c, v }; }) }) });
    gotItem(saved); ED.version = saved.data.file.version; edits.clear();
    note('Saved. Formulas are worked out again when the workbook is next opened in Excel.');
  };
  const draw = () => {
    const s = book.sheets[at];
    $('edTools').innerHTML = ED.ro ? '' : '<button type="button" class="btn small" data-x="row">+ Row</button><button type="button" class="btn small" data-x="col">+ Column</button>';
    $('edBody').innerHTML = `<div id="xgrid" class="xgrid"></div><div class="tabs">${book.sheets.map((x, i) => `<button type="button" data-sheet="${i}" aria-pressed="${i === at}">${esc(x.name)}</button>`).join('')}
      ${s.truncated ? `<span class="muted">Showing the first ${s.rows.length} rows and ${Math.max(0, ...s.rows.map(r => r.length))} columns.</span>` : ''}</div>`;
    grid($('xgrid'), s.rows.length ? s.rows : [['']], { ro: ED.ro, spare: ED.ro ? 0 : 3, key: 'sheet:' + s.name, onEdit: (r, c, v) => {
      while (s.rows.length <= r) s.rows.push([]); while (s.rows[r].length <= c) s.rows[r].push('');
      s.rows[r][c] = v; edits.set(`${s.name}|${r}|${c}`, v); changed();
    } });
    $('edBody').querySelectorAll('[data-sheet]').forEach(b => b.onclick = () => { at = +b.dataset.sheet; draw(); });
    $('edTools').querySelectorAll('[data-x]').forEach(b => b.onclick = () => {
      if (b.dataset.x === 'row') s.rows.push(['']); else s.rows.forEach(r => r.push(''));
      if (!s.rows.length) s.rows.push(['']);
      draw();
    });
  };
  draw();
}

/* ---------- Word ---------- */
const STYLE_CLASS = s => /^title$/i.test(s) ? 'title' : /heading 1/i.test(s) ? 'h1' : /heading 2/i.test(s) ? 'h2' : /heading [3-9]/i.test(s) ? 'h3' : /^quote|intense quote/i.test(s) ? 'quote' : '';
const runsHtml = runs => (runs || []).map(r => { let h = esc(r.t).replace(/\n/g, '<br>').replace(/\t/g, '&emsp;'); if (r.u) h = `<u>${h}</u>`; if (r.i) h = `<i>${h}</i>`; if (r.b) h = `<b>${h}</b>`; return h; }).join('');

function runsOf(el) {
  const out = [];
  const push = (t, f) => { if (!t) return; const last = out[out.length - 1]; if (last && last.b === f.b && last.i === f.i && last.u === f.u) last.t += t; else out.push({ t, ...f }); };
  const walk = (n, f) => {
    for (const ch of n.childNodes) {
      if (ch.nodeType === 3) push(ch.nodeValue.replace(/ /g, '\t'), f);
      else if (ch.nodeName === 'BR') { if (ch.nextSibling || ch.parentNode !== el) push('\n', f); }
      else if (ch.nodeType === 1) {
        const st = ch.style || {}, tag = ch.nodeName;
        walk(ch, {
          b: f.b || tag === 'B' || tag === 'STRONG' || /bold|[6-9]00/.test(st.fontWeight || ''),
          i: f.i || tag === 'I' || tag === 'EM' || st.fontStyle === 'italic',
          u: f.u || tag === 'U' || /underline/.test(st.textDecoration || st.textDecorationLine || ''),
        });
        if (/^(DIV|P)$/.test(tag) && ch.nextSibling) push('\n', f);
      }
    }
  };
  walk(el, { b: false, i: false, u: false });
  if (out.length) out[out.length - 1].t = out[out.length - 1].t.replace(/\n$/, '');
  return out.filter(r => r.t);
}

async function openWord() {
  const { mod, item } = ED;
  const doc = await api(fileUrl(mod, item, '/docx'));
  ED.version = doc.version;
  const blockHtml = b => {
    if (b.kind === 'p') {
      const list = b.list || /^list/i.test(b.style);
      return `<div class="blk p ${STYLE_CLASS(b.style)} ${list ? 'list' : ''}" data-id="${b.id ?? ''}" ${b.like != null ? `data-like="${b.like}"` : ''} data-style="${esc(b.style)}" ${ED.ro ? '' : 'contenteditable="true"'}>${runsHtml(b.runs)}</div>`;
    }
    if (b.kind === 'table') return `<div class="blk tbl" data-id="${b.id ?? ''}"><table class="dtable"><tbody>${b.rows.map(r => `<tr>${r.map(c => `<td ${ED.ro ? '' : 'contenteditable="plaintext-only"'}>${esc(c)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
    return `<div class="blk locked" data-id="${b.id}" contenteditable="false" title="Kept exactly as it is in the document">
      <span>${esc(b.label)}${b.text ? ': ' + esc(b.text) : ''}</span>${ED.ro ? '' : '<button type="button" class="x" data-rm title="Remove from the document">×</button>'}</div>`;
  };
  $('edBody').innerHTML = `<div class="page" id="wpage">${doc.blocks.map(blockHtml).join('') || ''}</div>`;
  const page = $('wpage');
  if (!page.querySelector('.blk')) page.insertAdjacentHTML('beforeend', blockHtml({ kind: 'p', style: 'Normal', runs: [] }));
  if (!ED.ro) {
    $('edTools').innerHTML = `<select class="pick" id="wStyle" aria-label="Paragraph style">${doc.styles.map(s => `<option>${esc(s)}</option>`).join('')}</select>
      <button type="button" class="btn small fmt" data-cmd="bold" title="Bold (⌘B)"><b>B</b></button><button type="button" class="btn small fmt" data-cmd="italic" title="Italic (⌘I)"><i>I</i></button><button type="button" class="btn small fmt" data-cmd="underline" title="Underline (⌘U)"><u>U</u></button>
      <button type="button" class="btn small" data-w="row" title="Add a row under this one">+ Table row</button><button type="button" class="btn small" data-w="delrow">Delete table row</button>`;
    note('Pictures, page layout, headers and footers stay as they are. Only the paragraphs and table cells you change are rewritten.');
  }
  let cur = null;   // the block with the cursor
  const focusEnd = (el, start) => { el.focus(); const s = getSelection(), r = document.createRange(); r.selectNodeContents(el); r.collapse(!!start); s.removeAllRanges(); s.addRange(r); };
  const newBlock = (after, style, like, html = '') => {
    const d = document.createElement('div');
    d.className = `blk p ${STYLE_CLASS(style)} ${/^list/i.test(style) || after?.classList.contains('list') && style === after.dataset.style ? 'list' : ''}`;
    d.dataset.id = ''; d.dataset.style = style; if (like !== '' && like != null) d.dataset.like = like;
    d.contentEditable = 'true'; d.innerHTML = html;
    after ? after.after(d) : page.append(d);
    return d;
  };
  page.addEventListener('focusin', e => {
    cur = e.target.closest('.blk');
    if (cur?.classList.contains('p') && $('wStyle')) $('wStyle').value = cur.dataset.style;
  });
  if (ED.ro) return;
  page.addEventListener('input', changed);
  page.addEventListener('click', e => { const x = e.target.closest('[data-rm]'); if (x) { x.closest('.blk').remove(); changed(); } });
  // Pasted text comes in plain (no outside formatting); several lines become several paragraphs.
  page.addEventListener('paste', e => {
    const blk = e.target.closest('.blk.p'); if (!blk) return;
    e.preventDefault();
    const lines = e.clipboardData.getData('text/plain').replace(/\r/g, '').split('\n');
    document.execCommand('insertText', false, lines[0]);
    let at = blk;
    for (const line of lines.slice(1)) at = newBlock(at, blk.dataset.style, blk.dataset.id, esc(line));
    if (at !== blk) focusEnd(at);
    changed();
  });
  page.addEventListener('keydown', e => {
    const blk = e.target.closest('.blk.p');
    if (!blk) return;
    const s = getSelection(); if (!s.rangeCount) return;
    const r = s.getRangeAt(0);
    const atStart = () => { const t = document.createRange(); t.selectNodeContents(blk); t.setEnd(r.startContainer, r.startOffset); return !t.toString().length && r.collapsed; };
    const atEnd = () => { const t = document.createRange(); t.selectNodeContents(blk); t.setStart(r.endContainer, r.endOffset); return !t.toString().length && r.collapsed; };
    if (e.key === 'Enter' && !e.shiftKey) {
      // A new paragraph from the cursor on: a heading's next paragraph is Normal, anything else carries on its style.
      e.preventDefault();
      r.deleteContents();
      const tail = document.createRange(); tail.setStart(r.startContainer, r.startOffset); tail.setEnd(blk, blk.childNodes.length);
      const frag = tail.extractContents(), holder = document.createElement('div'); holder.append(frag);
      const heading = /^(h1|h2|h3|title)$/.test(STYLE_CLASS(blk.dataset.style));
      const style = heading ? (doc.styles.includes('Normal') ? 'Normal' : blk.dataset.style) : blk.dataset.style;
      const d = newBlock(blk, style, heading ? '' : (blk.dataset.id || blk.dataset.like || ''), holder.innerHTML);
      if (blk.classList.contains('list') && !heading) d.classList.add('list');
      focusEnd(d, true); changed();
    } else if (e.key === 'Backspace' && atStart()) {
      const prev = blk.previousElementSibling;
      if (prev?.classList.contains('p')) {
        e.preventDefault();
        const mark = prev.childNodes.length;
        while (blk.firstChild) prev.append(blk.firstChild);
        blk.remove(); prev.focus();
        const rr = document.createRange(); rr.setStart(prev, Math.min(mark, prev.childNodes.length)); rr.collapse(true); s.removeAllRanges(); s.addRange(rr);
        changed();
      } else if (!blk.textContent && blk.nextElementSibling) { e.preventDefault(); const n = blk.nextElementSibling; blk.remove(); if (n.classList.contains('p')) focusEnd(n, true); changed(); }
    } else if (e.key === 'Delete' && atEnd()) {
      const next = blk.nextElementSibling;
      if (next?.classList.contains('p')) { e.preventDefault(); while (next.firstChild) blk.append(next.firstChild); next.remove(); changed(); }
    } else if ((e.key === 'ArrowUp' || e.key === 'ArrowLeft') && atStart()) {
      const prev = blk.previousElementSibling?.closest('.blk.p'); if (prev && blk.previousElementSibling === prev) { e.preventDefault(); focusEnd(prev); }
    } else if ((e.key === 'ArrowDown' || e.key === 'ArrowRight') && atEnd()) {
      const next = blk.nextElementSibling; if (next?.classList.contains('p')) { e.preventDefault(); focusEnd(next, true); }
    }
  });
  $('wStyle').onchange = () => {
    if (!cur?.classList.contains('p')) return;
    const st = $('wStyle').value;
    cur.dataset.style = st;
    cur.className = `blk p ${STYLE_CLASS(st)} ${/^list/i.test(st) ? 'list' : ''}`;
    cur.focus(); changed();
  };
  $('edTools').querySelectorAll('[data-cmd]').forEach(b => { b.onmousedown = e => e.preventDefault(); b.onclick = () => { document.execCommand(b.dataset.cmd); changed(); }; });
  $('edTools').querySelectorAll('[data-w]').forEach(b => { b.onmousedown = e => e.preventDefault(); b.onclick = () => {
    const td = getSelection().anchorNode?.parentElement?.closest('td') || (getSelection().anchorNode?.nodeName === 'TD' ? getSelection().anchorNode : null);
    const tr = td?.closest('tr');
    if (!tr || !page.contains(tr)) return cp.alert('Put the cursor in a table first.');
    if (b.dataset.w === 'row') { const n = tr.cloneNode(true); n.querySelectorAll('td').forEach(c => c.textContent = ''); tr.after(n); n.querySelector('td').focus(); }
    else if (tr.parentElement.children.length > 1) tr.remove();
    changed();
  }; });

  ED.save = async () => {
    const blocks = [...page.querySelectorAll(':scope > .blk')].map(el => {
      const id = el.dataset.id === '' ? undefined : Number(el.dataset.id);
      if (el.classList.contains('p')) return { id, kind: 'p', style: el.dataset.style, runs: runsOf(el), like: el.dataset.like ? Number(el.dataset.like) : undefined };
      if (el.classList.contains('tbl')) return { id, kind: 'table', rows: [...el.querySelectorAll('tr')].map(tr => [...tr.children].map(td => td.innerText.replace(/\n$/, ''))) };
      return { id, kind: 'locked' };
    });
    const saved = await api(fileUrl(mod, item, '/docx'), { method: 'PUT', body: JSON.stringify({ version: ED.version, blocks }) });
    gotItem(saved);
    ED.dirty = false;
    const top = $('edBody').scrollTop;
    await openWord();   // the saved document, with its paragraphs numbered afresh
    $('edBody').scrollTop = top;
    note('Saved as version ' + ED.version + '.');
  };
}
