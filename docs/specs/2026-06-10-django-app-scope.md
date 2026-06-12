# All Pro Charter — Lead Manager · Django App Scope

**Prepared:** 2026-06-10 · Lansdowne Data
**Builds on:** [Solution Design](./../../APC_Lead_Manager_Solution_Design.pdf) · [Integration Matrix](./../../APC_Integration_Matrix_FINAL.pdf) · [Portal design spec](./2026-06-09-lead-manager-portal-design.md) · [ERD](./2026-06-10-lead-manager-erd.md)
**Version 1 scope:** capture → quote (multi-reservation, transfer/hourly/multi-stop) → **50% deposit (Stripe)** → auto-book to LimoAnywhere → **balance charge 30 days out** → Podium messaging, touch-points & reviews.

---

## 1. Architecture & stack

| Layer | Choice |
|---|---|
| Backend | **Django + Django REST Framework** |
| Frontend | Django templates + **Tailwind CSS + Alpine.js** (the approved prototype lifts in screen-by-screen) |
| Database | **MySQL** (managed) |
| Async / scheduled | **Celery + Redis** (beat for scheduled jobs, worker for I/O) |
| Payments | **Stripe** (Checkout/Payment Links + off-session PaymentIntents) |
| Integration glue | **Zapier REST Hooks** → LimoAnywhere |
| Messaging | **Podium API** (OAuth 2.0) |
| Hosting | **Heroku or Azure** *(open — §15)*, HTTPS, `leads.allprocharter.com`, automated backups |

---

## 2. Project layout (Django apps)

```
config/                     # settings (split: base/dev/prod), celery, urls, wsgi/asgi
apps/
  accounts/                 # User (role), 2FA, AuditLog, permissions
  contacts/                 # Contact (the LimoAnywhere "Account")
  leads/                    # Lead/Quote, pipeline, Vehicle (reference)
  reservations/             # Reservation, Stop, pricing logic
  payments/                 # PaymentPlan, Charge, Stripe service, balance scheduler
  messaging/                # Message, TouchPoint, Review — Podium service (webhook in)
  integrations/             # ZapEvent, Zapier REST-Hook subscriptions, LA sync, webhooks
  notifications/            # Notification + bell feed
  core/                     # shared mixins, base models (TimeStamped), settings/config
```

Each app owns its models, serializers, services (external-API calls live in `services.py`, not views), and tasks. Keeping payments, messaging, and integrations isolated means each can be built and tested independently and a change in one (e.g., swapping Stripe for interchange-plus) doesn't ripple.

---

## 3. Data model (by app → ERD)

- **accounts** — `User` (`role`: owner_admin / agent, `two_factor_enabled`), `AuditLog` (who changed what).
- **contacts** — `Contact` (name, company, phone, email, channel, `la_account_id`, `podium_contact_uid`).
- **leads** — `Lead` (→ Contact, agent, `quote_no`, `status`, `notes`, `has_alert`), `Vehicle` (reference list).
- **reservations** — `Reservation` (→ Lead, Vehicle, `trip_type`, schedule, pricing fields, **`trip_status` + `trip_phase`**), `Stop` (→ Reservation, `sequence`, address, note), `TripStatusEvent` (dispatch-status history).
- **payments** — `PaymentPlan` (1↔1 Lead; deposit/balance amounts + statuses, `balance_due_date`, Stripe refs, card brand/last4), `Charge` (→ PaymentPlan; kind, amount, status, PaymentIntent, idempotency key, failure reason).
- **messaging** — `Message` (→ Lead; direction, `channel` ∈ sms/email/facebook/whatsapp/apple, body, `podium_message_uid`, `podium_conversation_uid`, delivery state + failure reason), `TouchPoint` (→ Lead; kind, schedule), `Review` (→ Lead/Contact; `podium_review_invite_uid`, `delivery_status`, `link_clicked`, attributed rating/body/site).
- **integrations** — `ZapEvent` (→ Lead; action, payload, result, idempotency key — the LimoAnywhere sync log) and `PodiumEvent` (inbound Podium webhook log: event type, payload, processed flag).
- **notifications** — `Notification` (→ Lead, optional User; kind, title, detail, read).

