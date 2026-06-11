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
- Driven by a global Alpine store (`$store.modal`) so any view can open one:
  `$store.modal.open({ title, body, variant, actions })`. Variants: `notify`, `confirm`,
  `form`, `danger`. Brand-styled (charcoal frame, gold accent, Fraunces headings) to match
  the prototype's reservation editor / sync overlay.
- Accessible: focus-trap, `Esc` to close, backdrop click, `role="dialog"`, `aria-modal`.

### 2. Dropdowns — never use native `<select>`
- **Never** use a bare `<select>` for an option input. Use the reusable searchable select
  built on **Tom Select** (vanilla JS, no jQuery, accessible): **`templates/components/searchable_select.html`**
  + the Django widget **`apps/core/widgets.py::SearchableSelect`** + the initializer in
  `static/js/components.js` that auto-enhances any `[data-select]`.
- Searchable by default; supports single / multi / grouped / remote options. Themed to the
  brand (charcoal/gold, `.card` ring styles). One include, one widget — used for vehicle
  pickers, trip-status (the LA taxonomy), channel, agent assignment, filters, etc.
- Tom Select is loaded via the base template (CDN in dev, vendored/bundled for prod).

> Until the template layer is built these live as documented intent; implement them as the
> **first** reusable components when we start templates, before any screen ships.

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
config/   settings/{base,dev,prod} · celery.py · urls.py
apps/     core accounts contacts leads reservations payments
          messaging integrations notifications     (name = "apps.<x>")
docs/     specs (design · ERD · scope) + prototype (clickable HTML)
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
