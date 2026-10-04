'use strict';

const POLL_MS = 4000;
const REQUEST_TIMEOUT_MS = 10000;
const MAX_THROTTLE_RETRIES = 2;
const TOAST_MS = 6000;
const SESSION_KEY = 'seatres.session';
// Shows the load test (burst/burst.py) leaves behind are not part of the programme.
const LOAD_TEST_PREFIX = 'burst-';
const MAX_SHOW_PAGES = 10;
const LONG_ROW = 26;
const AISLE_MIN_ROW = 14;
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
// The booking a signed-out person asked for, carried out once they have an account.
let pendingBooking = null;

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

// A show's name carries its showtime and screen after the title: "Title · when · where".
function showParts(name) {
  const [title, ...meta] = name.split(' · ');
  return { title, meta };
}

function poster(name, size = '') {
  const { title } = showParts(name);
  let hue = 0;
  for (const char of title) hue = (hue * 31 + char.codePointAt(0)) % 360;
  const initials = title.split(/\s+/).slice(0, 2).map((word) => word[0] || '').join('').toUpperCase();
  return h('span', { class: `poster ${size}`, style: `--hue: ${hue}`, 'aria-hidden': 'true' }, initials);
}

const isLoadTest = (name) => Boolean(name) && name.startsWith(LOAD_TEST_PREFIX);

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
  // Another person's bookings must not stay on screen, even if reloading them fails.
  if (!session || !state.session || session.user_id !== state.session.user_id) {
    state.bookings = [];
    state.bookingsCursor = null;
    state.mine = new Set();
    renderBookings();
    if (state.show) renderShow();
  }
  state.session = session;
  try {
    if (session) sessionStorage.setItem(SESSION_KEY, JSON.stringify(session));
    else sessionStorage.removeItem(SESSION_KEY);
  } catch { /* storage unavailable: the session lasts until reload */ }
  renderAccount();
}

function restoreSession() {
  try {
    const session = JSON.parse(sessionStorage.getItem(SESSION_KEY));
    // Booking needs an account, so a guest session has nothing to restore.
    return session && !session.is_guest ? session : null;
  } catch {
    return null;
  }
}

// Concurrent requests that all met a 401 share one recovery.
function recoverSession(staleToken) {
  if (!state.session || state.session.access_token !== staleToken) {
    return Promise.resolve(Boolean(state.session));
  }
  recovering ??= (async () => {
    try {
      const fresh = await api('POST', '/auth/refresh', {
        auth: false, body: { refresh_token: state.session.refresh_token },
      });
      setSession({ ...state.session, access_token: fresh.access_token });
      return true;
    } catch (error) {
      // Only a refused refresh ends the session; a network failure leaves it to retry.
      if (error instanceof ApiError && error.code === 'UNAUTHENTICATED') {
        setSession(null);
        toast('Your session expired. Sign in again to see your tickets.');
      }
      return false;
    } finally {
      recovering = null;
    }
  })();
  return recovering;
}

function renderAccount() {
  const session = state.session;
  $('who').textContent = session ? session.email : '';
  $('btn-signin').hidden = Boolean(session);
  $('btn-register').hidden = Boolean(session);
  $('btn-signout').hidden = !session;
  $('admin-panel').hidden = !(session && session.role === 'admin');
  $('bookings-empty').textContent = session ? 'Nothing booked yet.' : 'Sign in to see your tickets.';
}

async function sessionChanged(keepSelection = false) {
  if (!keepSelection) state.selected.clear();
  state.attempt = null;
  await Promise.all([loadBookings(), loadMine()]);
  renderShow();
}

function openAuth(mode) {
  authMode = mode;
  const registering = mode === 'register';
  $('auth-title').textContent = registering ? 'Create account' : 'Sign in';
  $('auth-submit').textContent = registering ? 'Register' : 'Sign in';
  $('auth-switch').textContent = registering ? 'Already have an account? Sign in' : 'New here? Create an account';
  $('auth-note').textContent = pendingBooking
    ? 'An account is needed to book, so your tickets stay yours on any device. Your selection is kept while you do this.'
    : 'Your tickets are kept with your account, on any device.';
  $('auth-form').elements.password.autocomplete = registering ? 'new-password' : 'current-password';
  $('auth-error').hidden = true;
  if (!$('auth-dialog').open) $('auth-dialog').showModal();
}