Full fields, enums, and relationships are in the [ERD](./2026-06-10-lead-manager-erd.md). Money/derived values (`Reservation.line_total`, `Lead.quote_total`) are computed; `PaymentPlan` snapshots the total at quote-send time.

---

## 4. Capture channels → Lead (from the Integration Matrix)

| # | Channel | Mechanism | Endpoint / trigger |
|---|---|---|---|
| 1 | Website quote form | Embeddable form posts to the portal | `POST /api/capture/website/` |
| 2 | Wedding Pro | WeddingWire/The Knot → **Podium → webhook** | `POST /webhooks/podium/lead/` |
| 3 | Phone / walk-in | Agent quick-intake screen | `POST /api/leads/` (UI) |
| 4 | Open API | Any source | `POST /api/capture/inbound/` (API-key auth) |

All four normalise onto a `Lead` + `Contact` (dedupe Contact by phone/email), seed a default `PaymentPlan`, and fire the greeting `TouchPoint`.

---

## 5. REST API surface (DRF)

**Portal (session-auth, consumed by Alpine):**
- `GET/POST /api/leads/`, `GET/PATCH /api/leads/{id}/` — list (filter `status`, `channel`, search), detail, edit.
- `GET/POST /api/leads/{id}/reservations/`, `PATCH/DELETE /api/reservations/{id}/`, nested `…/stops/`.
- `POST /api/leads/{id}/send-quote/` — status→Quoted, create Stripe deposit Payment Link, send via Podium, `deposit_status=requested`.
- `POST /api/leads/{id}/book/` — manual/offline book (auto path is the Stripe deposit webhook); runs the LA sync + schedules balance.
- `GET /api/leads/{id}/messages/` (thread) · `POST /api/leads/{id}/messages/` (send via Podium).
- `GET /api/leads/{id}/payment/` · `POST /api/leads/{id}/payment/charge-balance/` (retry now) · `POST …/request-new-card/`.
- `GET /api/notifications/` · `POST /api/notifications/{id}/read/` · `POST /api/notifications/read-all/`.
- `GET /api/contacts/`, `GET /api/reviews/`, `POST /api/reviews/{id}/invite/`.

**Capture & webhooks (no session):**
- `POST /api/capture/website/`, `POST /api/capture/inbound/` (API key).
- `POST /webhooks/stripe/` (signed), `POST /webhooks/podium/` (`message.received/sent/failed` + Wedding Pro lead), `POST /webhooks/limoanywhere/` (status writeback via Zapier).
- `POST /api/zapier/subscribe/` · `DELETE /api/zapier/unsubscribe/` (Zapier REST-Hook subscription management).

---

## 6. Integrations

### 6a. LimoAnywhere — via Zapier REST Hooks (system of record)
On **book** (deposit paid, or manual), the portal POSTs a structured payload to the subscribed Zapier hook; the Zap maps to LA actions in order, logged per step as `ZapEvent`s with an idempotency key:
1. **Find / Create Account** (from Contact)
2. **Create Quote Request** (the quote)
3. **Create Reservation** — **one per `Reservation`** (trip type, stops, vehicle, schedule, pricing)

LA status flows back via `POST /webhooks/limoanywhere/` → updates `Reservation.la_reservation_id`, `Contact.la_account_id`, the `ZapEvent`, and **`Reservation.trip_status`** (logging a `TripStatusEvent`). Idempotency keys ensure retries never duplicate.

**Trip status (operational, separate from the sales pipeline).** Each `Reservation` mirrors LimoAnywhere's exact dispatch status — *Unassigned → Offered → Assigned → Dispatched → On The Way → Circling → Arrived → Customer In Car → Done*, plus the *Cancelled / No Show* and *Affiliate / Farm-out* states — grouped by phase for display. It's read from the LA writeback above and is **manually editable in-portal for off-LA affiliate trips** (manual email handoff). Reaching **Done** fires the post-trip **review-request** TouchPoint. A booked quote may hold several trips at different statuses, and a trip can be Cancelled / No-show while the Lead stays Booked.

