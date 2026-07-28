# CLAUDE.md — All Pro Charter Lead Manager

Conventions and rules for working in this repo. Read before building features.

## What this is
Custom lead-to-booking portal for All Pro Charter. Lead = one Quote holding many
Reservations; 50% deposit (Stripe) → auto-book to LimoAnywhere (Zapier) → balance charged
30 days before pickup; messaging/touch-points/reviews via Podium. LimoAnywhere is the system
of record. Full design in **`docs/`** (specs, ERD, and the clickable prototype).

## Stack
Python 3.13 · Django 5.2 LTS · DRF · HTTP cron endpoints (cron-job.org) · MySQL (prod) / SQLite (dev) ·
Stripe · Podium · LimoAnywhere API · LocationIQ. Front end: Django templates + **Tailwind + Alpine.js**.

---

## 🎨 UI rules (non-negotiable)

These are hard rules. Build the reusable component once; use it everywhere.

### 1. Modals — never use native dialogs
- **Never** use `window.alert` / `confirm` / `prompt`, the native `<dialog>` element, or a
  one-off modal. **Every** modal — notifications, confirmations, option pickers, forms —
  uses the single reusable modal component: **`templates/components/modal.html`** (Alpine.js).
- **Built** as a global Alpine store (`$store.modal`) — open from any view:
  - `$store.modal.confirm({ title, message, variant, confirmText, onConfirm, onCancel })`
  - `$store.modal.alert({ title, message, variant })` — single acknowledge button
  - `$store.modal.show({ …, html, cancelText, showCancel })` — full control
  - Variants: `info` · `success` · `danger` · `gold`. `onConfirm` may return a promise — the
    confirm button shows a spinner / disables until it resolves.
- Brand-styled (charcoal/gold, Fraunces heading). Closes on `Esc` + backdrop click.
  **TODO:** focus-trap + `role="dialog"`/`aria-modal` for full a11y.

### 2. Dropdowns — never use native `<select>`
- **Never** use a bare `<select>` for an option input. Use the reusable searchable select on
  **Tom Select**: include **`templates/components/searchable_select.html`** (renders a
  `<select data-tom>`), auto-enhanced by `initTomSelects()` in **`static/js/app.js`** and
  themed in `static/css/app.css` (`.ts-*`). Example:
  ```django
  {% include "components/searchable_select.html" with name="channel" field_id="f-channel"
     options=channels selected=channel_filter empty_label="All channels" autosubmit=1 search="off" %}
  ```
  `options` = any iterable of `(value, label)` (e.g. a `TextChoices.choices`). Hooks:
  `data-autosubmit` (submit the form on change), `data-search="off"`, `multiple`.
- **TODO before prod:** a Django form-widget wrapper for model forms. Alpine / Tom Select /
  Tabler / flatpickr still load via **CDN**; Tailwind no longer does (see below).

> ✅ These are now **built** (`templates/components/`) and used by the live screens —
> see the **Web portal** section below. Use them; don't reintroduce native modals/selects.

### 3. Reuse everything else too
- **Templates:** factor shared markup into `templates/components/` partials (`{% include %}`)
  — buttons, badges (status/channel/trip-phase/payment), avatars, stat cards, data-table
  shell, empty states, toasts. Match the prototype's classes.
- **Forms:** custom widgets in `apps/core/widgets.py`; a base styled `forms.ModelForm`.
- **API:** base serializers/viewsets/mixins in `apps/core` (e.g. a `TimeStampedSerializer`,
  shared pagination, permission mixins) — don't repeat per app.
- **Models:** abstract bases + mixins in `apps/core` (`TimeStampedModel`, `MoneyField`),
  `TextChoices` enums, and `QuerySet`/`Manager` methods over ad-hoc filtering.

---

## 🎨 CSS build (Tailwind — compiled, not CDN)
Tailwind **3.4** is installed via npm and compiled ahead of time. There is no `cdn.tailwindcss.com`
and no inline `tailwind.config` in any template — the single source of truth is `tailwind.config.js`.

```bash
npm install          # once
npm run build:css    # assets/css/tailwind.src.css → static/css/tailwind.css (minified)
npm run watch:css    # keep running while editing templates
```

- **`static/css/tailwind.css` is generated AND committed.** Heroku runs the Python buildpack
  only, so the compiled CSS must be in the repo. **Rebuild and commit it whenever you add or
  change a class in a template, `apps/**/*.py`, or `static/js/*.js`** — otherwise the new class
  silently has no styles in prod. Never hand-edit it.
- **Load order is `tailwind.css` → `app.css`** in all three shells (`base.html`,
  `public/base_public.html`, `registration/login.html`). `app.css` must win: it and Tailwind
  both define `.font-display`, and the `app.css` version adds `letter-spacing`. Don't
  reverse these two `<link>`s.
- **JIT only emits classes it can find as literal strings.** `content` in `tailwind.config.js`
  covers `templates/`, `apps/**/*.py` (form widget `attrs={"class": …}`) and `static/js/`
  (modal/toast markup). A class assembled by concatenation at runtime will NOT be generated —
  write the full class name as a literal, or add it to `safelist`.
- Pinned to v3 deliberately: v4 requires Safari 16.4+ / Chrome 111+, too new for the public
  booking site's customers. Revisit when that floor ages out.

---

## 🖥 Web portal (server-rendered) — built
The authenticated UI is Django templates + Alpine + Tom Select, "executive chauffeur" theme.
- **Shell:** `templates/base.html` (`x-data="shell()"` → sidebar + notification tray).
  Blocks: `title`, `head_extra`, `content`, `body_extra`. Login page
  `templates/registration/login.html` is standalone (no shell).
