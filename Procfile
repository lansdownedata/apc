release: python manage.py check --deploy --fail-level ERROR && python manage.py migrate --noinput && (python manage.py backfill_trip_timezones || true)
web: gunicorn config.wsgi --log-file - --access-logfile - --workers 3 --timeout 60