### 6b. Podium — REST API (OAuth 2.0), messaging backend
*Verified against docs.podium.com (2026-06). Base host `api.podium.com`.*

- **Auth:** OAuth 2.0 **authorization-code** grant (`/oauth/authorize` → `/oauth/token`). Access tokens last **10 hours** — store the **refresh token** and refresh on 401. Request only the scopes we use: `read_messages`, `write_messages`, `read_contacts`, `write_contacts`, `read_reviews`, `write_reviews`. Calls are scoped to a **location UID** under the **organization UID**.
- **Inbound messages → webhook, not polling.** Podium's **Message Webhook API** posts `message.received` / `message.sent` / `message.failed` to `POST /webhooks/podium/`; we upsert `Message` and surface delivery failures (e.g. `failureReason: landline`). **This supersedes the earlier "poll conversations" plan** — Podium now offers message webhooks.
- **Send message** (`write_messages`): outbound replies, the Stripe deposit link, and `TouchPoint`s. Multi-channel — `channel.type` ∈ {sms, email, facebook, whatsapp, apple}; default to SMS/email on Podium's number.
- **Contacts** (`read_/write_contacts`): full CRUD — create without a conversation, get by phone/email, update/sync; mirror `podium_contact_uid` on `Contact`.
- **Review invites** (`write_reviews`): **Create Review Invitation** on trip `Done`; the review-invite object returns `deliveryStatus` (pending/sent/delivered/failed), `linkClicked`, and `attributions[]` (resulting rating/body/site) → mirrored onto `Review`.
- **Wedding Pro** leads surface as inbound Podium messages/events → captured into a `Lead`.
- Every Podium object is a string **UID** (contact, conversation, message, location, org, review-invite) — store the relevant ones on our models.

### 6c. Stripe — deposits & balance
- **Deposit**: on send-quote, create/reuse a Stripe **Customer**, generate a **Payment Link / Checkout Session** for `deposit_amount` with `setup_future_usage=off_session` (saves the card). The link is texted via Podium.
- **Auto-book**: `payment_intent.succeeded` (deposit) webhook → `deposit_status=paid`, store `payment_method`, trigger the LA sync, set `balance_status=scheduled`, `balance_due_date = earliest pickup − 30 days`.
- **Balance**: the scheduler (§7) creates an off-session **PaymentIntent** (`off_session=true`, saved PaymentMethod, idempotency key). `payment_intent.succeeded` → `paid`; `payment_intent.payment_failed` (or `authentication_required`) → `failed` + Notification.
- **PCI**: card data never touches our servers (Checkout/Payment Links + tokens) → **SAQ-A** scope.

---

## 7. Background jobs (Celery beat)

| Task | Cadence | What it does |
|---|---|---|
| `messaging.run_touchpoints` | ~15 min | Send due `TouchPoint`s (greeting, follow-up, pre-trip reminder, review request) via Podium. |
| **`payments.charge_due_balances`** | **daily** | Find `PaymentPlan`s with `balance_status=scheduled` and `balance_due_date <= today`; create a `Charge` + off-session PaymentIntent. **Success → `paid`. Failure → `balance_status=failed`, `lead.has_alert=true`, create `Notification(balance_failed)`.** ← the core requirement. |
| `integrations.retry_failed_syncs` | ~30 min | Retry `ZapEvent`s in `error` (idempotent). |

> Inbound Podium messages are **webhook-driven** (`message.received` / `sent` / `failed`) — there is **no polling task**.
>
> Failure recovery is **notify + manual** (per decision): no auto-retry/dunning in v1 — ops gets the notification and uses **Retry charge** / **Request new card**. Hooks are left in place to add scheduled retries later.

---

