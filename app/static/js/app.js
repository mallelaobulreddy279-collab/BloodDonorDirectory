(() => {
'use strict';

/* ------------------------------------------------------------ helpers */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const main = $('#main');
const state = { user: null, meta: null, unread: 0, search: { group: '', cityId: null, sectorId: null, compatible: false } };

async function api(path, { method = 'GET', body } = {}) {
  const res = await fetch('/api' + path, {
    method, credentials: 'same-origin',
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const e = new Error((data && data.error) || 'Something went wrong. Please try again.');
    e.status = res.status; e.field = data && data.field;
    throw e;
  }
  return data;
}

let toastTimer;
function toast(msg, kind = '') {
  const t = $('#toast');
  t.textContent = msg; t.className = 'show ' + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = ''; }, 3200);
}

const parseUtc = s => new Date(String(s).replace(' ', 'T') + 'Z');
function timeAgo(s) {
  const mins = Math.max(0, Math.round((Date.now() - parseUtc(s)) / 60000));
  if (mins < 2) return 'just now';
  if (mins < 60) return `${mins} min ago`;
  const h = Math.round(mins / 60);
  if (h < 24) return `${h} hour${h === 1 ? '' : 's'} ago`;
  const d = Math.round(h / 24);
  return `${d} day${d === 1 ? '' : 's'} ago`;
}
const fmtDate = iso => new Date(iso + 'T00:00:00').toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' });
const fmtPhone = n => `${n.slice(0, 5)} ${n.slice(5)}`;
function daysAgoText(n) {
  if (n < 1) return 'today';
  if (n < 60) return `${n} day${n === 1 ? '' : 's'} ago`;
  const m = Math.floor(n / 30);
  return `${m} months ago`;
}
const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;

/* ---- forms */
function formValues(form) {
  const o = {};
  $$('[name]', form).forEach(el => {
    if (el.type === 'checkbox') o[el.name] = el.checked;
    else if (el.type === 'radio') { if (el.checked) o[el.name] = el.value; }
    else o[el.name] = el.value;
  });
  return o;
}
function clearErrors(form) {
  $$('.err, .form-error', form).forEach(e => e.remove());
  $$('[aria-invalid]', form).forEach(e => e.removeAttribute('aria-invalid'));
}
function showError(form, err) {
  clearErrors(form);
  const field = err.field && $(`[name="${err.field}"]`, form);
  if (field) {
    field.setAttribute('aria-invalid', 'true');
    const p = document.createElement('span');
    p.className = 'err'; p.id = 'err-' + err.field; p.textContent = err.message;
    field.setAttribute('aria-describedby', p.id);
    field.closest('.field').appendChild(p);
    field.focus();
  } else {
    const p = document.createElement('div');
    p.className = 'form-error full'; p.setAttribute('role', 'alert'); p.textContent = err.message;
    form.prepend(p);
  }
}
function onSubmit(form, handler) {
  form.addEventListener('submit', async ev => {
    ev.preventDefault();
    const btn = $('button[type=submit]', form);
    clearErrors(form);
    if (btn) btn.disabled = true;
    try { await handler(formValues(form), form); }
    catch (err) { showError(form, err); }
    finally { if (btn) btn.disabled = false; }
  });
}

/* ---- city -> sector nested dropdown (SRS F2) */
const cityOptions = sel => state.meta.cities.map(c => `<option value="${c.city_id}" ${c.city_id == sel ? 'selected' : ''}>${esc(c.city_name)}</option>`).join('');
const sectorOptions = (cityId, sel) => {
  const city = state.meta.cities.find(c => c.city_id == cityId) || state.meta.cities[0];
  return `<option value="">Choose a sector</option>` + city.sectors.map(s => `<option value="${s.sector_id}" ${s.sector_id == sel ? 'selected' : ''}>${esc(s.sector_name)}</option>`).join('');
};
function cityOfSector(sectorId) {
  const c = state.meta.cities.find(c => c.sectors.some(s => s.sector_id == sectorId));
  return c ? c.city_id : state.meta.cities[0].city_id;
}
function locationFields(cityId, sectorId, label = 'Sector') {
  return `
    <div class="field"><label for="f-city">City</label>
      <select id="f-city" name="city_id">${cityOptions(cityId)}</select></div>
    <div class="field"><label for="f-sector">${label}</label>
      <select id="f-sector" name="sector_id">${sectorOptions(cityId, sectorId)}</select></div>`;
}
function bindNested(root) {
  const city = $('[name=city_id]', root), sector = $('[name=sector_id]', root);
  if (city && sector) city.addEventListener('change', () => { sector.innerHTML = sectorOptions(city.value); });
}

/* ---------------------------------------------------------- shared UI */
function contactAction(d) {
  const u = state.user;
  const masked = `<span class="masked" title="Hidden until you sign in as a recipient">${esc(d.masked_contact)}</span>`;
  if (!u) return `${masked}<a class="btn small" href="#/login">Sign in to view</a>`;
  if (u.role === 'donor') return `${masked}<span class="meta">Recipients only</span>`;
  return `${masked}<button class="btn small primary" data-reveal="${d.donor_id}">Show number</button>`;
}
function donorRow(d, requested) {
  const last = d.last_donation_date ? `Last donated ${fmtDate(d.last_donation_date)} (${daysAgoText(d.days_since)})` : 'First-time donor';
  const compat = requested && d.blood_group !== requested ? `<span class="meta">Compatible with ${esc(requested)} patients</span>` : '';
  return `<li class="donor-row">
    <span class="bg-badge" role="img" aria-label="Blood group ${esc(d.blood_group)}">${esc(d.blood_group)}</span>
    <div class="donor-main"><strong class="donor-name">${esc(d.display_name)}</strong>
      <span class="meta">${esc(d.sector)}, ${esc(d.city)}</span><span class="meta">${last}</span>${compat}</div>
    <span class="chip ${d.status === 'Available' ? 'ok' : 'gray'}">${esc(d.status)}</span>
    <div class="donor-action">${contactAction(d)}</div></li>`;
}
const urgencyChip = u => `<span class="chip ${u === 'Critical' ? 'crit' : u === 'Urgent' ? 'warn' : 'info'}">${esc(u)}</span>`;

function requestItem(r, { mine = false } = {}) {
  const u = state.user;
  let action = '';
  if (!mine) {
    if (u && u.role === 'donor') {
      const fits = state.meta.compat[r.blood_group].includes(u.blood_group);
      action = r.responded ? `<button class="btn small" disabled>You responded</button>`
        : !fits ? `<button class="btn small" disabled title="${esc(u.blood_group)} blood can't be given to a ${esc(r.blood_group)} patient">Not your blood group</button>`
        : !u.can_donate ? `<button class="btn small" disabled title="Check your eligibility and availability on your profile">Not available now</button>`
        : `<button class="btn small primary" data-respond="${r.request_id}">I can donate</button>`;
    } else if (!u) action = `<a class="btn small" href="#/login">Sign in to respond</a>`;
  }
  return `<div class="req" data-request="${r.request_id}">
    <span class="bg-badge" role="img" aria-label="Blood group ${esc(r.blood_group)}">${esc(r.blood_group)}</span>
    <div><div class="req-title"><strong>${plural(r.units, 'unit')} needed</strong> ${urgencyChip(r.urgency)}
        ${r.status === 'Verified' ? '<span class="chip ok">Verified</span>' : r.status === 'Pending' ? '<span class="chip gray">Awaiting verification</span>' : `<span class="chip gray">${esc(r.status)}</span>`}</div>
      <div class="meta">${esc(r.hospital)} &middot; ${esc(r.sector)}, ${esc(r.city)}</div>
      <div class="meta">Posted ${timeAgo(r.created_at)}${mine && r.patient_name ? ` &middot; Patient: ${esc(r.patient_name)}` : ''} &middot; ${plural(r.responders, 'donor')} responded</div></div>
    <div class="req-actions">${action}</div>
    ${mine ? `<div class="sub" data-responders></div>` : ''}</div>`;
}

/* delegated actions: reveal number, respond to request */
document.addEventListener('click', async e => {
  const reveal = e.target.closest('[data-reveal]');
  if (reveal) {
    reveal.disabled = true;
    try {
      const d = await api(`/donors/${reveal.dataset.reveal}/contact`, { method: 'POST', body: {} });
      const row = reveal.closest('.donor-row');
      $('.donor-name', row).textContent = d.full_name;
      reveal.closest('.donor-action').innerHTML = `<a class="phone" href="tel:+91${d.contact_no}">${fmtPhone(d.contact_no)}</a>`;
    } catch (err) { toast(err.message, 'bad'); reveal.disabled = false; }
    return;
  }
  const respond = e.target.closest('[data-respond]');
  if (respond) {
    respond.disabled = true;
    try {
      await api(`/requests/${respond.dataset.respond}/respond`, { method: 'POST', body: {} });
      toast('Thank you. The requester has been told you can donate.');
      respond.textContent = 'You responded';
    } catch (err) { toast(err.message, 'bad'); respond.disabled = false; }
  }
});

/* --------------------------------------------------------- nav / auth */
const bellIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg>';
function renderChrome() {
  const u = state.user;
  const links = [['', 'Find donors'], ['requests', 'Live requests']];
  if (!u) links.push(['register', 'Become a donor']);
  else if (u.role === 'admin') links.push(['admin', 'Admin']);
  else links.push(['account', u.role === 'donor' ? 'My donor profile' : 'My requests']);
  $('#nav').innerHTML = links.map(([k, t]) => `<a href="#/${k}" data-key="${k}">${t}</a>`).join('');
  $('#authSlot').innerHTML = u
    ? `${u.role !== 'admin' ? `<a class="bell" href="#/account" aria-label="Alerts${state.unread ? `, ${state.unread} unread` : ''}">${bellIcon}${state.unread ? `<span class="count">${state.unread}</span>` : ''}</a>` : ''}
       <span class="whoami">${esc(u.full_name)}</span><button class="btn small" id="logout">Sign out</button>`
    : `<a class="btn small" href="#/login">Sign in</a><a class="btn small primary" href="#/register">Register</a>`;
  const lo = $('#logout');
  if (lo) lo.onclick = async () => { await api('/logout', { method: 'POST', body: {} }); state.user = null; state.unread = 0; renderChrome(); location.hash = '#/'; toast('You have been signed out.'); };
  markNav();
}
function markNav() {
  const key = routeKey();
  $$('#nav a').forEach(a => { if (a.dataset.key === key) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current'); });
}
async function refreshMe() { try { state.user = (await api('/me')).user; renderChrome(); } catch { /* ignore */ } }
async function refreshUnread() {
  if (!state.user || state.user.role === 'admin') return;
  try { const n = await api('/notifications'); if (n.unread !== state.unread) { state.unread = n.unread; renderChrome(); } } catch { /* ignore */ }
}

/* -------------------------------------------------------------- pages */
async function pageHome() {
  const s = state.search, m = state.meta;
  if (!s.cityId) s.cityId = (m.cities.find(c => c.city_name === 'Madanapalle') || m.cities[0]).city_id;
  main.innerHTML = `
  <section class="hero"><div class="wrap">
    <h1>Find an eligible blood donor in the sector where you need one.</h1>
    <p class="lede">Donors are listed by city sector, so the person you call is minutes from the hospital, not across town. Anyone who gave blood in the last ${m.eligibility_days} days is hidden automatically.</p>
    <p class="hero-meta"><strong>${m.totals.available}</strong> of ${m.totals.donors} registered donors can donate today, across ${m.cities.reduce((n, c) => n + c.sectors.length, 0)} sectors in ${m.cities.length} cities.</p>
  </div></section>
  <div class="wrap">
    <section class="finder" aria-label="Find donors">
      <fieldset><legend>Blood group needed</legend>
        <div class="groups" id="groups">${m.blood_groups.map(g => `<button type="button" class="group-btn" data-group="${g}" aria-pressed="${s.group === g}">${g}</button>`).join('')}</div></fieldset>
      <div class="finder-row">
        <div class="field"><label for="city">City</label><select id="city">${cityOptions(s.cityId)}</select></div>
        <label class="checkline"><input type="checkbox" id="compat" ${s.compatible ? 'checked' : ''}> Also show compatible blood groups</label>
      </div>
      <fieldset><legend>Sector</legend><div class="sectors" id="sectors"></div></fieldset>
    </section>
    <section class="results" id="results" aria-live="polite"></section>
    <section id="teaser"></section>
    <section class="strip" aria-label="How it works">
      <div><h3>Hyper-local by design</h3><p>Every donor belongs to one sector of one city. A search for Downtown never returns someone from the other side of town.</p></div>
      <div><h3>Eligibility handled for you</h3><p>The ${m.eligibility_days}-day rule runs on the server. If a donor gave blood recently, or marked themselves busy, they simply don't appear.</p></div>
      <div><h3>Phone numbers stay private</h3><p>Numbers are masked for everyone. Signed-in recipients can reveal one at a time, and every reveal is logged.</p></div>
    </section>
  </div>`;

  $('#groups').addEventListener('click', e => {
    const b = e.target.closest('[data-group]'); if (!b) return;
    s.group = s.group === b.dataset.group ? '' : b.dataset.group;
    $$('.group-btn').forEach(x => x.setAttribute('aria-pressed', String(x.dataset.group === s.group)));
    refreshTiles(); runSearch();
  });
  $('#city').addEventListener('change', e => { s.cityId = +e.target.value; s.sectorId = null; refreshTiles(); runSearch(); });
  $('#compat').addEventListener('change', e => { s.compatible = e.target.checked; refreshTiles(); runSearch(); });
  $('#sectors').addEventListener('click', e => {
    const b = e.target.closest('[data-sector]'); if (!b) return;
    s.sectorId = b.dataset.sector ? +b.dataset.sector : null;
    $$('.sector').forEach(x => x.setAttribute('aria-pressed', String((x.dataset.sector || '') === (b.dataset.sector || ''))));
    runSearch();
  });
  refreshTiles(true); runSearch();
  api('/requests/open').then(({ requests }) => {
    if (!requests.length) return;
    $('#teaser').innerHTML = `<div class="page-head" style="margin-top:2.4rem"><h2>Needed right now</h2><a href="#/requests">See all live requests</a></div>
      <div class="list">${requests.slice(0, 3).map(r => requestItem(r)).join('')}</div>`;
  }).catch(() => {});
}

async function refreshTiles(first) {
  const s = state.search, box = $('#sectors');
  if (!box) return;
  const qs = new URLSearchParams({ city_id: s.cityId });
  if (s.group) { qs.set('blood_group', s.group); if (s.compatible) qs.set('compatible', '1'); }
  const { sectors } = await api('/sector-stats?' + qs);
  if (!$('#sectors')) return;
  const total = sectors.reduce((n, x) => n + x.available, 0);
  const label = s.group ? `${s.group} available` : 'available';
  const tile = (id, name, n) => `<button type="button" class="sector ${n === 0 ? 'zero' : ''}" data-sector="${id}" aria-pressed="${(s.sectorId || '') == id}">
      <span class="s-name">${esc(name)}</span><span class="s-count" data-n="${n}">${n}</span><span class="s-label">${label}</span></button>`;
  box.innerHTML = tile('', 'Whole city', total) + sectors.map(x => tile(x.sector_id, x.sector_name, x.available)).join('');
  if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) countUp($$('.s-count', box));
}
function countUp(nodes) {
  const t0 = performance.now();
  const step = now => {
    const k = Math.min(1, (now - t0) / 550);
    nodes.forEach(n => { n.textContent = Math.round(+n.dataset.n * (1 - Math.pow(1 - k, 3))); });
    if (k < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

async function runSearch() {
  const s = state.search, box = $('#results');
  if (!box) return;
  if (!s.group) { box.innerHTML = `<div class="empty"><strong>Choose a blood group to see eligible donors</strong>Then pick a sector, or search the whole city.</div>`; return; }
  const qs = new URLSearchParams({ blood_group: s.group });
  if (s.sectorId) qs.set('sector_id', s.sectorId); else qs.set('city_id', s.cityId);
  if (s.compatible) qs.set('compatible', '1');
  let data;
  try { data = await api('/search?' + qs); } catch (e) { box.innerHTML = `<div class="form-error">${esc(e.message)}</div>`; return; }
  if (!$('#results')) return;
  const city = state.meta.cities.find(c => c.city_id == s.cityId);
  const sec = s.sectorId && city.sectors.find(x => x.sector_id == s.sectorId);
  const where = sec ? `${sec.sector_name}, ${city.city_name}` : `all of ${city.city_name}`;
  const n = data.donors.length;
  box.innerHTML = `<div class="results-head"><h2>${n ? `${plural(n, 'eligible donor')} for ${esc(s.group)} in ${esc(where)}` : `No eligible ${esc(s.group)} donors in ${esc(where)}`}</h2></div>
    ${data.hidden ? `<div class="note">${plural(data.hidden, 'more donor')} here ${data.hidden === 1 ? 'is' : 'are'} hidden because they gave blood in the last ${data.eligibility_days} days or marked themselves busy.</div>` : ''}
    ${n ? `<ul class="list">${data.donors.map(d => donorRow(d, s.group)).join('')}</ul>`
        : `<div class="empty"><strong>Nobody is available in this selection right now</strong>Try the whole city${s.compatible ? '' : ', tick compatible blood groups'}, or <a href="#/${state.user && state.user.role === 'recipient' ? 'account' : 'login'}">post an emergency request</a> to alert donors.</div>`}`;
}

async function pageRequests() {
  main.innerHTML = `<div class="wrap page"><div class="page-head"><div><h1>Live requests</h1>
    <p>Open requests from the last 72 hours, most urgent first. Donors can respond here; patient names and phone numbers are never shown publicly.</p></div>
    <div class="field"><label for="rc">City</label><select id="rc"><option value="">All cities</option>${cityOptions()}</select></div></div>
    <div class="list" id="rlist"><div class="empty">Loading requests&hellip;</div></div></div>`;
  const load = async () => {
    const q = $('#rc').value ? `?city_id=${$('#rc').value}` : '';
    const { requests } = await api('/requests/open' + q);
    $('#rlist').outerHTML = `<div class="list" id="rlist">${requests.length ? requests.map(r => requestItem(r)).join('') : '<div class="empty"><strong>No open requests</strong>New emergency requests appear here as soon as they are posted.</div>'}</div>`;
  };
  $('#rc').addEventListener('change', load);
  await load();
}

function pageLogin() {
  main.innerHTML = `<div class="page"><div class="narrow"><h1>Sign in</h1>
    <form class="panel stack" id="loginForm" style="margin-top:1.2rem" novalidate>
      <div class="field"><label for="le">Email</label><input id="le" name="email" type="email" autocomplete="username" required></div>
      <div class="field"><label for="lp">Password</label><input id="lp" name="password" type="password" autocomplete="current-password" required></div>
      <button class="btn primary" type="submit">Sign in</button>
      <p class="meta">New here? <a href="#/register">Create an account</a> as a donor or recipient.</p>
    </form>
    <div class="demo"><strong>Demo accounts</strong> (click to fill)
      <dl>
        <dt>Recipient</dt><dd><button data-fill="recipient@demo.in|Demo@123">recipient@demo.in / Demo@123</button></dd>
        <dt>Donor</dt><dd><button data-fill="donor1@demo.in|Donor@123">donor1@demo.in / Donor@123</button></dd>
        <dt>Admin</dt><dd><button data-fill="admin@blooddonor.local|Admin@123">admin@blooddonor.local / Admin@123</button></dd>
      </dl></div></div></div>`;
  $$('[data-fill]').forEach(b => b.onclick = () => { const [e, p] = b.dataset.fill.split('|'); $('#le').value = e; $('#lp').value = p; });
  onSubmit($('#loginForm'), async v => {
    const { user } = await api('/login', { method: 'POST', body: v });
    await afterAuth(user);
  });
}
async function afterAuth(user) {
  state.user = user; state.unread = 0;
  await refreshUnread(); renderChrome();
  location.hash = user.role === 'admin' ? '#/admin' : '#/account';
  toast(`Welcome, ${user.full_name.split(' ')[0]}.`);
}

function pageRegister() {
  const first = state.meta.cities[0];
  main.innerHTML = `<div class="page"><div class="narrow" style="width:min(680px,100% - 2rem)"><h1>Create your account</h1>
    <p class="muted" style="margin-top:.5rem">Donors appear in sector searches while eligible. Recipients can reveal donor numbers and post emergency requests.</p>
    <form class="panel" id="regForm" style="margin-top:1.2rem" novalidate>
      <div class="form-grid">
        <div class="full"><div class="seg" role="group" aria-label="Account type">
          <button type="button" data-role="donor" aria-pressed="true">I want to donate</button>
          <button type="button" data-role="recipient" aria-pressed="false">I need blood</button></div></div>
        <div class="field"><label for="rn">Full name</label><input id="rn" name="full_name" autocomplete="name" required></div>
        <div class="field"><label for="re">Email</label><input id="re" name="email" type="email" autocomplete="email" required></div>
        <div class="field full"><label for="rp">Password</label><input id="rp" name="password" type="password" autocomplete="new-password" required>
          <span class="hint">At least 8 characters, with a letter and a number. Stored hashed, never in plain text.</span></div>
        <div class="donor-only form-grid full" style="padding:0">
          <div class="field"><label for="rb">Blood group</label><select id="rb" name="blood_group"><option value="">Choose</option>${state.meta.blood_groups.map(g => `<option>${g}</option>`).join('')}</select></div>
          <div class="field"><label for="rph">Mobile number</label><input id="rph" name="contact_no" inputmode="numeric" autocomplete="tel" placeholder="10-digit number"></div>
          ${locationFields(first.city_id, '', 'Your sector')}
          <div class="field"><label for="ra">Age</label><input id="ra" name="age" type="number" min="18" max="65"><span class="hint">Donors must be 18 or older.</span></div>
          <div class="field"><label for="rw">Weight (kg)</label><input id="rw" name="weight_kg" type="number" min="45" max="200"><span class="hint">Minimum 45 kg.</span></div>
          <div class="field full"><label for="rl">Last donation date <span class="muted">(optional)</span></label><input id="rl" name="last_donation_date" type="date" max="${new Date().toISOString().slice(0, 10)}">
            <span class="hint">If it was within ${state.meta.eligibility_days} days you'll be hidden from searches until you're eligible again.</span></div>
        </div>
        <div class="full"><button class="btn primary" type="submit" style="width:100%">Create account</button></div>
      </div></form></div></div>`;
  const form = $('#regForm');
  let role = 'donor';
  bindNested(form);
  $$('.seg button', form).forEach(b => b.onclick = () => {
    role = b.dataset.role;
    $$('.seg button', form).forEach(x => x.setAttribute('aria-pressed', String(x === b)));
    $('.donor-only', form).classList.toggle('hidden', role !== 'donor');
  });
  onSubmit(form, async v => {
    const body = { role, full_name: v.full_name, email: v.email, password: v.password };
    if (role === 'donor') Object.assign(body, { blood_group: v.blood_group, contact_no: v.contact_no, sector_id: v.sector_id, age: v.age, weight_kg: v.weight_kg, last_donation_date: v.last_donation_date });
    const { user } = await api('/register', { method: 'POST', body });
    await afterAuth(user);
  });
}

function alertsPanel(list) {
  return `<div class="panel"><h2>Alerts</h2>${list.length ? list.map(n => `<div class="alert ${n.is_read ? '' : 'unread'}"><span>${esc(n.message)}</span><time>${timeAgo(n.created_at)}</time></div>`).join('')
    : '<p class="muted">No alerts yet. Emergency requests in your sector will appear here.</p>'}</div>`;
}
async function loadAlerts() {
  const { notifications, unread } = await api('/notifications');
  if (unread) { await api('/notifications/read', { method: 'POST', body: {} }); }
  state.unread = 0; renderChrome();
  return notifications;
}

async function pageAccount() {
  if (!state.user) { location.hash = '#/login'; return; }
  if (state.user.role === 'admin') { location.hash = '#/admin'; return; }
  return state.user.role === 'donor' ? donorDashboard() : recipientDashboard();
}

async function donorDashboard() {
  const [info, alerts, open] = await Promise.all([api('/donor/me'), loadAlerts(), api('/requests/open')]);
  const p = info.profile, e = info.eligibility, days = info.eligibility_days;
  const pct = e.days_since === null ? 100 : Math.min(100, Math.round(e.days_since / days * 100));
  const mine = open.requests.filter(r => r.sector === p.sector && r.city === p.city);
  main.innerHTML = `<div class="wrap page">
    <div class="page-head"><div><h1>Hello, ${esc(p.full_name.split(' ')[0])}</h1>
      <p>${esc(p.sector)}, ${esc(p.city)} &middot; Blood group ${esc(p.blood_group)}</p></div>
      <span class="bg-badge" role="img" aria-label="Blood group ${esc(p.blood_group)}">${esc(p.blood_group)}</span></div>
    <div class="dash"><div class="stack">
      <div class="panel"><h2>Your eligibility</h2>
        <div class="elig-state"><span class="elig-title">${e.eligible ? 'You can donate today' : `Eligible again in ${plural(e.days_left, 'day')}`}</span>
          <span class="chip ${info.visible_in_search ? 'ok' : 'warn'}">${info.visible_in_search ? 'Visible in searches' : 'Hidden from searches'}</span></div>
        <div class="bar-track" role="progressbar" aria-valuemin="0" aria-valuemax="${days}" aria-valuenow="${Math.min(e.days_since ?? days, days)}" aria-label="Days since last donation">
          <div class="bar-fill ${e.eligible ? '' : 'wait'}" style="width:${pct}%"></div></div>
        <p class="meta">${e.last_donation_date ? `Last donation ${fmtDate(e.last_donation_date)} &middot; ${e.eligible ? 'the ' + days + '-day gap is complete' : `eligible on ${fmtDate(e.eligible_on)}`}` : 'No donation recorded yet.'}</p>
        <div class="mini-stats"><div><b>${info.stats.donations}</b><span class="meta">donations</span></div><div><b>${(info.stats.total_ml / 1000).toFixed(2)} L</b><span class="meta">given in total</span></div></div></div>
      <div class="panel"><h2>Availability</h2>
        <div class="seg" role="group" aria-label="Availability status" id="status">${['Available', 'Busy', 'Traveling'].map(s => `<button type="button" data-status="${s}" aria-pressed="${p.status === s}">${s}</button>`).join('')}</div>
        <p class="meta" style="margin-top:.7rem">Set Busy or Traveling to stop calls. Inside the ${days}-day window you stay hidden whatever you choose here.</p></div>
      ${mine.length ? `<div class="panel"><h2>Needed in your sector</h2><div>${mine.map(r => requestItem(r)).join('')}</div></div>` : ''}
      <div class="panel"><h2>Donation history</h2>${info.history.length ? `<div class="table-scroll"><table><thead><tr><th>Date</th><th>Volume</th><th>Place</th><th>Status</th></tr></thead><tbody>
        ${info.history.map(h => `<tr><td>${fmtDate(h.donation_date)}</td><td>${h.volume_ml} ml</td><td>${esc(h.location)}</td><td>${esc(h.status)}</td></tr>`).join('')}</tbody></table></div>` : '<p class="muted">Nothing recorded yet.</p>'}</div>
    </div><div class="stack">
      ${alertsPanel(alerts)}
      <form class="panel" id="donationForm" novalidate><h2>Record a donation</h2><div class="form-grid">
        <div class="field"><label for="dd">Date</label><input id="dd" name="donation_date" type="date" max="${new Date().toISOString().slice(0, 10)}" required></div>
        <div class="field"><label for="dv">Volume (ml)</label><select id="dv" name="volume_ml"><option>350</option><option selected>450</option></select></div>
        <div class="field full"><label for="dl">Place</label><input id="dl" name="location" placeholder="e.g. ${esc(p.city)} Government Blood Bank" required></div>
        <div class="full"><button class="btn primary" type="submit">Save donation</button></div></div></form>
      <form class="panel" id="profileForm" novalidate><h2>Your details</h2><div class="form-grid">
        ${locationFields(p.city_id, p.sector_id, 'Sector')}
        <div class="field"><label for="pp">Mobile number</label><input id="pp" name="contact_no" value="${esc(p.contact_no)}" inputmode="numeric"></div>
        <div class="field"><label for="pw">Weight (kg)</label><input id="pw" name="weight_kg" type="number" value="${p.weight_kg}"></div>
        <div class="full"><button class="btn" type="submit">Save changes</button></div></div></form>
    </div></div></div>`;
  $('#status').addEventListener('click', async ev => {
    const b = ev.target.closest('[data-status]'); if (!b) return;
    try { await api('/donor/status', { method: 'PATCH', body: { status: b.dataset.status } }); toast(`Status set to ${b.dataset.status}.`); await refreshMe(); donorDashboard(); }
    catch (err) { toast(err.message, 'bad'); }
  });
  bindNested($('#profileForm'));
  onSubmit($('#donationForm'), async v => { await api('/donor/donations', { method: 'POST', body: v }); toast('Donation saved. Your eligibility date was updated.'); await refreshMe(); donorDashboard(); });
  onSubmit($('#profileForm'), async v => { await api('/donor/profile', { method: 'PUT', body: { sector_id: v.sector_id, contact_no: v.contact_no, weight_kg: v.weight_kg } }); toast('Details saved.'); donorDashboard(); });
}

async function recipientDashboard() {
  const [{ requests }, alerts] = await Promise.all([api('/requests/mine'), loadAlerts()]);
  const first = state.meta.cities.find(c => c.city_name === 'Madanapalle') || state.meta.cities[0];
  main.innerHTML = `<div class="wrap page">
    <div class="page-head"><div><h1>My requests</h1><p>Post an emergency requirement and eligible, compatible donors in that sector are alerted straight away.</p></div></div>
    <div class="dash"><div class="stack">
      <div class="panel"><h2>Your requests</h2>${requests.length ? requests.map(r => requestItem(r, { mine: true })).join('') : '<p class="muted">You haven\'t posted a request yet.</p>'}</div>
      ${alertsPanel(alerts)}
    </div>
    <form class="panel" id="reqForm" novalidate><h2>Post an emergency requirement</h2><div class="form-grid">
      <div class="field full"><label for="qp">Patient name</label><input id="qp" name="patient_name" required></div>
      <div class="field"><label for="qb">Blood group needed</label><select id="qb" name="blood_group"><option value="">Choose</option>${state.meta.blood_groups.map(g => `<option>${g}</option>`).join('')}</select></div>
      <div class="field"><label for="qu">Units</label><input id="qu" name="units" type="number" min="1" max="10" value="1"></div>
      <div class="field full"><label for="qh">Hospital</label><input id="qh" name="hospital" placeholder="Hospital name" required></div>
      ${locationFields(first.city_id, '', 'Hospital sector')}
      <div class="field"><label for="qg">Urgency</label><select id="qg" name="urgency_level"><option>Critical</option><option selected>Urgent</option><option>Planned</option></select></div>
      <div class="field"><label for="qc">Contact number</label><input id="qc" name="contact_no" inputmode="numeric" placeholder="10-digit number"></div>
      <div class="field full"><label for="qn">Note <span class="muted">(optional)</span></label><textarea id="qn" name="note" maxlength="300" placeholder="Ward, doctor's name, or anything donors should know"></textarea></div>
      <div class="full"><button class="btn primary" type="submit" style="width:100%">Alert donors in this sector</button></div></div></form></div></div>`;
  bindNested($('#reqForm'));
  onSubmit($('#reqForm'), async v => {
    const r = await api('/requests', { method: 'POST', body: v });
    toast(r.notified ? `Request posted. ${plural(r.notified, 'eligible donor')} alerted.` : 'Request posted. No eligible donors in that sector yet; it is visible on Live requests.');
    recipientDashboard();
  });
  main.addEventListener('click', recipientClicks);
  await decorateMine();
}
async function recipientClicks(ev) {
  if (routeKey() !== 'account') { main.removeEventListener('click', recipientClicks); return; }
  const req = ev.target.closest('[data-request]');
  const act = ev.target.closest('[data-act]');
  if (act) {
    try { await api(`/requests/${act.dataset.id}/status`, { method: 'POST', body: { status: act.dataset.act } }); toast('Request updated.'); recipientDashboard(); }
    catch (e) { toast(e.message, 'bad'); }
  }
}
async function pageAdmin() {
  if (!state.user || state.user.role !== 'admin') { location.hash = '#/login'; return; }
  const o = await api('/admin/overview');
  const c = o.counts;
  main.innerHTML = `<div class="wrap page"><div class="page-head"><div><h1>Administration</h1><p>Verify emergency requests, manage the city and sector list, and keep an eye on donor activity.</p></div></div>
    <div class="counters">
      <div><b>${c.donors}</b><span>registered donors</span></div><div><b>${c.available}</b><span>can donate today</span></div>
      <div><b>${c.open_requests}</b><span>open requests (${c.pending} to verify)</span></div><div><b>${c.contact_views}</b><span>phone numbers revealed</span></div></div>
    <div class="stack">
      <div class="panel"><h2>Emergency requests</h2><div class="table-scroll"><table><thead><tr><th>Need</th><th>Where</th><th>Urgency</th><th>Status</th><th>Posted</th><th></th></tr></thead><tbody>
        ${o.requests.map(r => `<tr><td><strong>${esc(r.blood_group)}</strong> &times; ${r.units}<div class="meta">${esc(r.patient_name)}</div></td>
          <td>${esc(r.hospital)}<div class="meta">${esc(r.sector)}, ${esc(r.city)}</div></td><td>${urgencyChip(r.urgency)}</td>
          <td>${esc(r.status)}<div class="meta">${plural(r.responders, 'response')}</div></td><td>${timeAgo(r.created_at)}</td>
          <td class="req-actions">${r.status === 'Pending' ? `<button class="btn small primary" data-adm="Verified" data-id="${r.request_id}">Verify</button>` : ''}
            ${['Pending', 'Verified'].includes(r.status) ? `<button class="btn small" data-adm="Closed" data-id="${r.request_id}">Close</button>` : ''}</td></tr>`).join('')}</tbody></table></div></div>
      <div class="panel"><h2>Cities and sectors</h2>
        ${state.meta.cities.map(ct => `<p style="margin-bottom:.7rem"><strong>${esc(ct.city_name)}</strong><br>${ct.sectors.map(s => `<span class="chip gray" style="margin:.15rem .25rem .15rem 0">${esc(s.sector_name)} <button class="btn quiet small" style="min-height:0;padding:0 .2rem" data-delsec="${s.sector_id}" aria-label="Remove ${esc(s.sector_name)}">&times;</button></span>`).join('')}</p>`).join('')}
        <form class="inline-form" id="secForm" novalidate>
          <div class="field"><label for="sc">City</label><select id="sc" name="city_id">${cityOptions()}<option value="">New city&hellip;</option></select></div>
          <div class="field hidden" id="ncWrap"><label for="nc">New city name</label><input id="nc" name="city_name"></div>
          <div class="field"><label for="sn">New sector name</label><input id="sn" name="sector_name" required></div>
          <button class="btn primary" type="submit">Add sector</button></form></div>
      <div class="panel"><h2>Latest donors</h2><div class="table-scroll"><table><thead><tr><th>Name</th><th>Group</th><th>Location</th><th>Status</th><th>Eligibility</th></tr></thead><tbody>
        ${o.donors.map(d => `<tr><td>${esc(d.full_name)}</td><td>${esc(d.blood_group)}</td><td>${esc(d.sector)}, ${esc(d.city)}</td><td>${esc(d.status)}</td>
          <td>${d.eligible ? '<span class="chip ok">Eligible</span>' : `<span class="chip warn">Day ${d.days_since} of 90</span>`}</td></tr>`).join('')}</tbody></table></div></div>
    </div></div>`;
  main.onclick = async ev => {
    const a = ev.target.closest('[data-adm]'), d = ev.target.closest('[data-delsec]');
    try {
      if (a) { await api(`/requests/${a.dataset.id}/status`, { method: 'POST', body: { status: a.dataset.adm } }); toast(`Request ${a.dataset.adm.toLowerCase()}.`); pageAdmin(); }
      if (d) { await api(`/admin/sectors/${d.dataset.delsec}`, { method: 'DELETE', body: {} }); await loadMeta(); toast('Sector removed.'); pageAdmin(); }
    } catch (e) { toast(e.message, 'bad'); }
  };
  $('#sc').addEventListener('change', e => $('#ncWrap').classList.toggle('hidden', e.target.value !== ''));
  onSubmit($('#secForm'), async v => {
    await api('/admin/sectors', { method: 'POST', body: v.city_id ? { city_id: v.city_id, sector_name: v.sector_name } : { city_name: v.city_name, sector_name: v.sector_name } });
    await loadMeta(); toast('Sector added.'); pageAdmin();
  });
}

function pageNotFound() {
  main.innerHTML = `<div class="wrap page"><div class="empty"><strong>That page doesn't exist</strong><a href="#/">Back to donor search</a></div></div>`;
}

/* ------------------------------------------------------ recipient extras */
// Responders list + status buttons for the recipient's own requests.
async function decorateMine() {
  if (routeKey() !== 'account' || !state.user || state.user.role !== 'recipient') return;
  for (const el of $$('[data-request]')) {
    const id = el.dataset.request, box = $('[data-responders]', el);
    if (!box) continue;
    const closed = /Fulfilled|Closed/.test(el.textContent);
    $('.req-actions', el).innerHTML = closed ? '' : `<button class="btn small" data-act="Fulfilled" data-id="${id}">Mark fulfilled</button><button class="btn small quiet" data-act="Closed" data-id="${id}">Close</button>`;
    const { donors } = await api(`/requests/${id}/responses`);
    if (donors.length) box.innerHTML = `<ul class="list" style="margin-top:.5rem">${donors.map(d => donorRow(d)).join('')}</ul>`;
  }
}

/* -------------------------------------------------------------- router */
const routes = { '': pageHome, requests: pageRequests, login: pageLogin, register: pageRegister, account: pageAccount, admin: pageAdmin };
const routeKey = () => location.hash.replace(/^#\/?/, '').split('?')[0];

async function render() {
  markNav();
  main.onclick = null;
  const fn = routes[routeKey()] || pageNotFound;
  await fn();
  markNav();
  window.scrollTo(0, 0);
}

async function loadMeta() { state.meta = await api('/meta'); }

async function boot() {
  try {
    const [{ user }] = await Promise.all([api('/me'), loadMeta()]);
    state.user = user;
    await refreshUnread();
  } catch (e) {
    main.innerHTML = `<div class="wrap page"><div class="form-error">The server isn't responding. Start it with <code>python server.py</code> and refresh this page.</div></div>`;
    return;
  }
  renderChrome();
  window.addEventListener('hashchange', render);
  render();
  setInterval(refreshUnread, 30000);
}
boot();
})();