async function submitAuth(event) {
  event.preventDefault();
  const form = $('auth-form');
  const body = { email: form.elements.email.value.trim(), password: form.elements.password.value };
  $('auth-submit').disabled = true;
  try {
    setSession(await api('POST', authMode === 'signin' ? '/auth/login' : '/auth/register', { body, auth: false }));
    // Closing the dialog drops a pending booking, so it is taken first.
    const resume = pendingBooking;
    form.reset();
    $('auth-dialog').close();
    toast(authMode === 'signin' ? 'Signed in.' : 'Account created.', 'ok');
    await sessionChanged(Boolean(resume));
    if (resume) await reserve(resume.hold);
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
  await sessionChanged();
}

/* ---------- shows ---------- */

async function loadShows(more = false) {
  let cursor = more ? state.showsCursor : null;
  const found = [];
  // A page can be nothing but load-test shows, so read on until one has something to list.
  for (let page = 0; page < MAX_SHOW_PAGES; page += 1) {
    const data = await api('GET', `/shows${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`);
    cursor = data.next_cursor;
    for (const show of data.items) showNames.set(show.show_id, show.name);
    found.push(...data.items.filter((show) => !isLoadTest(show.name)));
    if (found.length || !cursor) break;
  }
  state.shows = more ? state.shows.concat(found) : found;
  state.showsCursor = cursor;
  renderShows();
}

function renderShows() {
  const open = state.show && state.show.show_id;
  $('shows-empty').hidden = state.shows.length > 0;
  $('btn-more-shows').hidden = !state.showsCursor;
  $('show-list').replaceChildren(...state.shows.map((show) => {
    const { title, meta } = showParts(show.name);
    return h('li', {},
      h('button', {
        class: `show-item${show.show_id === open ? ' active' : ''}`,
        type: 'button',
        'aria-current': show.show_id === open ? 'true' : null,
        onclick: () => openShow(show.show_id).catch(fail),
      },
        poster(show.name),
        h('span', { class: 'show-text' },
          h('span', { class: 'name' }, title),
          h('span', { class: 'muted small' }, meta.join(' · ') || `${show.total_seats} seats`),
          h('span', { class: 'muted small' },
            show.status === 'on_sale' ? money(show.price_paise, show.currency) : STATUS_TEXT[show.status]))));
  }));
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
    rows.get(row).push({ seat, number: match ? match[2] : seat.label });
  }
  return [...rows.entries()]
    // A…Z before AA, and labels with no row letter last.
    .sort(([a], [b]) => (!a) - (!b) || a.length - b.length || a.localeCompare(b))
    .map(([row, items]) => ({ row, items: items.sort((a, b) => byLabel(a.seat.label, b.seat.label)) }));
}

