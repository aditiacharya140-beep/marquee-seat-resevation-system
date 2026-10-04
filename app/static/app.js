'use strict';

const POLL_MS = 4000;
const REQUEST_TIMEOUT_MS = 10000;
const MAX_THROTTLE_RETRIES = 2;
const TOAST_MS = 6000;
const SESSION_KEY = 'seatres.session';
const SEAT_LABEL = /^([A-Za-z]+)[-\s]?(\d+)$/;
const STATUS_TEXT = {
  held: 'Held', confirmed: 'Confirmed', cancelled: 'Cancelled', expired: 'Expired',
  draft: 'Not on sale yet', closed: 'Closed',
};

const state = {
  session: null,
  shows: [],
  showsCursor: null,
  show: null,
  mine: new Set(),
  selected: new Set(),
  // One idempotency key per booking attempt: kept across retries of the same
  // selection, replaced only when the selection or the action changes.
  attempt: null,
  bookings: [],
  bookingsCursor: null,
  busy: false,
};

const showNames = new Map();
const seatEls = new Map();
let builtMapFor = null;
let clockOffsetMs = 0;
let recovering = null;
let polling = false;
let authMode = 'signin';

const $ = (id) => document.getElementById(id);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const now = () => Date.now() + clockOffsetMs;
const byLabel = new Intl.Collator('en', { numeric: true }).compare;

function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [name, value] of Object.entries(props)) {
    if (name.startsWith('on')) el.addEventListener(name.slice(2), value);
    else if (value != null && value !== false) el.setAttribute(name, value === true ? '' : value);
  }
  el.append(...children.filter((child) => child != null && child !== false));
  return el;
}

function money(paise, currency) {
  return new Intl.NumberFormat('en-IN', {
    style: 'currency', currency, minimumFractionDigits: paise % 100 ? 2 : 0,
  }).format(paise / 100);
}

function duration(seconds) {
  if (seconds % 60) return `${seconds} seconds`;
  const minutes = seconds / 60;
  return `${minutes} minute${minutes === 1 ? '' : 's'}`;
}

function listOf(labels) {
  return labels.length > 1
    ? `${labels.slice(0, -1).join(', ')} and ${labels.at(-1)}`
    : labels.join('');
}

function newKey() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, '0')).join('');
}

/* ---------- toasts ---------- */

function toast(message, kind = 'info', requestId = null) {
  const el = h('div', { class: `toast ${kind}`, onclick: () => el.remove() }, message,
    requestId && h('span', { class: 'rid' }, `request ${requestId}`));
  $('toasts').append(el);
  setTimeout(() => el.remove(), kind === 'error' ? TOAST_MS * 1.5 : TOAST_MS);
}

function explain(error) {
  const details = error.details || {};
  switch (error.code) {
    case 'PER_USER_LIMIT':
      return `This show allows ${details.limit} seats per person, and you already have ${details.currently_held}.`;
    case 'VALIDATION_ERROR': {
      const field = (details.fields || [])[0];
      return field ? `${field.loc.at(-1)}: ${field.msg}` : error.message;
    }
    case 'IDEMPOTENCY_IN_PROGRESS':
      return 'Your booking is still being processed. Try again in a moment.';
    case 'RATE_LIMITED':
      return `Too many requests. Try again in ${error.retryAfter || 1}s.`;
    default:
      return error.message;
  }
}

function fail(error) {
  if (!(error instanceof ApiError)) throw error;
  toast(explain(error), 'error', error.requestId);
}

/* ---------- API ---------- */

class ApiError extends Error {
  constructor(status, envelope, response) {
    const error = (envelope && envelope.error) || {};
    super(error.message || 'Could not reach the service. Check your connection.');
    this.status = status;
    this.code = error.code || 'NETWORK';
    this.details = error.details;
    this.requestId = error.request_id || (response && response.headers.get('X-Request-ID'));
    this.retryAfter = response ? Number(response.headers.get('Retry-After')) || 0 : 0;
  }
}

