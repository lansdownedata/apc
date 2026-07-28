release: python manage.py migrate --noinput
web: gunicorn config.wsgi --log-file - --access-logfile - --workers 3 --timeout 60
