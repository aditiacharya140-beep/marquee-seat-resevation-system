# Frontend

A small web page for the seat reservation service: browse shows, pick seats on a seat map, book them, and manage bookings. Clean, deep-navy theme. **Built** on the `stage-4-frontend` branch as planned below; what differs from the plan is listed under [As built](#as-built).

## The short answers

- **Can we?** Yes. Every screen below maps onto an endpoint that already exists.
- **How does it get a URL?** It is served by the same service that serves the API, so the URL is the one you already have: `https://seat-reservation-vw5k.onrender.com/`. No second deployment, no new account, nothing new to configure.
- **How long?** About an hour to build and test, plus one merge and redeploy.

## How it is deployed

**Chosen: serve the page from the existing service.**

The page is three static files — HTML, CSS, JavaScript — placed inside `app/static/` and served by FastAPI itself. Merging to `main` redeploys the service on Render exactly as today, and the page is live at the root of the same URL.

Why this way:

| | Same service (chosen) | Separate static site |
|---|---|---|
| URL to submit | The existing one | A second URL |
| Setup | None | A new Render static site, plus cross-origin (CORS) settings on the API |
| Can break | Nothing new | CORS misconfiguration, two deploys drifting apart |
| Docker image | Already copies `app/`, so the files ship automatically | Not involved |

The separate-site route is only worth it if the page later grows into a real application with its own build pipeline.

**No build step.** Plain HTML, CSS and JavaScript — no Node, no framework, no bundler. Nothing to install, nothing that can fail to build on Render, and the container image is unchanged apart from three files.

## What the page does

One page, four areas.

### 1. Header
Service name, and who you are: "Guest" or your email. Buttons: **Sign in**, **Register**, **Sign out**.

A visitor is given a guest session automatically on first load, so they can book immediately — the same path the API is designed around.

### 2. Shows
A list of shows, newest first: name, price, number of seats. "Load more" for the next page. Click a show to open it.

### 3. Seat map — the main screen
- Seats drawn as a grid, grouped into rows by the letters at the start of the label (`A1 … A12` is row A).
- Colour by state: **available**, **held**, **sold**, and **your selection**.
- Seats with their own price show it; premium sections are marked.
- A summary bar: seats selected, total price, the per-person limit for this show.
- Two actions: **Book now** (confirms immediately) and **Hold for 2 minutes** (reserves, then needs confirming).
- The map refreshes every few seconds, so seats taken by someone else change colour without a reload.
- Counts line: available / held / sold / total — the same numbers the API guarantees always add up.

### 4. My bookings
The signed-in person's reservations: seats, amount, status. A held booking shows a live countdown with **Confirm** and **Cancel** buttons. An expired hold reads "expired".

### Admin (only when signed in as admin)
A small form: show name, price, and seats entered as a quick pattern (rows `A–E`, seats `1–10`) — creates a show.

## How each screen talks to the API

| Screen action | Endpoint |
|---|---|
| First visit | `POST /auth/guest` |
| Register / sign in | `POST /auth/register`, `POST /auth/login` |
| Keep a signed-in session alive | `POST /auth/refresh` when a request answers 401 |
| Turn a guest into an account, keeping bookings | `POST /auth/upgrade` |
| List shows | `GET /shows?limit=…&cursor=…` |
| Open a show, refresh the seat map | `GET /shows/{id}` |
| Book now / hold | `POST /shows/{id}/reserve` (with or without `hold_ttl_seconds`) |
| Confirm, cancel | `POST /reservations/{id}/confirm`, `/cancel` |
| My bookings | `GET /reservations` |
| Create a show | `POST /shows` |

## Behaviour that must be right

These are where a booking page usually goes wrong, and where this one must use what the API already provides.

- **A retry must never double-book.** The page generates one idempotency key per booking attempt and reuses it if the request is retried after a timeout. A second key is only made when the person changes their selection.
- **Losing a race is normal, not an error.** `409 SEAT_TAKEN` names the seats that were taken; the page deselects those seats, refreshes the map, and says "A12 was just taken" — not "something went wrong".
- **The limit is explained.** `409 PER_USER_LIMIT` shows how many seats the person already has and what the limit is.
- **Throttling is respected.** On `429` the page waits the time the `Retry-After` header says, and tells the person it is waiting.
- **Tokens stay in the browser tab** (session storage), never in the URL. Signing out clears them.
- **Every error shows its request id** in small print, so a problem can be traced in the logs.

## Look and feel

| Element | Choice |
|---|---|
| Background | Deep navy `#0b1220`, panels `#111a2e` |
| Text | Soft white `#e6ecf5`, muted `#8fa0bd` |
| Accent | Bright blue `#3b82f6` for buttons and your selection |
| Seat colours | Available: outlined navy · Selected: bright blue · Held: amber · Sold: dim grey |
| Type | The system font; no web fonts to load |
| Layout | Single column on a phone, two columns (shows · seat map) on a wide screen |

Seat state is never shown by colour alone: sold seats are also crossed, held seats carry a small clock mark, so the map is readable for colour-blind visitors.

## Changes needed in the service

Small, and all in the existing codebase.

1. **Serve the files.** Add `app/static/` and mount it; `GET /` returns the page.
2. **Exempt the page's own files from rate limiting.** Otherwise loading the page spends the visitor's read allowance — and while the proxy-hop setting on Render is wrong, every visitor shares one allowance.
3. **Tests:** `/` returns the page; the static files are served; they are not rate limited; an unknown path still returns the `ROUTE_NOT_FOUND` envelope.
4. **Docs:** README gets the link and a screenshot; the middleware and API documents get one line each.

Nothing in the booking logic changes.

## As built

- **"Yours" is a fifth seat state.** The API does not say who holds a seat, so the page reads the person's own active reservations for the open show and draws those seats green with a tick, instead of as held or sold.
- **A failed request is not retried automatically.** After a timeout the selection and its idempotency key are kept and the person is told that pressing again is safe. Only a `429` is waited out and retried, with the same key.
- **A guest whose token expires continues as a new guest**, and is told so; a guest has no refresh token, so its earlier bookings are no longer visible from the page.
- **Countdowns use the server's clock**, taken from the `Date` header, so a wrong clock on the device does not show a hold as live after it lapsed.
- **The admin pattern is rows `A` to a chosen letter and a number of seats per row.** Per-seat prices and sections still need the API.
- **The open show is kept in the URL fragment**, so a reload returns to it. Tokens are never in the URL.

## What it will not do

- **No payment screen.** The API has no payment step, so the page cannot add one. "Book now" confirms outright.
- **No show editing or closing.** The API has no endpoint for it.
- **No seat-map designer.** Seats are laid out from their labels; an irregular hall will look like a plain grid.
- **No offline mode, no notifications.**

## Before this is useful on the live site

The rate-limit setting on Render had to be corrected first (`RATE_LIMIT_TRUSTED_PROXY_HOPS`): at its old value every visitor shared one sign-in and guest allowance. It is now 3, established by test (LEARN-019).

## Steps to ship

1. Fix the Render setting above and confirm it with a test request. Done (LEARN-019).
2. Build the three files and the two small service changes on the `stage-4-frontend` branch.
3. Run the test suite and try the page locally against a local database.
4. Merge to `main`; Render redeploys.
5. Open `https://seat-reservation-vw5k.onrender.com/` and walk through: guest booking, a hold and confirm, a cancel, and a seat lost to a second browser window.
6. Submit that URL.