// `quiet` is for background refreshes: no waiting out a throttle, no toasts.
async function api(method, path, { body, headers = {}, auth = true, quiet = false } = {}, tries = 0, recovered = false) {
  const token = auth && state.session ? state.session.access_token : null;
  let response;
  try {
    response = await fetch(path, {
      method,
      headers: {
        ...(body ? { 'Content-Type': 'application/json' } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...headers,
      },
      body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
  } catch {
    throw new ApiError(0, null, null);
  }
  const serverTime = Date.parse(response.headers.get('Date'));
  if (!Number.isNaN(serverTime)) clockOffsetMs = serverTime - Date.now();
  const data = await response.json().catch(() => null);
  if (response.ok) return data;

  const error = new ApiError(response.status, data, response);
  if (response.status === 429 && !quiet && tries < MAX_THROTTLE_RETRIES) {
    const wait = Math.max(error.retryAfter, 1);
    toast(`Too many requests. Waiting ${wait}s, then retrying.`);
    await sleep(wait * 1000);
    return api(method, path, { body, headers, auth, quiet }, tries + 1, recovered);
  }
  if (error.code === 'UNAUTHENTICATED' && token && !recovered && await recoverSession(token)) {
    return api(method, path, { body, headers, auth, quiet }, tries, true);
  }
  throw error;
}

/* ---------- session ---------- */

function setSession(session) {
  state.session = session;
  try {
    if (session) sessionStorage.setItem(SESSION_KEY, JSON.stringify(session));
    else sessionStorage.removeItem(SESSION_KEY);
  } catch { /* storage unavailable: the session lasts until reload */ }
  renderAccount();
}

function restoreSession() {
  try {
    return JSON.parse(sessionStorage.getItem(SESSION_KEY));
  } catch {
    return null;
  }
}

async function startGuest() {
  setSession(await api('POST', '/auth/guest', { auth: false }));
}

// Concurrent requests that all met a 401 share one recovery.
function recoverSession(staleToken) {
  if (!state.session || state.session.access_token !== staleToken) return Promise.resolve(true);
  recovering ??= (async () => {
    try {
      const refreshToken = state.session.refresh_token;
      if (refreshToken) {
        try {
          const fresh = await api('POST', '/auth/refresh', { auth: false, body: { refresh_token: refreshToken } });
          setSession({ ...state.session, access_token: fresh.access_token });
          return true;
        } catch { /* fall through to a guest session */ }
      }
      await startGuest();
      toast('Your session expired, so you are continuing as a new guest.');
      return true;
    } catch {
      return false;
    } finally {
      recovering = null;
    }
  })();
  return recovering;
}

function renderAccount() {
  const session = state.session;
  const signedIn = Boolean(session && !session.is_guest);
  $('who').textContent = signedIn ? session.email : 'Guest';
  $('btn-signin').hidden = signedIn;
  $('btn-register').hidden = signedIn;
  $('btn-signout').hidden = !signedIn;
  $('admin-panel').hidden = !(session && session.role === 'admin');
}

async function sessionChanged() {
  state.selected.clear();
  state.attempt = null;
  await Promise.all([loadBookings(), loadMine()]);
  renderShow();
}

function openAuth(mode) {
  authMode = mode;
  const registering = mode === 'register';
  $('auth-title').textContent = registering ? 'Create account' : 'Sign in';
  $('auth-submit').textContent = registering ? 'Register' : 'Sign in';
  $('auth-note').textContent = registering
    ? 'Bookings you made as a guest stay with your new account.'
    : 'Signing in switches to that account\'s bookings.';
  $('auth-form').elements.password.autocomplete = registering ? 'new-password' : 'current-password';
  $('auth-error').hidden = true;
  $('auth-dialog').showModal();
}

async function submitAuth(event) {
  event.preventDefault();
  const form = $('auth-form');
  const body = { email: form.elements.email.value.trim(), password: form.elements.password.value };
  const upgrading = authMode === 'register' && state.session && state.session.is_guest;
  const path = authMode === 'signin' ? '/auth/login' : upgrading ? '/auth/upgrade' : '/auth/register';
  $('auth-submit').disabled = true;
  try {
    setSession(await api('POST', path, { body, auth: upgrading }));
    form.reset();
    $('auth-dialog').close();
    toast(authMode === 'signin' ? 'Signed in.' : 'Account created.', 'ok');
    await sessionChanged();
  } catch (error) {
    if (!(error instanceof ApiError)) throw error;
    $('auth-error').textContent = explain(error) + (error.requestId ? ` (request ${error.requestId})` : '');
    $('auth-error').hidden = false;
  } finally {
    $('auth-submit').disabled = false;
  }
}

async function signOut() {
  setSession(null);
  await startGuest().catch(fail);
  await sessionChanged();
}

/* ---------- shows ---------- */

async function loadShows(more = false) {
  const query = more && state.showsCursor ? `?cursor=${encodeURIComponent(state.showsCursor)}` : '';
  const page = await api('GET', `/shows${query}`);
  state.shows = more ? state.shows.concat(page.items) : page.items;
  state.showsCursor = page.next_cursor;
  for (const show of page.items) showNames.set(show.show_id, show.name);
  renderShows();
}

function renderShows() {
  const open = state.show && state.show.show_id;
  $('shows-empty').hidden = state.shows.length > 0;
  $('btn-more-shows').hidden = !state.showsCursor;
  $('show-list').replaceChildren(...state.shows.map((show) => h('li', {},
    h('button', {
      class: `show-item${show.show_id === open ? ' active' : ''}`,
      type: 'button',
      'aria-current': show.show_id === open ? 'true' : null,
      onclick: () => openShow(show.show_id).catch(fail),
    },
      h('span', {},
        h('span', { class: 'name' }, show.name),
        h('span', { class: 'muted small' },
          `${show.event_kind} · ${show.total_seats} seats`
          + (show.status === 'on_sale' ? '' : ` · ${STATUS_TEXT[show.status]}`))),
      h('span', { class: 'price' }, money(show.price_paise, show.currency))))));
}

async function openShow(showId) {
  const show = await api('GET', `/shows/${showId}`);
  state.show = show;
  state.selected.clear();
  state.attempt = null;
  state.mine = new Set();
  showNames.set(show.show_id, show.name);
  history.replaceState(null, '', `#${show.show_id}`);
  renderShows();
  renderShow();
  await loadMine();
  renderShow();
}

// The API does not say who holds a seat, so "yours" comes from this person's own
// active reservations for the show.
async function loadMine() {
  const show = state.show;
  if (!show || !state.session) return;
  try {
    const pages = await Promise.all(['held', 'confirmed'].map((status) =>
      api('GET', `/reservations?show_id=${show.show_id}&status=${status}`, { quiet: true })));
    if (state.show && state.show.show_id === show.show_id) {
      state.mine = new Set(pages.flatMap((page) => page.items.flatMap((item) => item.seats)));
    }
  } catch (error) {
    if (!(error instanceof ApiError)) throw error;
  }
}

async function refreshShow() {
  const showId = state.show && state.show.show_id;
  if (!showId) return;
  let show;
  try {
    show = await api('GET', `/shows/${showId}`, { quiet: true });
  } catch (error) {
    if (!(error instanceof ApiError)) throw error;
    return;
  }
  if (!state.show || state.show.show_id !== showId) return;
  state.show = show;
  const taken = show.seats
    .filter((seat) => state.selected.has(seat.label) && seat.status !== 'available')
    .map((seat) => seat.label);
  taken.forEach((label) => state.selected.delete(label));
  // A seat that turned out to be this person's own was not lost to anyone.
  const lost = taken.filter((label) => !state.mine.has(label));
  if (lost.length) toast(`${listOf(lost)} ${lost.length > 1 ? 'were' : 'was'} just taken.`);
  renderShow();
}

/* ---------- seat map ---------- */

function seatRows(seats) {
  const rows = new Map();
  for (const seat of seats) {
    const match = SEAT_LABEL.exec(seat.label);
    const row = match ? match[1].toUpperCase() : '';
    if (!rows.has(row)) rows.set(row, []);
    rows.get(row).push({ label: seat.label, text: match ? match[2] : seat.label });
  }
  return [...rows.entries()]
    // A…Z before AA, and labels with no row letter last.
    .sort(([a], [b]) => (!a) - (!b) || a.length - b.length || a.localeCompare(b))
    .map(([row, items]) => [row, items.sort((a, b) => byLabel(a.label, b.label))]);
}

function buildMap(show) {
  seatEls.clear();
  $('seat-map').replaceChildren(...seatRows(show.seats).map(([row, items]) => {
    const buttons = items.map(({ label, text }) => {
      const el = h('button', { type: 'button', onclick: () => toggleSeat(label) }, text);
      seatEls.set(label, el);
      return el;
    });
    return row
      ? h('div', { class: 'seat-row' },
        h('span', { class: 'row-label', 'aria-hidden': 'true' }, row), ...buttons,
        h('span', { class: 'row-label', 'aria-hidden': 'true' }, row))
      : h('div', { class: 'seat-row loose' }, ...buttons);
  }));
}

function paintSeats(show) {
  const onSale = show.status === 'on_sale';
  for (const seat of show.seats) {
    const el = seatEls.get(seat.label);
    const selected = state.selected.has(seat.label);
    let kind = 'available';
    let text = 'available';
    if (seat.status !== 'available' && state.mine.has(seat.label)) {
      kind = 'mine';
      text = seat.status === 'held' ? 'held by you' : 'booked by you';
    } else if (seat.status === 'confirmed') {
      kind = text = 'sold';
    } else if (seat.status === 'held') {
      kind = 'held';
      text = `held until ${new Date(seat.held_until).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
    } else if (selected) {
      kind = text = 'selected';
    }
    const ownPrice = seat.price_paise !== show.price_paise || seat.section;
    el.className = `seat ${kind}${ownPrice ? ' tier' : ''}`;
    el.disabled = !onSale || (kind !== 'available' && kind !== 'selected');
    el.setAttribute('aria-pressed', String(selected));
    const description = [seat.label, seat.section, money(seat.price_paise, show.currency), text]
      .filter(Boolean).join(' · ');
    el.title = description;
    el.setAttribute('aria-label', description);
  }
}

function renderShow() {
  const show = state.show;
  $('show-empty').hidden = Boolean(show);
  $('show-view').hidden = !show;
  if (!show) return;

  $('show-name').textContent = show.name;
  $('show-meta').textContent = `${show.event_kind} · ${money(show.price_paise, show.currency)} per seat`
    + (show.status === 'on_sale' ? '' : ` · ${STATUS_TEXT[show.status]}`);
  $('screen').textContent = show.event_kind === 'cinema' ? 'Screen' : 'Stage';
  const { available, held, confirmed, total } = show.counts;
  $('show-counts').replaceChildren(
    ...[['Available', available], ['Held', held], ['Sold', confirmed], ['Total', total]]
      .map(([name, value]) => h('div', {}, h('dt', {}, name), h('dd', {}, String(value)))));

  const mapKey = `${show.show_id}:${show.seats.length}`;
  if (builtMapFor !== mapKey) {
    buildMap(show);
    builtMapFor = mapKey;
  }
  paintSeats(show);
  renderSummary();
}

function renderSummary() {
  const show = state.show;
  const labels = [...state.selected].sort(byLabel);
  const prices = new Map(show.seats.map((seat) => [seat.label, seat.price_paise]));
  const total = labels.reduce((sum, label) => sum + prices.get(label), 0);
  $('selection-line').replaceChildren(...(labels.length
    ? [`${labels.join(', ')} · `, h('strong', {}, money(total, show.currency))]
    : ['Select seats on the map.']));
  const mine = show.seats.filter((seat) => seat.status !== 'available' && state.mine.has(seat.label)).length;
  $('limit-line').textContent = `Up to ${show.per_user_limit} seats per person`
    + (mine ? ` · you have ${mine}` : '');
  const idle = !labels.length || state.busy || show.status !== 'on_sale';
  $('btn-book').disabled = idle;
  $('btn-hold').disabled = idle;
  $('btn-book').textContent = state.busy ? 'Working…' : 'Book now';
  $('btn-hold').textContent = `Hold for ${duration(show.hold_ttl_seconds)}`;
}

function toggleSeat(label) {
  const show = state.show;
  if (state.busy) return;
  if (state.selected.has(label)) {
    state.selected.delete(label);
  } else if (state.selected.size >= show.per_user_limit) {
    toast(`This show allows ${show.per_user_limit} seats per person.`);
    return;
  } else {
    state.selected.add(label);
  }
  paintSeats(show);
  renderSummary();
}

async function reserve(hold) {
  const show = state.show;
  const seats = [...state.selected].sort(byLabel);
  if (!seats.length || state.busy) return;
  const signature = [show.show_id, hold ? 'hold' : 'book', ...seats].join('|');
  if (!state.attempt || state.attempt.signature !== signature) {
    state.attempt = { signature, key: newKey() };
  }
  state.busy = true;
  renderSummary();
  try {
    if (!state.session) await startGuest();
    const reservation = await api('POST', `/shows/${show.show_id}/reserve`, {
      body: hold ? { seats, hold_ttl_seconds: show.hold_ttl_seconds } : { seats },
      headers: { 'Idempotency-Key': state.attempt.key },
    });
    state.attempt = null;
    state.selected.clear();
    toast(reservation.status === 'held'
      ? `${listOf(seats)} held. Confirm under My bookings before the hold runs out.`
      : `${listOf(seats)} booked.`, 'ok');
  } catch (error) {
    if (!(error instanceof ApiError)) throw error;
    if (error.code === 'SEAT_TAKEN') {
      const conflicts = (error.details && error.details.conflicts) || [];
      conflicts.forEach((label) => state.selected.delete(label));
      toast(conflicts.length
        ? `${listOf(conflicts)} ${conflicts.length > 1 ? 'were' : 'was'} just taken. Nothing was booked.`
        : 'Those seats are busy right now. Try again.', 'info', error.requestId);
    } else if (error.code === 'NETWORK') {
      // The key is kept, so pressing again replays this attempt instead of booking twice.
      toast('No answer from the service. Your selection is kept, and pressing again is safe: it cannot book twice.', 'error');
    } else {
      fail(error);
    }
  } finally {
    state.busy = false;
    await Promise.all([loadMine().then(refreshShow), loadBookings()]);
    renderShow();
  }
}

/* ---------- bookings ---------- */

async function loadBookings(more = false) {
  if (!state.session) return;
  const query = more && state.bookingsCursor ? `?cursor=${encodeURIComponent(state.bookingsCursor)}` : '';
  let page;
  try {
    page = await api('GET', `/reservations${query}`, { quiet: !more });
  } catch (error) {
    if (!(error instanceof ApiError)) throw error;
    if (more) fail(error);
    return;
  }
  state.bookings = more ? state.bookings.concat(page.items) : page.items;
  state.bookingsCursor = page.next_cursor;
  renderBookings();
  const unnamed = [...new Set(page.items.map((item) => item.show_id))].filter((id) => !showNames.has(id));
  if (unnamed.length) {
    await Promise.all(unnamed.map((id) => api('GET', `/shows/${id}`, { quiet: true })
      .then((show) => showNames.set(id, show.name))
      .catch(() => {})));
    renderBookings();
  }
}

const secondsLeft = (booking) => Math.ceil((Date.parse(booking.expires_at) - now()) / 1000);

function countdown(seconds) {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}

function renderBookings() {
  $('bookings-empty').hidden = state.bookings.length > 0;
  $('btn-more-bookings').hidden = !state.bookingsCursor;
  $('booking-list').replaceChildren(...state.bookings.map((booking) => {
    const held = booking.status === 'held';
    const act = (verb) => h('button', {
      class: `btn small${verb === 'confirm' ? ' primary' : ''}`,
      type: 'button',
      onclick: (event) => settle(booking, verb, event.currentTarget),
    }, verb === 'confirm' ? 'Confirm' : 'Cancel');
    return h('li', { class: 'booking' },
      h('div', {},
        h('button', { class: 'link', type: 'button', onclick: () => openShow(booking.show_id).catch(fail) },
          showNames.get(booking.show_id) || 'Show'),
        h('div', { class: 'muted small' },
          `${booking.seats.join(', ')} · ${money(booking.amount_paise, booking.currency)}`)),
      h('div', { class: 'booking-side' },
        held && h('span', { class: 'countdown', 'data-expires': booking.expires_at },
          countdown(Math.max(secondsLeft(booking), 0))),
        h('span', { class: `badge ${booking.status}` }, STATUS_TEXT[booking.status]),
        held && act('confirm'),
        held && act('cancel')));
  }));
}

async function settle(booking, verb, button) {
  button.disabled = true;
  try {
    await api('POST', `/reservations/${booking.reservation_id}/${verb}`);
    toast(verb === 'confirm' ? `${listOf(booking.seats)} confirmed.` : 'Hold cancelled.', 'ok');
  } catch (error) {
    fail(error);
  }
  await Promise.all([loadBookings(), loadMine().then(refreshShow)]);
  if (state.show) renderShow();
}

function tick() {
  const lapsed = state.bookings.filter((booking) => booking.status === 'held' && secondsLeft(booking) <= 0);
  if (lapsed.length) {
    lapsed.forEach((booking) => { booking.status = 'expired'; });
    renderBookings();
    loadMine().then(refreshShow);
    return;
  }
  for (const el of document.querySelectorAll('[data-expires]')) {
    el.textContent = countdown(Math.max(secondsLeft({ expires_at: el.dataset.expires }), 0));
  }
}

async function poll() {
  if (polling || document.hidden || state.busy || !state.show) return;
  polling = true;
  try {
    await refreshShow();
  } finally {
    polling = false;
  }
}

/* ---------- admin ---------- */

function patternSeats(form) {
  const last = form.elements.last_row.value.toUpperCase().charCodeAt(0);
  const perRow = Number(form.elements.per_row.value);
  const seats = [];
  for (let code = 65; code <= last && code <= 90; code += 1) {
    for (let number = 1; number <= perRow; number += 1) seats.push(String.fromCharCode(code) + number);
  }
  return seats;
}

function renderAdminPreview() {
  const seats = patternSeats($('admin-form'));
  $('admin-preview').textContent = seats.length
    ? `${seats.length} seats: ${seats[0]} to ${seats.at(-1)}`
    : 'Enter a row letter and a seat count.';
}

async function createShow(event) {
  event.preventDefault();
  const form = $('admin-form');
  const submit = form.querySelector('[type=submit]');
  submit.disabled = true;
  try {
    const show = await api('POST', '/shows', {
      body: {
        name: form.elements.name.value.trim(),
        seats: patternSeats(form),
        price_paise: Math.round(Number(form.elements.price.value) * 100),
      },
    });
    form.elements.name.value = '';
    toast(`${show.name} created.`, 'ok');
    await loadShows();
    await openShow(show.show_id);
  } catch (error) {
    fail(error);
  } finally {
    submit.disabled = false;
  }
}

/* ---------- start ---------- */

async function init() {
  $('btn-signin').addEventListener('click', () => openAuth('signin'));
  $('btn-register').addEventListener('click', () => openAuth('register'));
  $('btn-signout').addEventListener('click', () => signOut());
  $('auth-cancel').addEventListener('click', () => $('auth-dialog').close());
  $('auth-form').addEventListener('submit', submitAuth);
  $('btn-more-shows').addEventListener('click', () => loadShows(true).catch(fail));
  $('btn-more-bookings').addEventListener('click', () => loadBookings(true));
  $('btn-book').addEventListener('click', () => reserve(false));
  $('btn-hold').addEventListener('click', () => reserve(true));
  $('admin-form').addEventListener('submit', createShow);
  $('admin-form').addEventListener('input', renderAdminPreview);
  renderAdminPreview();

  setSession(restoreSession());
  if (!state.session) await startGuest().catch(fail);

  await loadShows().catch(fail);
  const wanted = location.hash.slice(1) || (state.shows[0] && state.shows[0].show_id);
  if (wanted) {
    await openShow(wanted).catch(() => history.replaceState(null, '', location.pathname));
  }
  await loadBookings();
  // A restored token may have been replaced while the bookings loaded.
  await loadMine();
  renderShow();

  setInterval(poll, POLL_MS);
  setInterval(tick, 1000);
  document.addEventListener('visibilitychange', poll);
}

init();
