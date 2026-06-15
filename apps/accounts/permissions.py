"""Access decorators for the portal."""

from functools import wraps

from django.core.exceptions import PermissionDenied


def _require(test):
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated or not test(request.user):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


owner_admin_required = _require(lambda u: u.is_owner_admin)
payment_access_required = _require(lambda u: u.has_payments_access)
