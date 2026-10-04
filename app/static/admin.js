'use strict';

/* The admin console. Everything shown comes from admin-only endpoints; every value is
   written with textContent, never innerHTML, because the audit trail and the logs
   contain strings chosen by whoever made the request. */

const SESSION_KEY = 'seatres.session';
const REFRESH_MS = 5000;
const $ = (id) => document.getElementById(id);

const state = { session: null, tab: 'overview', timer: null, auditBefore: null, auditRows: [] };

/* ---------- session ---------- */

function loadSession() {
  try { return JSON.parse(sessionStorage.getItem(SESSION_KEY)); } catch { return null; }
}

function setSession(session) {
  state.session = session;
  if (session) sessionStorage.setItem(SESSION_KEY, JSON.stringify(session));
  else sessionStorage.removeItem(SESSION_KEY);
  const admin = Boolean(session && session.role === 'admin');
  $('signin').hidden = admin;
  $('console').hidden = !admin;
  $('btn-signout').hidden = !session;
  $('who').textContent = session ? (session.email || 'Guest') : '';
  if (session && !admin) $('signin-error').textContent = 'That account is not an admin.';
}

async function api(path, { method = 'GET', body, params } = {}) {
  const url = new URL(path, location.origin);
  for (const [key, value] of Object.entries(params || {})) {
    if (value !== '' && value != null) url.searchParams.set(key, value);
  }
  const headers = {};
  if (body) headers['Content-Type'] = 'application/json';
  if (state.session) headers.Authorization = `Bearer ${state.session.access_token}`;
  const response = await fetch(url, { method, headers, body: body ? JSON.stringify(body) : undefined });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401 && state.session) {
    setSession(null);
    throw new Error('Your session has expired. Sign in again.');
  }
  if (!response.ok) {
    const error = data.error || {};
    throw new Error(`${error.message || response.statusText} (${error.code || response.status})`);
  }
  return data;
}

/* ---------- small DOM helpers ---------- */

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'onclick') node.addEventListener('click', value);
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) if (child) node.append(child);
  return node;
}

function table(target, columns, rows, empty) {
  const node = $(target);
  node.replaceChildren();
  if (!rows.length) {
    node.append(el('tbody', {}, el('tr', {}, el('td', { class: 'muted', text: empty }))));
    return;
  }
  node.append(el('thead', {}, el('tr', {}, columns.map((c) => el('th', { class: c.num ? 'num' : '', text: c.title })))));
  node.append(el('tbody', {}, rows.map((row) => el('tr', {}, columns.map((c) => {
    const value = c.render(row);
    const cell = el('td', { class: c.num ? 'num' : (c.mono ? 'mono' : '') });
    if (value instanceof Node) cell.append(value); else cell.textContent = value ?? '';
    return cell;
  })))));
}

const number = (value) => Number(value || 0).toLocaleString('en-IN');
const rupees = (paise) => `₹${(paise / 100).toLocaleString('en-IN')}`;
const clock = (iso) => new Date(iso).toLocaleTimeString('en-GB');
const short = (id) => (id ? `${String(id).slice(0, 8)}…` : '');

function statusPill(code) {
  return el('span', { class: `pill s${String(code)[0]}`, text: String(code) });
}

