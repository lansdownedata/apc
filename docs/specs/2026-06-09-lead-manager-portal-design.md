# All Pro Charter — Lead Manager Portal · Design Spec

**Prepared:** 2026-06-09 · for All Pro Charter (Lansdowne Data)
**Status:** Approved direction — pending spec review
**Supersedes:** `APC_Lead_Manager_Mockup.html` and the current `html_designs/{index,inbox}.html` prototype

---

## 1. Purpose

Elevate the existing basic mock-up into a **high-grade, enterprise-quality, clickable front-end prototype** of the All Pro Charter Lead Manager — the custom portal that captures every lead, builds a quote with multiple reservations, and books it into LimoAnywhere. This is a **design prototype** (hard-coded sample data, no backend) used to review and refine the UX before the Django build. It must map cleanly onto the planned production stack (Django templates + Tailwind + Alpine).

**Emphasis (confirmed):** visual polish & brand first; the quote → reservations → booking workspace is fully interactive; surrounding screens are polished and clickable.

### Goals
- A cohesive, branded, **full portal** (7 screens) that feels like a real enterprise SaaS product.
- An **interactive quote builder** where a lead = one quote holding **multiple reservations**, each with **transfer / hourly** trip logic and **multi-stop** routing.
- Make the To-Be architecture tangible: "Mark booked" plays the **Find/Create Account → Create Quote Request → Create Reservation ×N** sync into LimoAnywhere.

### Non-goals
- No backend, real API calls, auth, or persistence (data resets on reload).
- No real Podium/Zapier/LimoAnywhere integration — these are represented visually.
- Not the production Django app; it is the design reference for it.

---

## 2. Architecture

**Single-page app shell + external logic, all via CDN** (opens by double-click, no build step).

- **`index.html`** — the charcoal/gold shell (persistent sidebar + top bar), with each screen authored as a distinct view section toggled by Alpine `x-show`. View sections are written so each can later be lifted into a Django template.
- **`app.js`** — one mock data store (leads + reservations, conversations, contacts, pipeline) plus the Alpine component logic and pricing helpers. **Single source of truth** so a lead opened from the Inbox is the same record in Leads, Pipeline and the workspace.
- **CDN dependencies:** Tailwind (Play CDN) with an inline `tailwind.config` for the brand tokens, Alpine.js, Inter (Google Fonts), Tabler icons webfont.

**Why SPA shell over multi-file:** instant view switching (no white-flash reloads) reads as premium; shared state makes the booking/sync flow feel real end-to-end. View sections remain template-liftable, preserving the path to the Django build.

---

## 3. Data model (mock)

### Lead / Quote (one record)
`id, name, company?, initials, channel, phone, email, status, created, assignedAgent, notes, reservations[], conversation[]`

- **channel:** `Website | Wedding Pro | Phone | API`
- **status (pipeline):** `New → Quoted → Booked → Lost`
- **quoteNo:** derived (`Q-####`)
- **quote total:** Σ of reservation totals

### Reservation (line item — multiple per quote)
`id, tripType, service, date, time, vehicle, pax, stops[], pricing, laResId?`

- **tripType:** `transfer | hourly`
- **stops[]:** ordered route. First = **Pickup**, last = **Drop-off**, any in between = intermediate **stops**. `stops.length > 2` ⇒ **multi-stop**. Each stop: `{ address, note? }`.
- **pricing:**
  - **transfer** → `{ baseRate }` — flat, agent-set; multi-stop is shown and priced into the base.
  - **hourly** → `{ hours, hourlyRate, minHours }` — line total = `max(hours, minHours) × hourlyRate`; UI flags when the minimum is applied.
  - optional `surcharges[]` `{label, amount}`; gratuity & fees show as **"Calculated in LimoAnywhere."**
- **laResId:** mock `LA-####`, assigned on booking.

### Supporting entities
- **Conversation / Message:** `{ out:bool, text, time, channel }` — the Podium thread per lead.
- **Contact:** person/company, channel, lifetime value, last activity (derived from leads where practical).
- **Pipeline:** the leads grouped by status with per-column value.