- **Design system:** `static/css/app.css` (charcoal/gold tokens, `.card`, `.btn-gold`,
  `.field`, animations, Tom Select theme). Behaviour + Alpine stores: `static/js/app.js`.
- **App:** `apps.portal` owns the dashboard (`/`, name `dashboard`) and the `chrome` context
  processor (nav + notification bell on every page). Leads UI is in `apps.leads`:
  `lead_list` (`/leads/`), `lead_detail` (`/leads/<pk>/`), and the `pipeline` kanban
  (`/pipeline/`, per-column value + payment chips + guarded drag). Contacts directory
  (`/contacts/`, LTV/trips/last-activity/search) is `apps.contacts`. Messaging inbox
  (`/inbox/`, Podium conversations + composer) and the reviews board (`/reviews/`, invite
  statuses + incoming ratings) are both in `apps.messaging`. All views `@login_required`.
- **Auth:** `django.contrib.auth.urls`; `LOGIN_URL` / `LOGIN_REDIRECT_URL` / `LOGOUT_REDIRECT_URL` set.
- **Components:** `templates/components/` — `modal.html`, `toasts.html`,
  `searchable_select.html`, `status_badge.html`.
- **Demo data:** `python manage.py seed_demo [--fresh]`.

---

## 🧪 Testing & TDD (required)
- **Test-first.** No production code without a failing test first (see the team TDD skill).
- **pytest + pytest-django** (config in `pyproject.toml`, settings preset to dev).
  **factory-boy** factories live in each app at `apps/<app>/factories.py` (reusable fixtures).
- Tests live in `apps/<app>/tests/` (or `tests.py`). Run: `pytest` (whole suite) or
  `pytest apps/leads`. Keep output pristine.
- Cover the **logic**: pricing (unified `rate × max(hours,min_hours) + gratuity`,
  rate/minimums snapshotted from the VehicleType rate card), quote totals,
  `balance_due_date`, trip-status→phase mapping, multi-stop, idempotency.

## 🧱 Code style
- **ruff** for lint + format (`ruff check . && ruff format .`); config in `pyproject.toml`,
  line length 100. Migrations are excluded from lint.
- Type hints on functions/methods. Docstrings on non-obvious logic. Keep modules focused.
- External-API calls go in `services.py` (never in views); side-effects/jobs in `tasks.py`.
- **Django `{# #}` comments are SINGLE-LINE ONLY** (the lexer regex isn't `re.DOTALL`). A
  multi-line `{# … #}` is NOT stripped — its body renders as literal text (and any markup
  inside it, e.g. a stray `<input>`, becomes real HTML). This has bitten repeatedly. Use
  `{% comment %}…{% endcomment %}` for anything spanning more than one line.

## 🗂 Structure
```
config/     settings/{base,dev,prod} · urls.py
apps/       core accounts contacts leads reservations payments
            messaging integrations notifications portal   (name = "apps.<x>")
templates/  base.html · registration/ · components/ · portal/ · leads/
static/     css/app.css · js/app.js
docs/       specs (design · ERD · scope) + prototype (clickable HTML)
```

## ⚙️ Settings & env
- Default settings: `config.settings.dev` (manage.py); deploy: `config.settings.prod`.
- Env-driven via `django-environ`; see `.env.example`. **Never commit `.env` or secrets.**
- Custom user is `accounts.User` (`AUTH_USER_MODEL`) — already set; don't swap it.

## 🚀 Commands
```bash
source .venv/bin/activate
python manage.py check | makemigrations | migrate | runserver
pytest                       # tests
ruff check . && ruff format .
npm run watch:css            # Tailwind rebuild-on-save (run alongside runserver)
npm run build:css            # one-off minified build — commit static/css/tailwind.css
```

## ✅ Definition of done (per feature)
Tests written first and green · ruff clean · `manage.py check` clean · migrations committed ·
reused shared components (no native modal/select) · admin registered where useful.

---

# AWS Guidance

- Prefer the AWS MCP Server for AWS interactions — it provides sandboxed
  execution, observability, and audit logging. If unavailable, use the
  AWS CLI directly.
- Before starting a task, check whether a relevant AWS skill is available.
  Load the skill with `retrieve_skill` and prefer its guidance over
  general knowledge.
- When uncertain about specific AWS details (API parameters, permissions,
  limits, error codes), verify against documentation rather than guessing.
  State uncertainty explicitly if you cannot confirm.
- When creating infrastructure, prefer infrastructure-as-code (AWS CDK or
  CloudFormation) over direct CLI commands.
- When working with infrastructure, follow AWS Well-Architected Framework
  principles.
- Do not use em dashes in AWS resource names or descriptions. Use
  hyphens instead.

## Secret Safety

- MUST load the `aws-secrets-manager` skill first for any secret,
  credential, API key, token, or password task. MUST NOT call
  `secretsmanager get-secret-value` or `batch-get-secret-value`, and MUST
  NOT hit the Secrets Manager Agent daemon directly. MUST use
  `{{resolve:secretsmanager:secret-id:SecretString:json-key}}` with
  `asm-exec` so the secret resolves at runtime without entering context.

## APC account layout

- AWS org: management (payer) account (root email `moe@lansdownedata.com`)
  holds **no workloads** — billing and org management only. Client
  resources live in member accounts. Root emails are globally unique
  across all of AWS, so never reuse the payer's for a member account.
- `allprocharter-prod` (root email `apc@lansdownedata.com`) under the
  `Clients` OU is where this app's S3 bucket and IAM live. Default region
  `us-east-1`.
- Billing is consolidated on the payer account's card; per-client cost
  separation comes from the account boundary, not tags.
