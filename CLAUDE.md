# CLAUDE.md — All Pro Charter Lead Manager

Conventions and rules for working in this repo. Read before building features.

## What this is
Custom lead-to-booking portal for All Pro Charter. Lead = one Quote holding many
Reservations; 50% deposit (Stripe) → auto-book to LimoAnywhere (Zapier) → balance charged
30 days before pickup; messaging/touch-points/reviews via Podium. LimoAnywhere is the system
of record. Full design in **`docs/`** (specs, ERD, and the clickable prototype).

## Stack
Python 3.13 · Django 5.2 LTS · DRF · Celery + Redis · MySQL (prod) / SQLite (dev) ·
Stripe · Podium · Zapier. Front end: Django templates + **Tailwind + Alpine.js**.

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
- **TODO before prod:** a Django form-widget wrapper for model forms, and a real asset build —
  Tailwind / Alpine / Tom Select currently load via **CDN** in `base.html`.

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

## 🖥 Web portal (server-rendered) — built
The authenticated UI is Django templates + Alpine + Tom Select, "executive chauffeur" theme.
- **Shell:** `templates/base.html` (`x-data="shell()"` → sidebar + notification tray).
  Blocks: `title`, `head_extra`, `content`, `body_extra`. Login page
  `templates/registration/login.html` is standalone (no shell).
- **Design system:** `static/css/app.css` (charcoal/gold tokens, `.card`, `.btn-gold`,
  `.field`, animations, Tom Select theme). Behaviour + Alpine stores: `static/js/app.js`.
- **App:** `apps.portal` owns the dashboard (`/`, name `dashboard`) and the `chrome` context
  processor (nav + notification bell on every page). Leads UI is in `apps.leads`:
  `lead_list` (`/leads/`) and `lead_detail` (`/leads/<pk>/`). All views `@login_required`.
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
- Cover the **logic**: pricing (transfer flat vs hourly `max(hours,min)×rate`), quote totals,
  `balance_due_date`, trip-status→phase mapping, multi-stop, idempotency.

## 🧱 Code style
- **ruff** for lint + format (`ruff check . && ruff format .`); config in `pyproject.toml`,
  line length 100. Migrations are excluded from lint.
- Type hints on functions/methods. Docstrings on non-obvious logic. Keep modules focused.
- External-API calls go in `services.py` (never in views); side-effects/jobs in `tasks.py`.

## 🗂 Structure
```
config/     settings/{base,dev,prod} · celery.py · urls.py
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
celery -A config worker -l info   # + `beat` for the scheduler (needs Redis)
```

## ✅ Definition of done (per feature)
Tests written first and green · ruff clean · `manage.py check` clean · migrations committed ·
reused shared components (no native modal/select) · admin registered where useful.