## 8. Notifications
A `Notification` is created on `balance_failed` (primary), `sync_failed`, optionally `deposit_paid` / `new_lead`. The portal bell polls `GET /api/notifications/`; clicking opens the lead. `lead.has_alert` drives the row/card/workspace alert styling.

---

## 9. Security, roles, audit
- Django auth; **role-based permissions** (owner_admin vs agent) via DRF permission classes.
- **2FA** (django-otp / TOTP) — required for admins (toggle in Settings).
- **Audit trail** on lead/quote/payment changes (`AuditLog` or `django-simple-history`).
- HTTPS-only, secrets via env, Stripe/Podium keys in a secrets store; idempotency keys on every money/external write. Data lives in APC's own hosting account — APC owns it all.

---

## 10. Environments, testing, deployment
- **Settings split** (base/dev/prod), `.env` config, separate Stripe/Podium **test vs live** keys.
- **Tests**: model/pricing unit tests (transfer vs hourly vs min-hours, multi-stop totals, `balance_due_date`), payment-flow tests with **Stripe test cards** (incl. the decline `4000000000000341` off-session-fail), webhook signature tests, integration tests with mocked Zapier/Podium.
- **Deploy**: managed MySQL + Redis, web + worker + beat dynos/containers, migrations, automated backups, Sentry/error logging.

---

## 11. Build phases & estimate (folds payments into the prior figure)

@ **$75/hr**. The prior Solution Design totalled **120 hrs / $9,000**; adding **Payments & deposits** brings it to **140 hrs / $10,500**. Run as time-and-materials or a not-to-exceed cap.

| Phase | Covers | Hrs |
|---|---|---|
| A · Discovery & design | Brand match, data model, flows, inbox UX | 6 |
| B · Core application | Django models, auth/roles, lead inbox, contacts, pipeline | 30 |
| C · Capture channels | Website form/widget, Wedding Pro (Podium webhook), intake, open API | 20 |
| D · LimoAnywhere integration | Zapier REST Hooks, field mapping, sync log, retries, status writeback | 8 |
| E · Podium messaging | OAuth, message-webhook receiver, send, touch-point scheduler, review invites | 8 |
| F · Branding & UI polish | APC theme, responsive layout, templates | 15 |
| **G · Payments & deposits (Stripe)** ⟵ new | Customer + saved card, deposit Payment Link + webhook (auto-book), **off-session balance scheduler (30-day rule)**, failure → Notification, retry/request-card actions, Settings, tests | **18** |
| H · Testing, deploy, docs & handoff | E2E testing, deployment, documentation, training | 8 |
| I · PM & contingency | Coordination, client reviews, iteration, buffer | 27 |
| **Total** | | **140** |

*Third-party costs (Stripe 2.9% + $0.30, Podium, LimoAnywhere, Zapier, hosting) are billed to APC directly and are not included.*

---

## 12. Open items / decisions
- **Hosting:** Heroku vs Azure (provision MySQL + Redis).
- **Processor at scale:** Stripe for v1; revisit interchange-plus (Authorize.Net gateway-only / LA CardConnect) if monthly card volume clears ~$5–7k.
- **Podium Developer access:** ✅ test **organization** (UID), OAuth **app** (client_id/secret), and **dev redirect URI** `https://<ngrok-subdomain>.ngrok-free.dev/integrations/podium/callback/` (ngrok HTTPS tunnel → `localhost:8000`, since Podium requires HTTPS). Still to confirm: the **location UID** under the org to scope calls, and production-app approval. Scopes: `read_/write_messages`, `read_/write_contacts`, `read_/write_reviews`.
- **Deposit policy:** 50% confirmed; expose as a Setting (per-quote override?).
- **Balance reference date:** earliest pickup across the quote (confirmed) vs per-reservation.

## 13. Out of scope (v1)
Custom SMS/number (messaging stays on Podium), affiliate dispatch & COI/DOT compliance (native LimoAnywhere), automated dunning, customer self-serve portal, multi-currency.