### Sample data (showcases all three logics)
- **Sarah Reyes — Wedding Pro — Quoted:** R1 *Hourly* (vehicle stays, 6 hrs, 3-hr min) **multi-stop** (Ritz → photo stop → winery); R2 *Transfer* return (winery → Ritz).
- **James Tran — Website — New:** R1 *Transfer* airport (Arlington → IAD).
- **Denise Walker — Phone — Quoted:** R1 *Transfer* conference arrival (DCA → Marriott); R2 *Transfer* departure (Marriott → DCA). 2× 55-pax.
- **Olivia Grant — Wedding Pro — Booked:** R1 *Hourly* multi-stop (hotel → estate, photo stop), already synced to LA.
- **Marcus Kelly — Website — New:** R1 *Transfer* airport (Bethesda → DCA), Sprinter, 8 pax.

---

## 4. Screens

1. **Inbox** — Podium-style 3-pane: conversation list → thread → context rail. Reply composer ("sends via Podium"). "Synced to LimoAnywhere" strip. Deep-link to the lead's quote workspace.
2. **Leads & Quotes** — elevated data table: sticky header, sortable columns, channel-coded avatars, **reservation-count chip**, status pills, right-aligned **tabular** quote totals. Filter tabs (All / New / Quoted / Booked / Lost) + channel filter + search. Stat cards (New · Quoted · Booked · Open pipeline value).
3. **Lead / Quote workspace** (core) — full two-column view:
   - **Left:** quote header (customer, channel, quote #, status, total) → **reservations builder** (add/edit/duplicate/remove; per-reservation trip-type toggle, stops route editor, vehicle/pax, pricing) → live totals → **booking action**.
   - **Right:** Podium **conversation** + **activity / touch-point timeline** (greeting · quote sent · reminder · review request).
4. **Pipeline** — kanban (New → Quoted → Booked → Lost), per-column value, drag-to-advance (with toast + status update).
5. **Contacts** — directory: avatar, channel, phone/email, lifetime value, last activity; click → lead workspace.
6. **Reviews** — Podium review-invite requests (sent/pending) + incoming star ratings.
7. **Settings** — capture channels, integrations (Podium · Zapier/LA status), users & roles, branding. Polished, mostly static.

---

## 5. Quote → reservations → booking flow (interactive)

- **Reservation editor:** trip-type toggle (Transfer / Hourly) swaps the pricing fields; **stops route editor** (Pickup pinned first, [+ Add stop] for intermediates, Drop-off pinned last) shown as a vertical route with connector pins; vehicle select; passengers; live line total. Hourly shows `hours × rate` with a "3-hr minimum applied" flag when relevant.
- **Live totals:** quote subtotal updates as reservations change; "Gratuity & fees — Calculated in LimoAnywhere"; quote total in tabular figures.
- **Send quote via Podium:** `New → Quoted`; appends an outbound message to the conversation; toast.
- **Mark booked → LimoAnywhere:** `Quoted → Booked`; plays a staged sync animation — **Find/Create Account → Create Quote Request → Create Reservation** (one row per reservation, spinner → check), assigning mock `LA-####` IDs; ends in a "Synced" state with an "Open in LimoAnywhere" affordance and a success toast.

---

## 6. Visual & brand system

- **Palette:** charcoal frame `#14181f` / `#1b212b` / `#222b37`; champagne **gold `#c2a14e`** (deep `#8a6d1f`, light `#fbf6e9`) used sparingly for primary actions + active nav; warm-neutral canvas `#eef0f2` / white.
- **Type:** Inter; tight heading tracking; **tabular-nums for all money**.
- **Channel system (everywhere):** Wedding Pro = rose, Website = blue, Phone = green, API = violet.
- **Status system:** New = blue, Quoted = gold/amber, Booked = green, Lost = slate.
- **Trip-type tags:** Transfer = slate/neutral, Hourly = indigo; multi-stop shown as "+N stops".
- **Craft details:** refined avatars with channel ring, segmented filter control, sticky-header tables, soft elevation, **empty states + loading skeletons**, smooth view transitions, **toasts**, the syncing→synced animation, a top-bar sync/notifications cluster, custom scrollbars, `[x-cloak]` to prevent flash.
- **Responsive:** sidebar collapses on narrow widths; tables/workspaces stack gracefully.

---

## 7. File plan (in `html_designs/`, elevated in place)

- `index.html` — SPA shell + all view sections (supersedes old `index.html`; Inbox becomes a view, so standalone `inbox.html` is removed/redirected).
- `app.js` — mock data store + Alpine logic + pricing helpers.
- `README.md` — refreshed to describe the portal, the trip-type/reservation model, and the planned Django mapping.

---

## 8. Out of scope / explicitly deferred
- Real integrations, auth, persistence, multi-user.
- Email parsing (per the updated Integration Matrix, Wedding Pro now flows Podium → webhook, not an email parser).
- Production Django templates (this prototype is their reference).

## 9. Decisions made
- Quote detail = **full-page workspace** (not modal/drawer).
- **Elevate in place** in `html_designs/`.
- Two core trip types (**transfer**, **hourly**) + **multi-stop** routing as a shared attribute; round trips expressed as two reservations.

---

## 10. Addendum — Merchant processing (deposits & balance) · 2026-06-09

**Processor decision: Stripe** (v1). Same headline rate as Authorize.Net all-in-one (2.9% + $0.30) with **no monthly fee**, and purpose-built for the required flow — collect a deposit, **save the card (PaymentMethod)**, then **charge the balance off-session on schedule**. Design stays processor-agnostic so an interchange-plus move (Authorize.Net gateway-only + a negotiated merchant account, or LimoAnywhere's CardConnect) is possible at scale.

### Policy
- **Deposit = 50%** of the quote total, requested **when the quote is sent** (configurable in Settings).
- **Balance = remaining 50%**, charged **automatically 30 days before the earliest reservation's pickup**, off-session against the saved card. If booked inside the 30-day window, the balance is **due immediately**.

### Flow (auto-book on deposit)
1. **Send quote + 50% deposit request** → status `New → Quoted`; a Podium message carries a pay link; `deposit = requested`.
2. **Deposit paid → auto-book**: card saved on file; the LimoAnywhere sync runs (Account → Quote Request → Reservations) **plus a final "Schedule balance charge" step**; status → `Booked`; `balance = scheduled`.
3. **Balance charge** (30 days out): `paid` on success, else `failed`.
4. **On failure → notification on the lead** + top-bar bell + an alert banner in the workspace; agent actions: **Retry charge** / **Request new card via Podium**. *No automated dunning in v1* (notify + manual, per client decision).

### Data model additions (for the ERD)
- **Payment plan** (one per quote/order): `deposit_pct`, `deposit_amount`, `balance_amount`, `deposit_status` (unsent · requested · paid), `balance_status` (na · scheduled · paid · failed), `balance_due_date`, `card_brand`, `card_last4`, `processor` (stripe), `stripe_customer_id`, `stripe_payment_method_id`, `fail_reason`, timestamps.
- **Charge / Transaction** (many per plan — like `ZapEvent`, for traceability + retries): `type` (deposit · balance), `amount`, `status`, `processor_charge_id` (Stripe PaymentIntent), `attempted_at`, `failure_reason`, idempotency key.
- **Notification** (belongs to a Lead): `kind` (balance_failed · balance_paid · deposit_paid · …), `title`, `detail`, `read`, `created_at`.

### Prototype representation
- Workspace **Payments panel** (deposit + balance tiles, card on file, state-driven controls), a red **balance-failed banner**, a working **notifications bell**, payment chips on the **Leads table / Pipeline / Inbox**, and a **Settings → Payments** card. Because the real balance charge is time-based, the panel exposes demo controls to **simulate the scheduled attempt (succeed / fail)**; **Olivia is seeded with a failed balance** so the failure + notification state is visible on load.
