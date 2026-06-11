# All Pro Charter — Lead Manager

Custom lead-to-booking portal for All Pro Charter. Captures every lead, builds a
multi-reservation quote, takes a **50% deposit (Stripe)**, **books into LimoAnywhere** via
Zapier, **charges the balance 30 days before pickup**, and runs messaging / touch-points /
reviews through **Podium**. LimoAnywhere is the system of record; the portal is the front door.

## Stack
- **Python 3.13** · **Django 5.2 LTS** · **Django REST Framework**
- **Celery + Redis** (scheduled jobs incl. the balance charger)
- **MySQL** (prod) / **SQLite** (dev) — via `DATABASE_URL`
- **Stripe** (deposits + off-session balance), **Podium API**, **Zapier REST Hooks**
- Front end: Django templates + **Tailwind + Alpine** (the approved prototype lifts in)

## Quickstart (local)
```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements/dev.txt
cp .env.example .env          # then set DJANGO_SECRET_KEY
python manage.py migrate      # SQLite by default
python manage.py createsuperuser
python manage.py runserver    # http://127.0.0.1:8000  (health: /healthz/)
```

## Layout
```
config/                 project config
  settings/             base.py · dev.py (default) · prod.py
  celery.py             Celery app  ·  urls.py  ·  wsgi/asgi (prod)
apps/                   the Lead Manager domain
  core/                 shared base models (TimeStampedModel)
  accounts/             custom User (owner_admin / agent) + 2FA flag, audit
  contacts/             Contact (= LimoAnywhere Account)
  leads/                Lead / Quote, pipeline, Vehicle
  reservations/         Reservation, Stop, trip status (LA taxonomy)
  payments/             PaymentPlan, Charge — Stripe deposit + balance
  messaging/            Message, TouchPoint, Review — Podium
  integrations/         ZapEvent, Zapier/LA sync, webhooks
  notifications/        Notification (bell feed)
requirements/           base.txt · dev.txt · prod.txt
```

## Settings & environment
- Default settings module: `config.settings.dev` (set in `manage.py`); deploy uses
  `config.settings.prod`.
- All config is env-driven (`django-environ`); see **`.env.example`**. `.env` is gitignored.

## Common commands
```bash
python manage.py check
python manage.py makemigrations && python manage.py migrate
pytest                         # tests (DJANGO_SETTINGS_MODULE preset in pyproject.toml)
ruff check . && ruff format .  # lint + format
celery -A config worker -l info    # worker (needs Redis)
celery -A config beat -l info      # scheduler
```

## Design reference
Solution design, ERD, Django scope, and the clickable HTML prototype live in the working
folder (`Desktop/LDS/APC/docs/` and `Desktop/LDS/APC/html_designs/`). Bring them into `docs/`
here when convenient.
