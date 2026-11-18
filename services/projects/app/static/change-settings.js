/* Change management settings: roles, the change flows (drag and drop), categories and risk levels.
 *
 * Served by the projects service at /changes/static/change-settings.js and shown in the portal's
 * Settings → Change management tab:  window.cpChangeSettings.mount(element)
 * Reads and saves /changes/api/settings (admins only).
 */
(() => {
  const API = '/changes/api/settings';
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const TYPES = {
    review: { label: 'Review', tip: 'People approve, decline or ask for changes', color: 'blue',
      icon: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>' },
    notify: { label: 'Notify', tip: 'Tells people, then moves straight on', color: 'violet',
      icon: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.9 1.9 0 0 0 3.4 0"/>' },
    schedule: { label: 'Schedule', tip: 'The window is confirmed', color: 'amber',
      icon: '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>' },
    implement: { label: 'Implement', tip: 'Started, then completed or failed', color: 'green',
      icon: '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.8-3.8a6 6 0 0 1-7.9 7.9l-6.9 6.9a2.1 2.1 0 0 1-3-3l6.9-6.9a6 6 0 0 1 7.9-7.9z"/>' },
    verify: { label: 'Verify', tip: 'Someone confirms it worked', color: 'teal',
      icon: '<path d="M22 11.1V12a10 10 0 1 1-5.9-9.1"/><path d="m22 4-10 10-3-3"/>' },
  };
  const svg = p => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${p}</svg>`;
  const css = `
  .cm h2 { font-size: 17px; font-weight: 600; margin: 0 0 4px; }
  .cm .sub { color: #3B404E; margin: 0 0 16px; }
  .cm .card { background: #FDFFF8; border: 1px solid #E1EAF9; border-radius: 14px; padding: 22px 24px; margin-bottom: 20px; }
  .cm label.lbl { display: block; font-weight: 600; font-size: 13px; margin: 14px 0 6px; }
  .cm .chips { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; min-height: 34px; padding: 4px; border: 1px solid #C1CFF7; border-radius: 8px; background: #fff; }
  .cm .chips input { border: 0; outline: none; flex: 1; min-width: 200px; height: 26px; font: inherit; }
  .cm .chipx { display: inline-flex; gap: 4px; align-items: center; background: #E1EAF9; color: #0F41F3; border-radius: 12px; padding: 2px 4px 2px 10px; font-size: 12.5px; font-weight: 600; }
  .cm .chipx button { border: 0; background: none; cursor: pointer; color: inherit; font-size: 14px; line-height: 1; padding: 0 4px; border-radius: 8px; }
  .cm .chipx button:hover { background: #C1CFF7; }
  .cm .radio { display: flex; gap: 18px; flex-wrap: wrap; margin-bottom: 8px; }
  .cm .radio label { display: flex; gap: 6px; align-items: center; cursor: pointer; }
  .cm .hint { font-size: 12.5px; color: #6B7080; margin-top: 4px; }
  .cm .palette { display: flex; gap: 8px; flex-wrap: wrap; position: sticky; top: 0; z-index: 5; background: #FDFFF8; padding: 10px 0 12px; border-bottom: 1px solid #E1EAF9; margin-bottom: 16px; }
  .cm .pchip { display: inline-flex; align-items: center; gap: 8px; padding: 7px 12px 7px 8px; border-radius: 10px; border: 1px dashed #C1CFF7; background: #fff; cursor: grab; user-select: none; font-weight: 600; }
  .cm .pchip:hover { border-color: #0F41F3; }
  .cm .pchip:active { cursor: grabbing; }
  .cm .pchip small { display: block; font-weight: 400; color: #6B7080; font-size: 11.5px; }
  .cm .ic { width: 28px; height: 28px; border-radius: 8px; display: grid; place-items: center; flex-shrink: 0; }
  .cm .ic svg { width: 16px; height: 16px; }
  .cm .blue .ic, .cm .ic.blue { background: #E1EAF9; color: #0F41F3; }
  .cm .violet .ic, .cm .ic.violet { background: #ECE8FD; color: #4A3AB0; }
  .cm .amber .ic, .cm .ic.amber { background: #FFF1D6; color: #8A5300; }
  .cm .green .ic, .cm .ic.green { background: #D6FAEE; color: #006B4A; }
  .cm .teal .ic, .cm .ic.teal { background: #DDF4F7; color: #0B6B78; }
  .cm .flow { border: 1px solid #E1EAF9; border-radius: 12px; padding: 16px; margin-bottom: 14px; background: #F7F9FE; }
  .cm .fhead { display: grid; grid-template-columns: 200px 1fr auto; gap: 10px; align-items: center; margin-bottom: 12px; }
  .cm .fhead input, .cm .step input, .cm .step select { height: 34px; border: 1px solid #C1CFF7; border-radius: 8px; padding: 0 10px; background: #fff; font: inherit; outline: none; min-width: 0; }
  .cm .fhead input.name { font-weight: 700; }
  .cm input:focus, .cm select:focus { border-color: #0F41F3; box-shadow: 0 0 0 3px rgba(15,65,243,.12); }
  .cm .lane { display: flex; flex-direction: column; gap: 0; min-height: 60px; border-radius: 10px; padding: 4px 0; }
  .cm .lane.over { background: rgba(15,65,243,.05); outline: 2px dashed #7D98F7; outline-offset: 2px; }
  .cm .lane .emptylane { color: #6B7080; text-align: center; padding: 16px; border: 1px dashed #C1CFF7; border-radius: 10px; }
  .cm .step { display: grid; grid-template-columns: 22px 28px 1fr auto; gap: 10px; align-items: start; background: #fff; border: 1px solid #E1EAF9; border-radius: 10px; padding: 10px 12px; position: relative; }
  .cm .step.dragging { opacity: .4; }
  .cm .step .grip { cursor: grab; color: #6B7080; padding-top: 6px; text-align: center; user-select: none; }
  .cm .step .grip:active { cursor: grabbing; }
  .cm .step .body { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 8px; }
  .cm .step .body > * { grid-column: span 2; }
  .cm .step .body .wide { grid-column: span 3; }
  .cm .step .body .full { grid-column: span 6; }
  .cm .step .kind { font-size: 11px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; color: #3B404E; grid-column: span 6; margin-bottom: -4px; }
  .cm .step .tools { display: flex; flex-direction: column; gap: 2px; }
  .cm .step .tools button { border: 0; background: none; cursor: pointer; color: #6B7080; width: 26px; height: 24px; border-radius: 6px; font-size: 14px; }
  .cm .step .tools button:hover { background: #E1EAF9; color: #0F41F3; }
  .cm .step .tools button.del:hover { background: #FDECEC; color: #B42318; }
  .cm .arrow { text-align: center; color: #7D98F7; height: 18px; line-height: 18px; font-size: 14px; }
  .cm .drop { height: 4px; border-radius: 2px; background: #0F41F3; margin: 4px 0; }
  .cm .btn { height: 34px; border-radius: 8px; padding: 0 12px; border: 1px solid #C1CFF7; background: #fff; cursor: pointer; font: inherit; }
  .cm .btn:hover { background: #E1EAF9; }
  .cm .btn.primary { background: #0F41F3; border-color: #0F41F3; color: #FDFFF8; font-weight: 600; }
  .cm .btn.primary:hover { background: #0B34C8; }
  .cm .btn.danger { color: #B42318; border-color: #F7B4B4; }
  .cm .savebar { position: sticky; bottom: 0; background: #FDFFF8; border: 1px solid #E1EAF9; border-radius: 12px; padding: 12px 16px; display: flex; gap: 12px; align-items: center; box-shadow: 0 -6px 20px rgba(4,14,39,.06); }
  .cm .savebar .msg { color: #3B404E; }
  .cm .savebar .msg.dirty { color: #8A5300; font-weight: 600; }
  .cm .two { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
  .cm textarea { width: 100%; min-height: 150px; border: 1px solid #C1CFF7; border-radius: 8px; padding: 8px 10px; font: inherit; background: #fff; outline: none; resize: vertical; }
  @media (max-width: 800px) { .cm .fhead { grid-template-columns: 1fr; } .cm .step .body > *, .cm .step .body .wide { grid-column: span 6; } .cm .two { grid-template-columns: 1fr; } }`;

  let S = null, ROOT = null, PEOPLE = [], DIRTY = false, DRAG = null;

  async function api(method, body) {
    const r = await fetch(API, { method, cache: 'no-store', headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'nscale-admin' },
      body: body ? JSON.stringify(body) : undefined });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(Array.isArray(d.detail) ? d.detail.map(x => x.msg).join('; ') : (d.detail || r.statusText));
    return d;
  }
  const dirty = () => { DIRTY = true; const m = ROOT.querySelector('#cmMsg'); if (m) { m.textContent = 'You have unsaved changes.'; m.className = 'msg dirty'; } };
  const nameOf = e => (PEOPLE.find(p => p.email === e) || {}).name || e;

  /* ---------- Rendering ---------- */
  function chipsBox(id, list) {
    return `<div class="chips" data-chips="${id}">${list.map(e => `<span class="chipx" title="${esc(e)}">${esc(nameOf(e))}<button type="button" data-rm="${esc(e)}" aria-label="Remove ${esc(e)}">×</button></span>`).join('')}
      <input list="cmPeople" placeholder="Add someone by name or email, then Enter" aria-label="Add person"></div>`;
  }

  function stepCard(f, i, st) {
    const t = TYPES[st.type];
    const who = `<select data-k="who" aria-label="Who">${[['reviewers', 'Change reviewers'], ['requestor', 'The requestor'], ['people', 'Named people']]
      .map(([v, l]) => `<option value="${v}" ${st.who === v ? 'selected' : ''}>${l}</option>`).join('')}</select>`;
    const people = st.who === 'people' ? `<input class="full" data-k="people" value="${esc((st.people || []).join(', '))}" placeholder="Emails, separated by commas" aria-label="People" list="cmPeople">` : '';
    const rule = ['review', 'verify'].includes(st.type) ? `<select data-k="rule" aria-label="How many must approve">${[['any', 'Any one approves'], ['count', 'A number approve'], ['all', 'Everyone approves']]
      .map(([v, l]) => `<option value="${v}" ${(st.rule || 'any') === v ? 'selected' : ''}>${l}</option>`).join('')}</select>${st.rule === 'count'
      ? `<input type="number" min="1" max="20" data-k="count" value="${esc(st.count || 2)}" aria-label="How many">` : ''}` : '';
    const msg = st.type === 'notify' ? `<input class="full" data-k="message" value="${esc(st.message || '')}" placeholder="Message (optional): defaults to the change summary" aria-label="Message">` : '';
    const whoLabel = { review: 'Reviewed by', verify: 'Checked by', notify: 'Tells', schedule: 'Scheduled by', implement: 'Done by' }[st.type];
    return `<div class="step ${t.color}" data-flow="${f}" data-i="${i}">
      <span class="grip" title="Drag to reorder" aria-hidden="true">⋮⋮</span><span class="ic">${svg(t.icon)}</span>
      <div class="body"><div class="kind">${i + 1}. ${t.label} · <span style="font-weight:400;text-transform:none;letter-spacing:0">${whoLabel}</span></div>
        <input class="wide" data-k="name" value="${esc(st.name)}" placeholder="Step name" aria-label="Step name">${who}${rule}${people}${msg}</div>
      <div class="tools"><button type="button" data-mv="-1" title="Move up" aria-label="Move up">↑</button><button type="button" data-mv="1" title="Move down" aria-label="Move down">↓</button>
        <button type="button" class="del" data-del title="Remove step" aria-label="Remove step">×</button></div></div>`;
  }

  function draw() {
    const req = S.roles.requestors;
    ROOT.innerHTML = `<div class="cm">
      <datalist id="cmPeople">${PEOPLE.map(p => `<option value="${esc(p.email)}">${esc(p.name)}</option>`).join('')}</datalist>
      <section class="card"><h2>Roles</h2><p class="sub">Who raises changes, and who reviews them. People also need the Change management tool under People.</p>
        <label class="lbl">Change requestors <span style="font-weight:400;color:#6B7080">· fill changes out</span></label>
        <div class="radio"><label><input type="radio" name="cmReq" value="everyone" ${req === 'everyone' ? 'checked' : ''}> Everyone with Change management</label>
          <label><input type="radio" name="cmReq" value="list" ${req !== 'everyone' ? 'checked' : ''}> Only these people</label></div>
        ${req !== 'everyone' ? chipsBox('requestors', req) : ''}
        <label class="lbl">Change reviewers <span style="font-weight:400;color:#6B7080">· approve or decline, in steps set to “Change reviewers”</span></label>
        ${chipsBox('reviewers', S.roles.reviewers)}
        <div class="hint">Nobody reviews their own change. With no reviewers, admins can approve.</div></section>
      <section class="card"><h2>Change flows</h2><p class="sub">One flow per change type. Drag a step from the palette into a flow, drag steps to reorder them, or use the arrows. Changes already under way keep the flow they started with.</p>
        <div class="palette" aria-label="Steps to drag into a flow">${Object.entries(TYPES).map(([k, t]) => `<span class="pchip ${t.color}" draggable="true" data-new="${k}" tabindex="0" role="button" title="Drag into a flow, or click to add to the last one">
          <span class="ic">${svg(t.icon)}</span><span>${t.label}<small>${t.tip}</small></span></span>`).join('')}</div>
        ${S.flows.map((f, fi) => `<div class="flow" data-flowbox="${fi}">
          <div class="fhead"><input class="name" data-fk="name" data-fi="${fi}" value="${esc(f.name)}" placeholder="Change type, e.g. Normal" aria-label="Flow name">
            <input data-fk="description" data-fi="${fi}" value="${esc(f.description)}" placeholder="When to use it" aria-label="Flow description">
            <button type="button" class="btn danger" data-delflow="${fi}">Delete flow</button></div>
          <div class="lane" data-lane="${fi}">${f.steps.length ? f.steps.map((s, i) => (i ? '<div class="arrow" aria-hidden="true">↓</div>' : '') + stepCard(fi, i, s)).join('')
            : '<div class="emptylane">Drag steps here</div>'}</div></div>`).join('')}
        <button type="button" class="btn" id="cmAddFlow">Add a flow</button></section>
      <section class="card"><h2>Form choices</h2><p class="sub">One per line.</p>
        <div class="two"><div><label class="lbl" for="cmCats">Categories</label><textarea id="cmCats">${esc(S.categories.join('\n'))}</textarea></div>
          <div><label class="lbl" for="cmRisks">Risk levels</label><textarea id="cmRisks">${esc(S.risks.join('\n'))}</textarea></div></div></section>
      <div class="savebar"><button type="button" class="btn primary" id="cmSave">Save change management settings</button><span id="cmMsg" class="msg">${DIRTY ? 'You have unsaved changes.' : ''}</span></div>
    </div>`;
    if (DIRTY) ROOT.querySelector('#cmMsg').className = 'msg dirty';
    wire();
  }

  /* ---------- Behaviour ---------- */
  function wire() {
    ROOT.querySelectorAll('input[name=cmReq]').forEach(r => r.onchange = () => { S.roles.requestors = r.value === 'everyone' ? 'everyone' : []; dirty(); draw(); });
    ROOT.querySelectorAll('[data-chips]').forEach(box => {
      const key = box.dataset.chips, list = () => key === 'reviewers' ? S.roles.reviewers : S.roles.requestors;
      const inp = box.querySelector('input');
      const add = () => {
        const v = inp.value.trim().toLowerCase(), p = PEOPLE.find(x => x.email === v || x.name.toLowerCase() === v);
        const email = p ? p.email : v;
        if (!email) return;
        if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) { cp.alert({ title: 'Not an email address', message: `Pick someone from the list, or type their email.` }); return; }
        if (!list().includes(email)) list().push(email);
        dirty(); draw(); ROOT.querySelector(`[data-chips="${key}"] input`).focus();
      };
      inp.onkeydown = e => { if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); add(); } };
      inp.onchange = () => { if (PEOPLE.some(p => p.email === inp.value.trim().toLowerCase())) add(); };
      box.querySelectorAll('[data-rm]').forEach(b => b.onclick = () => { const l = list(); l.splice(l.indexOf(b.dataset.rm), 1); dirty(); draw(); });
    });
    ROOT.querySelectorAll('[data-fk]').forEach(i => i.oninput = () => { S.flows[i.dataset.fi][i.dataset.fk] = i.value; dirty(); });
    ROOT.querySelectorAll('[data-delflow]').forEach(b => b.onclick = async () => {
      const f = S.flows[b.dataset.delflow];
      if (S.flows.length === 1) { cp.alert({ title: 'Keep one flow', message: 'There has to be at least one change type.' }); return; }
      if (!(await cp.confirm({ title: `Delete the ${f.name || 'unnamed'} flow?`, message: 'Changes already using it carry on as they are. It goes when you save.', confirm: 'Delete flow', danger: true }))) return;
      S.flows.splice(b.dataset.delflow, 1); dirty(); draw();
    });
    ROOT.querySelector('#cmAddFlow').onclick = () => {
      S.flows.push({ id: '', name: '', description: '', steps: [{ type: 'review', name: 'Review', who: 'reviewers', people: [], rule: 'any' }] });
      dirty(); draw(); ROOT.querySelectorAll('.fhead input.name')[S.flows.length - 1].focus();
    };
    // Step fields
    ROOT.querySelectorAll('.step').forEach(card => {
      const st = S.flows[card.dataset.flow].steps[card.dataset.i];
      card.querySelectorAll('[data-k]').forEach(inp => {
        inp.oninput = inp.onchange = () => {
          const k = inp.dataset.k;
          st[k] = k === 'people' ? inp.value.split(',').map(s => s.trim()).filter(Boolean) : k === 'count' ? Number(inp.value) || 1 : inp.value;
          dirty();
          if (['who', 'rule'].includes(k) && inp.tagName === 'SELECT') draw();
        };
      });
      card.querySelector('[data-del]').onclick = () => { S.flows[card.dataset.flow].steps.splice(card.dataset.i, 1); dirty(); draw(); };
      card.querySelectorAll('[data-mv]').forEach(b => b.onclick = () => {
        const steps = S.flows[card.dataset.flow].steps, i = Number(card.dataset.i), j = i + Number(b.dataset.mv);
        if (j < 0 || j >= steps.length) return;
        [steps[i], steps[j]] = [steps[j], steps[i]]; dirty(); draw();
      });
      // Drag a step by its grip or icon, never while typing in it.
      card.addEventListener('mousedown', e => { card.draggable = !e.target.closest('input,select,button'); });
      card.addEventListener('dragstart', e => {
        DRAG = { from: 'step', flow: Number(card.dataset.flow), i: Number(card.dataset.i) };
        e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', 'step');
        requestAnimationFrame(() => card.classList.add('dragging'));
      });
      card.addEventListener('dragend', () => { card.classList.remove('dragging'); card.draggable = false; clearDrop(); DRAG = null; });
    });
    // Palette
    ROOT.querySelectorAll('[data-new]').forEach(p => {
      p.addEventListener('dragstart', e => { DRAG = { from: 'palette', type: p.dataset.new }; e.dataTransfer.effectAllowed = 'copy'; e.dataTransfer.setData('text/plain', p.dataset.new); });
      p.addEventListener('dragend', () => { clearDrop(); DRAG = null; });
      const addToLast = () => { const f = S.flows[S.flows.length - 1]; f.steps.push(newStep(p.dataset.new)); dirty(); draw(); };
      p.onclick = addToLast; p.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); addToLast(); } };
    });
    // Lanes
    ROOT.querySelectorAll('[data-lane]').forEach(lane => {
      lane.addEventListener('dragover', e => {
        if (!DRAG) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = DRAG.from === 'palette' ? 'copy' : 'move';
        lane.classList.add('over');
        showDrop(lane, indexAt(lane, e.clientY));
      });
      lane.addEventListener('dragleave', e => { if (!lane.contains(e.relatedTarget)) { lane.classList.remove('over'); clearDrop(); } });
      lane.addEventListener('drop', e => {
        e.preventDefault();
        if (!DRAG) return;
        const fi = Number(lane.dataset.lane), at = indexAt(lane, e.clientY), steps = S.flows[fi].steps;
        if (DRAG.from === 'palette') steps.splice(at, 0, newStep(DRAG.type));
        else {
          const [moved] = S.flows[DRAG.flow].steps.splice(DRAG.i, 1);
          const to = DRAG.flow === fi && DRAG.i < at ? at - 1 : at;
          steps.splice(to, 0, moved);
        }
        DRAG = null; dirty(); draw();
      });
    });
    ROOT.querySelector('#cmSave').onclick = save;
  }

  function newStep(type) {
    const names = { review: 'Review', notify: 'Tell the reviewers', schedule: 'Schedule', implement: 'Implement', verify: 'Check it worked' };
    return { type, name: names[type], who: ['schedule', 'implement'].includes(type) ? 'requestor' : 'reviewers', people: [],
      ...(['review', 'verify'].includes(type) ? { rule: 'any' } : {}), ...(type === 'notify' ? { message: '' } : {}) };
  }
  function indexAt(lane, y) {
    const cards = [...lane.querySelectorAll('.step')];
    for (let i = 0; i < cards.length; i++) { const r = cards[i].getBoundingClientRect(); if (y < r.top + r.height / 2) return i; }
    return cards.length;
  }
  function clearDrop() { ROOT.querySelectorAll('.drop').forEach(d => d.remove()); ROOT.querySelectorAll('.lane.over').forEach(l => l.classList.remove('over')); }
  function showDrop(lane, at) {
    ROOT.querySelectorAll('.drop').forEach(d => d.remove());
    const cards = [...lane.querySelectorAll('.step')], line = document.createElement('div');
    line.className = 'drop';
    if (at < cards.length) cards[at].before(line); else lane.append(line);
  }

  async function save() {
    const btn = ROOT.querySelector('#cmSave'), msg = ROOT.querySelector('#cmMsg');
    const lines = id => ROOT.querySelector(id).value.split('\n').map(s => s.trim()).filter(Boolean);
    btn.disabled = true;
    try {
      S = { ...S, ...(await api('PUT', { roles: S.roles, flows: S.flows, categories: lines('#cmCats'), risks: lines('#cmRisks') })) };
      DIRTY = false; draw();
      ROOT.querySelector('#cmMsg').textContent = 'Saved.'; ROOT.querySelector('#cmMsg').className = 'msg';
    } catch (e) { cp.alert({ title: "Couldn't save", message: e.message, error: true }); }
    finally { const b = ROOT.querySelector('#cmSave'); if (b) b.disabled = false; }
  }

  async function mount(el) {
    ROOT = el;
    if (!document.getElementById('cm-css')) { const st = document.createElement('style'); st.id = 'cm-css'; st.textContent = css; document.head.append(st); }
    el.innerHTML = '<div class="cm"><div class="card">Loading…</div></div>';
    try {
      const [s, people] = await Promise.all([api('GET'), fetch('/api/people', { cache: 'no-store' }).then(r => r.ok ? r.json() : { people: [] }).catch(() => ({ people: [] }))]);
      S = s; PEOPLE = people.people || []; DIRTY = false; draw();
    } catch (e) {
      el.innerHTML = `<div class="cm"><div class="card"><h2>Change management</h2><p class="sub">${esc(e.message)}</p></div></div>`;
    }
  }
  window.addEventListener('beforeunload', e => { if (DIRTY) { e.preventDefault(); e.returnValue = ''; } });
  window.cpChangeSettings = { mount, isDirty: () => DIRTY };
})();
