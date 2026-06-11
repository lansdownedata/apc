# All Pro Charter — Lead Manager (design prototype)

A self-contained, high-fidelity front-end prototype of the **Lead Manager** — All Pro
Charter's custom portal that captures every lead, builds a quote with multiple
reservations, and books it into LimoAnywhere. It is for **design review only**: there is
no backend, and all data is hard-coded sample data that resets on reload.

## Open it
Double-click **`index.html`** — it runs in any modern browser, no build step. (An internet
connection is needed the first time so Tailwind, Alpine, the fonts and icons load from
their CDNs.)

The whole portal is one page; the left nav switches views instantly. You can also deep-link
a view with a hash: `index.html#inbox`, `#pipeline`, `#contacts`, `#reviews`, `#settings`.

## What's inside (7 views)
- **Leads & Quotes** — sortable, filterable table of every lead. Channel-coded avatars, a
  reservation-count chip, status pills and tabular quote totals. **Click any row** to open
  the quote workspace.
- **Quote workspace** — the core. A lead **is** one quote holding **multiple reservations**.
  Add / edit / duplicate / remove reservations with a live total; the Podium conversation,
  an activity / touch-point timeline, and a **Payments panel** sit alongside.
  - **Send quote + 50% deposit request** → New → Quoted (Podium pay link).
  - **Deposit paid → auto-books**: card saved on file, the LimoAnywhere hand-off runs (*Find /
    Create Account → Create Quote Request → one Create Reservation per reservation*), and the
    **balance is scheduled** for 30 days before pickup.
  - **Balance charge** succeeds, or **fails → a notification on the lead** + the bell, with
    Retry / Request-new-card actions.
- **Inbox** — Podium-style conversation list → thread → lead-summary rail, with a reply
  composer and the "Synced to LimoAnywhere" strip.
- **Pipeline** — New → Quoted → Booked → Lost kanban; **drag a card** to change status.
- **Contacts**, **Reviews** (Podium invites + ratings), **Settings** (capture channels,
  integrations, **payments**, users & roles, branding).

## Trip-type logic (in the reservation editor)
- **Transfer** — point-to-point, flat base rate.
- **Hourly** — as-directed, priced `max(hours, minimum) × hourly rate`, with a minimum-hours flag.
- **Multi-stop** — an ordered route (pickup → stop(s) → drop-off), available on either type.

## Trip status (operational — separate from the sales pipeline)
- After booking, each reservation shows a **trip status** mirroring LimoAnywhere's exact dispatch
  taxonomy (Unassigned → Assigned → On The Way → Arrived → Customer In Car → Done, plus
  Cancelled / No Show and the affiliate / farm-out states), grouped by phase.
- Click the badge for the grouped LA picker; setting **Done** schedules the post-trip review
  request. (Synced from LimoAnywhere; editable in-portal for off-LA affiliate trips.)

## Merchant processing (Stripe)
- Quotes go out with a **50% deposit request**; paying it **auto-books** the lead and **schedules
  the balance** (the other 50%) for **30 days before pickup**, charged off-session against the
  saved card.
- A failed balance charge raises a **notification on the lead** (top-bar bell) + a workspace
  alert, with **Retry** / **Request new card** actions.
- The real balance charge is time-based, so the Payments panel includes demo controls to
  **simulate the scheduled attempt (succeed / fail)**. **Olivia is seeded with a failed balance**
  so the failure + notification state is visible on load.

## Files
- `index.html` — the app shell + all seven views.
- `app.js` — the in-memory data store, pricing/filter helpers, and all interactions.
- `styles.css` — the design system (fonts, brand tokens, paper grain, animations, scrollbars).

## Stack (matches the planned build)
- **Tailwind CSS** (Play CDN) · **Alpine.js** for interactivity
- **Fraunces** (display) + **Hanken Grotesk** (UI) + **Spline Sans Mono** (figures) · **Tabler** icons

The production build is **Django + DRF** serving these screens as templates with the same
Alpine + Tailwind front end, **Zapier REST Hooks → LimoAnywhere** for the booking sync, and
the **Podium API** for messaging. Each view here is written to lift cleanly into a template.

## Brand
Deep charcoal `#14181f` frame, champagne gold `#c2a14e` accent, warm paper `#f4f1ea` canvas,
served from `leads.allprocharter.com`. Swap in the exact brand hex and the APC logo when
they're confirmed.