function buildMap(show) {
  seatEls.clear();
  const rows = seatRows(show.seats);
  // A row whose seats share one section and price sits under a heading for that tier.
  const tiers = rows.map(({ items }) => {
    const { section, price_paise: price } = items[0].seat;
    const uniform = items.every(({ seat }) => seat.section === section && seat.price_paise === price);
    return uniform ? `${section || 'Standard'} · ${money(price, show.currency)}` : null;
  });
  const headed = new Set(tiers.filter(Boolean)).size > 1;
  const children = [];
  let marked = false;
  rows.forEach(({ row, items }, index) => {
    if (headed && tiers[index] && tiers[index] !== tiers[index - 1]) {
      children.push(h('div', { class: 'tier-head' }, tiers[index]));
    }
    // A hall whose labels do not form short lettered rows is drawn as a wrapped grid.
    const loose = !row || items.length > LONG_ROW;
    const quarter = Math.round(items.length / 4);
    const buttons = items.map(({ seat, number }, position) => {
      const ownPrice = !tiers[index] && (seat.price_paise !== show.price_paise || Boolean(seat.section));
      marked ||= ownPrice;
      const el = h('button', {
        type: 'button',
        'data-tier': ownPrice,
        'data-aisle': !loose && items.length >= AISLE_MIN_ROW
          && (position === quarter || position === items.length - quarter),
        onclick: () => toggleSeat(seat.label),
      }, loose ? seat.label : number);
      seatEls.set(seat.label, el);
      return el;
    });
    children.push(loose
      ? h('div', { class: 'seat-row loose' }, ...buttons)
      : h('div', { class: 'seat-row' },
        h('span', { class: 'row-label', 'aria-hidden': 'true' }, row), ...buttons,
        h('span', { class: 'row-label', 'aria-hidden': 'true' }, row)));
  });
  $('legend-tier').hidden = !marked;
  $('seat-map').replaceChildren(...children);
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
    el.className = `seat ${kind}`;
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

  const { title, meta } = showParts(show.name);
  const cheapest = Math.min(...show.seats.map((seat) => seat.price_paise));
  $('show-poster').replaceChildren(poster(show.name, 'large'));
  $('show-name').textContent = title;
  $('show-meta').replaceChildren(...[
    ...meta,
    `From ${money(cheapest, show.currency)}`,
    show.status === 'on_sale' ? null : STATUS_TEXT[show.status],
  ].filter(Boolean).map((text) => h('span', { class: 'chip' }, text)));
  $('screen').textContent = show.event_kind === 'cinema' ? 'Screen this way' : 'Stage';
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
    ? [`${labels.length} seat${labels.length > 1 ? 's' : ''} · ${labels.join(', ')} · `,
      h('strong', {}, money(total, show.currency))]
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
  if (!state.session) {
    pendingBooking = { hold };
    openAuth('register');
    return;
  }
  const signature = [show.show_id, hold ? 'hold' : 'book', ...seats].join('|');
  if (!state.attempt || state.attempt.signature !== signature) {
    state.attempt = { signature, key: newKey() };
  }
  state.busy = true;
  renderSummary();
  try {
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
    } else if (error.code === 'UNAUTHENTICATED') {
      pendingBooking = { hold };
      openAuth('signin');
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
  // Names first: a booking is drawn, or left out, by the show it is for.
  const unnamed = [...new Set(page.items.map((item) => item.show_id))].filter((id) => !showNames.has(id));
  await Promise.all(unnamed.map((id) => api('GET', `/shows/${id}`, { quiet: true })
    .then((show) => showNames.set(id, show.name))
    .catch(() => {})));
  renderBookings();
}

const secondsLeft = (booking) => Math.ceil((Date.parse(booking.expires_at) - now()) / 1000);

function countdown(seconds) {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}

function renderBookings() {
  // Bookings on a load-test show are no more part of the programme than the show is.
  const bookings = state.bookings.filter((booking) => !isLoadTest(showNames.get(booking.show_id)));
  $('bookings-empty').hidden = bookings.length > 0;
  $('btn-more-bookings').hidden = !state.bookingsCursor;
  $('booking-list').replaceChildren(...bookings.map((booking) => {
    const held = booking.status === 'held';
    // The owner can cancel a confirmed booking as well as a live hold.
    const cancellable = held || booking.status === 'confirmed';
    const act = (verb) => h('button', {
      class: `btn small${verb === 'confirm' ? ' primary' : ''}`,
      type: 'button',
      onclick: (event) => settle(booking, verb, event.currentTarget),
    }, verb === 'confirm' ? 'Confirm' : 'Cancel');
    const name = showNames.get(booking.show_id) || 'Show';
    const { title, meta } = showParts(name);
    const fact = (label, value) => h('div', {}, h('dt', {}, label), h('dd', {}, value));
    return h('li', { class: `ticket ${booking.status}` },
      poster(name),
      h('div', { class: 'ticket-body' },
        h('button', { class: 'link', type: 'button', onclick: () => openShow(booking.show_id).catch(fail) },
          title),
        meta.length > 0 && h('div', { class: 'muted small' }, meta.join(' · ')),
        h('dl', { class: 'facts' },
          fact('Seats', booking.seats.join(', ')),
          fact('Amount', money(booking.amount_paise, booking.currency)),
          fact('Booking ID', booking.reservation_id.slice(0, 8).toUpperCase()))),
      h('div', { class: 'ticket-side' },
        held && h('span', { class: 'countdown', 'data-expires': booking.expires_at },
          countdown(Math.max(secondsLeft(booking), 0))),
        h('span', { class: `badge ${booking.status}` }, STATUS_TEXT[booking.status]),
        held && act('confirm'),
        cancellable && act('cancel')));
  }));
}

async function settle(booking, verb, button) {
  // A confirmed booking is given up for good, so that one is asked about first.
  if (verb === 'cancel' && booking.status === 'confirmed'
      && !window.confirm(`Cancel your booking for ${listOf(booking.seats)}? The seats go back on sale.`)) {
    return;
  }
  button.disabled = true;
  try {
    await api('POST', `/reservations/${booking.reservation_id}/${verb}`);
    const cancelled = booking.status === 'confirmed'
      ? 'Booking cancelled. The seats are available again.' : 'Hold cancelled.';
    toast(verb === 'confirm' ? `${listOf(booking.seats)} confirmed.` : cancelled, 'ok');
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
  $('auth-switch').addEventListener('click', () => openAuth(authMode === 'signin' ? 'register' : 'signin'));
  $('auth-dialog').addEventListener('close', () => { pendingBooking = null; });
  $('auth-form').addEventListener('submit', submitAuth);
  $('btn-more-shows').addEventListener('click', () => loadShows(true).catch(fail));
  $('btn-more-bookings').addEventListener('click', () => loadBookings(true));
  $('btn-book').addEventListener('click', () => reserve(false));
  $('btn-hold').addEventListener('click', () => reserve(true));
  $('admin-form').addEventListener('submit', createShow);
  $('admin-form').addEventListener('input', renderAdminPreview);
  renderAdminPreview();

  setSession(restoreSession());

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
