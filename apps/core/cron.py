"""HTTP-triggered scheduled jobs — cron-job.org POSTs here on a schedule.

Replaces Celery beat: each slug maps to a plain job function returning a
processed-count. Auth is the X-Cron-Key header vs settings.CRON_SECRET
(fail closed). See docs/specs/2026-07-05-http-cron-endpoints-design.md.
"""

import hmac
from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.integrations import la_sync
from apps.payments import tasks

JOBS: dict[str, Callable[[], int]] = {
    "charge-due-balances": tasks.charge_due_balances,
    "recognize-due-revenue": tasks.recognize_due_revenue,
    "retry-la-sync": la_sync.retry_failed_pushes,
}


@csrf_exempt
@require_POST
def run_job(request: HttpRequest, job: str) -> HttpResponse:
    secret = settings.CRON_SECRET
    provided = request.headers.get("X-Cron-Key", "")
    if not secret or not hmac.compare_digest(provided, secret):
        return HttpResponse(status=403)
    fn = JOBS.get(job)
    if fn is None:
        return HttpResponse(status=404)
    return JsonResponse({"job": job, "processed": fn()})