function duration(seconds) {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m ${seconds % 60}s`;
}

/* ---------- overview ---------- */

function card(label, value, hint, tone) {
  return el('div', { class: `card ${tone || ''}` }, [
    el('div', { class: 'label', text: label }),
    el('div', { class: 'value', text: value }),
    hint ? el('div', { class: 'hint', text: hint }) : null,
  ]);
}

function sumCounters(counters, prefix) {
  return Object.entries(counters)
    .filter(([name]) => name.startsWith(prefix))
    .reduce((total, [, value]) => total + value, 0);
}

async function loadOverview() {
  const data = await api('/admin/overview', { params: { window_minutes: $('window').value } });
  const { requests, counters, system } = data;
  const classes = requests.by_status_class;
  const serverErrors = classes['5xx'] || 0;
  const unhandled = sumCounters(counters, 'unhandled_exceptions_total');
  const dropped = counters.audit_records_dropped_total || 0;
  const inUse = system.pool.size - system.pool.idle;

  $('cards').replaceChildren(
    card('Requests', number(requests.total), `last ${data.window_minutes} min`),
    card('Succeeded', number(classes['2xx'] || 0), '2xx', 'good'),
    card('Declined', number(classes['4xx'] || 0), '4xx — clean refusals', (classes['4xx'] || 0) ? 'warn' : ''),
    card('Server errors', number(serverErrors), '5xx — must stay 0', serverErrors ? 'bad' : 'good'),
    card('Bookings confirmed', number(counters.reservations_confirmed_total), 'since start'),
    card('Unhandled exceptions', number(unhandled), 'since start — must stay 0', unhandled ? 'bad' : 'good'),
    card('DB connections', `${inUse} / ${system.pool.max}`, `${system.pool.size} open`),
    card('Audit buffer', number(system.audit.queue_depth), `${number(dropped)} dropped`, dropped ? 'warn' : ''),
    card('Rate limiting', system.rate_limit_enabled ? 'On' : 'Off', system.rate_limit_enabled ? '' : 'switched off by configuration', system.rate_limit_enabled ? '' : 'warn'),
    card('Uptime', duration(system.uptime_seconds), `v${system.version}`),
  );

  const peak = Math.max(1, ...requests.per_minute.map((m) => m.requests));
  $('chart').replaceChildren(...requests.per_minute.map((m) => {
    const answered = m.requests - m.declined - m.server_errors;
    const part = (count, tone) => el('span', { class: tone, style: `height:${(count / peak) * 100}%` });
    return el('div', { class: 'bar', title: `${clock(m.minute)} — ${m.requests} requests, ${m.declined} declined, ${m.server_errors} server errors` },
      [part(answered, 'ok'), part(m.declined, 'warn'), part(m.server_errors, 'bad')]);
  }));
  if (!requests.per_minute.length) $('chart').append(el('p', { class: 'muted', text: 'No requests in this window.' }));

  table('outcomes', [
    { title: 'Reason', render: (r) => r.code, mono: true },
    { title: 'Requests', render: (r) => number(r.requests), num: true },
  ], requests.by_outcome, 'No declines in this window.');

  table('routes', [
    { title: 'Method', render: (r) => r.method },
    { title: 'Route', render: (r) => r.route, mono: true },
    { title: 'Requests', render: (r) => number(r.requests), num: true },
    { title: 'p50 ms', render: (r) => number(r.p50_ms), num: true },
    { title: 'p95 ms', render: (r) => number(r.p95_ms), num: true },
    { title: '5xx', render: (r) => number(r.server_errors), num: true },
  ], requests.by_route, 'No requests in this window.');

  table('counters', [
    { title: 'Counter', render: (r) => r[0], mono: true },
    { title: 'Value', render: (r) => number(r[1]), num: true },
  ], Object.entries(counters).sort(), 'No counters yet.');
}

/* ---------- shows ---------- */

function patternSeats(form) {
  const rows = Math.min(26, Number(form.rows.value) || 0);
  const perRow = Number(form.per_row.value) || 0;
  const seats = [];
  for (let r = 0; r < rows; r += 1) {
    for (let n = 1; n <= perRow; n += 1) seats.push(`${String.fromCharCode(65 + r)}${n}`);
  }
  return seats;
}

function previewShow() {
  const seats = patternSeats($('show-form'));
  $('show-preview').textContent = seats.length
    ? `${seats.length} seats: ${seats[0]} … ${seats[seats.length - 1]}`
    : 'Enter rows and seats per row.';
}

async function createShow(event) {
  event.preventDefault();
  const form = event.target;
  const result = $('show-result');
  result.className = 'small';
  try {
    const show = await api('/shows', {
      method: 'POST',
      body: {
        name: form.name.value.trim(),
        seats: patternSeats(form),
        price_paise: Math.round(Number(form.price.value) * 100),
        per_user_limit: Number(form.limit.value),
      },
    });
    result.textContent = `Created "${show.name}" with ${show.total_seats} seats.`;
    await loadShows();
  } catch (error) {
    result.className = 'small error';
    result.textContent = error.message;
  }
}

async function loadShows() {
  const { items } = await api('/admin/shows');
  table('shows', [
    { title: 'Show', render: (s) => s.name },
    { title: 'Price', render: (s) => rupees(s.price_paise), num: true },
    { title: 'Available', render: (s) => {
      const sold = s.total_seats - s.available;
      return el('div', {}, [
        el('span', { text: `${number(s.available)} of ${number(s.total_seats)}` }),
        el('div', { class: 'meter', title: `${sold} taken` }, el('span', { style: `width:${(sold / s.total_seats) * 100}%` })),
      ]);
    } },
    { title: 'Created', render: (s) => new Date(s.created_at).toLocaleString('en-GB') },
    { title: 'Id', render: (s) => s.show_id, mono: true },
    { title: '', render: (s) => el('button', {
      class: 'btn small danger', type: 'button', text: 'Delete', onclick: () => deleteShow(s),
    }) },
  ], items, 'No shows yet. Create one on the left.');
}

async function deleteShow(show) {
  const taken = show.total_seats - show.available;
  const warning = taken
    ? `${taken} of its seats are booked or held; those bookings are deleted too.`
    : 'Nobody has booked it.';
  if (!window.confirm(`Delete "${show.name}"? ${warning} This cannot be undone.`)) return;
  const result = $('show-result');
  try {
    const { deleted } = await api(`/shows/${show.show_id}`, { method: 'DELETE' });
    result.className = 'small';
    result.textContent = `Deleted "${show.name}": ${deleted.seats} seats, ${deleted.reservations} reservations.`;
  } catch (error) {
    result.className = 'small error';
    result.textContent = error.message;
  }
  await loadShows();
}

/* ---------- audit ---------- */

function auditParams() {
  const form = $('audit-filter');
  return {
    window_minutes: 1440,
    status_code: form.status_code.value,
    outcome_code: form.outcome_code.value.trim(),
    request_id: form.request_id.value.trim(),
    show_id: form.show_id.value.trim(),
  };
}

function renderAudit() {
  table('audit', [
    { title: 'Time', render: (r) => clock(r.occurred_at) },
    { title: 'Method', render: (r) => r.method },
    { title: 'Route', render: (r) => r.route, mono: true },
    { title: 'Status', render: (r) => statusPill(r.status_code) },
    { title: 'ms', render: (r) => number(r.duration_ms), num: true },
    { title: 'Outcome', render: (r) => r.outcome_code || '', mono: true },
    { title: 'Who', render: (r) => (r.user_id ? `${r.is_guest ? 'guest' : 'user'} ${short(r.user_id)}` : 'anonymous') },
    { title: 'Seats', render: (r) => (r.seat_labels || []).join(', ') },
    { title: 'Request id', render: (r) => el('button', {
      class: 'link mono', type: 'button', text: short(r.request_id), title: 'Show the log lines for this request',
      onclick: () => showLogsFor(r.request_id),
    }) },
  ], state.auditRows, 'No requests match.');
}

async function loadAudit({ older = false } = {}) {
  const params = auditParams();
  if (older) params.before_id = state.auditBefore;
  const data = await api('/admin/audit', { params });
  state.auditRows = older ? state.auditRows.concat(data.items) : data.items;
  state.auditBefore = data.next_before_id;
  $('audit-more').hidden = !data.next_before_id;
  renderAudit();
}

/* ---------- logs ---------- */

const LOG_HEAD = new Set(['ts', 'level', 'event', 'request_id', 'service', 'version']);

async function loadLogs() {
  const form = $('logs-filter');
  const { items } = await api('/admin/logs', {
    params: { limit: 300, level: form.level.value, event: form.event.value.trim(), request_id: form.request_id.value.trim() },
  });
  const rows = items.map((line) => {
    const rest = Object.fromEntries(Object.entries(line).filter(([key]) => !LOG_HEAD.has(key)));
    return el('div', { class: 'logline' }, [
      el('span', { text: clock(line.ts) }),
      el('span', { class: `lvl-${line.level}`, text: line.level }),
      el('span', { text: line.event }),
      el('span', { class: 'rest', text: `${line.request_id ? `${short(line.request_id)} ` : ''}${JSON.stringify(rest)}` }),
    ]);
  });
  $('logs').replaceChildren(...(rows.length ? rows : [el('p', { class: 'muted', text: 'No log lines match.' })]));
}

function showLogsFor(requestId) {
  $('logs-filter').request_id.value = requestId || '';
  selectTab('logs');
}

/* ---------- tabs and refresh ---------- */

const LOADERS = { overview: loadOverview, shows: loadShows, audit: loadAudit, logs: loadLogs };

async function refresh() {
  if (!state.session || state.session.role !== 'admin') return;
  try {
    await LOADERS[state.tab]();
    $('notice').textContent = '';
    $('updated').textContent = `Updated ${new Date().toLocaleTimeString('en-GB')}`;
  } catch (error) {
    $('notice').textContent = error.message;
  }
}

function selectTab(tab) {
  state.tab = tab;
  for (const button of document.querySelectorAll('.tab')) button.classList.toggle('active', button.dataset.tab === tab);
  for (const name of Object.keys(LOADERS)) $(`tab-${name}`).hidden = name !== tab;
  refresh();
}

function schedule() {
  clearInterval(state.timer);
  state.timer = setInterval(() => { if ($('auto').checked && !document.hidden) refresh(); }, REFRESH_MS);
}

async function signIn(event) {
  event.preventDefault();
  const form = event.target;
  $('signin-error').textContent = '';
  try {
    const session = await api('/auth/login', { method: 'POST', body: { email: form.email.value, password: form.password.value } });
    setSession(session);
    form.reset();
    refresh();
  } catch (error) {
    $('signin-error').textContent = error.message;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  for (const button of document.querySelectorAll('.tab')) button.addEventListener('click', () => selectTab(button.dataset.tab));
  $('signin-form').addEventListener('submit', signIn);
  $('btn-signout').addEventListener('click', () => setSession(null));
  $('window').addEventListener('change', refresh);
  $('show-form').addEventListener('submit', createShow);
  $('show-form').addEventListener('input', previewShow);
  $('audit-filter').addEventListener('submit', (e) => { e.preventDefault(); loadAudit().catch((x) => { $('notice').textContent = x.message; }); });
  $('audit-clear').addEventListener('click', () => { $('audit-filter').reset(); refresh(); });
  $('audit-more').addEventListener('click', () => loadAudit({ older: true }).catch((x) => { $('notice').textContent = x.message; }));
  $('logs-filter').addEventListener('submit', (e) => { e.preventDefault(); refresh(); });
  $('logs-clear').addEventListener('click', () => { $('logs-filter').reset(); refresh(); });
  previewShow();
  setSession(loadSession());
  schedule();
  refresh();
});
