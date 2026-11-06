"""Farm-in receive service: turns a GNet reservation payload into portal facts.

Contract: GNET-CONNECTION-GUIDE.md §1. `summarize` / `price_request` interpret a payload;
`receive` and the `handle_*` functions persist it and build the body GNet gets back.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.addresses.models import Airport
from apps.contacts.models import Company, Contact
from apps.core.choices import Channel
from apps.dispatch.models import FarmInRequest
from apps.integrations import gnet
from apps.leads.models import VehicleType
from apps.notifications.models import Notification
from apps.reservations import timezones

logger = logging.getLogger(__name__)

EVENT_NEW = "farm_in.new"
EVENT_UPDATE = "farm_in.update"
EVENT_QUOTE = "farm_in.quote"

ALERT_UNMAPPED = "Unmapped vehicle type: {code}"
ALERT_UNMAPPED_PREFIX = "Unmapped vehicle type: "
ALERT_NO_PICKUP = "No pickup time in request"
ALERT_TZ_ASSUMED = "Time zone assumed from company default"
ALERT_UPDATED = "Updated by requester"
ALERT_CANCELED = "Requester canceled"
ALERT_UPDATED_AFTER_ACCEPT = "Updated by requester after accept, review the order"
ALERT_CANCEL_AS_NEW = "Cancel received as a new delivery, review"

GNET_CANCEL = "CANCEL"
_CLOSED = (
    FarmInRequest.Status.DECLINED,
    FarmInRequest.Status.CANCELED,
    FarmInRequest.Status.EXPIRED,
)
_MAX_SMALL_INT = 32767

_CENT = Decimal("0.01")


@dataclass(slots=True)
class Summary:
    """What the dispatcher needs to see about an inbound request."""

    vehicle: VehicleType | None
    gnet_vehicle_type: str
    hourly: bool
    pickup_at: datetime | None
    timezone: str
    timezone_assumed: bool
    pickup_summary: str
    dropoff_summary: str
    passenger_name: str
    passenger_count: int
    alerts: list[str] = field(default_factory=list)


def _dict(value) -> dict:
    """``value`` when it is a dict, else an empty dict (payloads are untrusted)."""
    return value if isinstance(value, dict) else {}


def _passenger_count(raw, fallback: int) -> int:
    """Headcount from a GNet string; cosmetic, so junk falls back and the floor is 1."""
    try:
        count = int(str(raw).strip())
    except ValueError:
        count = fallback
    return max(count, 1)


def _parse_pickup(raw, tz_name: str) -> datetime | None:
    """UTC pickup instant. Aware values convert; naive values are read in ``tz_name``."""
    try:
        parsed = datetime.fromisoformat(str(raw))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(tz_name))
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _airport_timezone(location: dict) -> str | None:
    """IANA zone of an airport pickup named only by its IATA code, or None.

    Applies when the location says it is an airport or its address is a bare 3-letter code.
    """
    code = str(location.get("address") or "").strip().upper()
    is_airport = str(location.get("locationType") or "").lower() == "airport"
    if not code or not (is_airport or (len(code) == 3 and code.isalpha())):
        return None
    zones = Airport.objects.filter(iata=code).exclude(timezone="")
    return zones.values_list("timezone", flat=True).first()


def summarize(payload: dict) -> Summary:
    """Interpret a farm-in payload; problems become alerts rather than errors."""
    alerts: list[str] = []
    locations = _dict(payload.get("locations"))
    pickup = _dict(locations.get("pickup"))
    dropoff = _dict(locations.get("dropOff"))

    code = str(payload.get("preferredVehicleType") or "")
    vehicle = gnet.vehicle_type_for_code(code)
    if vehicle is None:
        alerts.append(ALERT_UNMAPPED.format(code=code))

    tz_name = timezones.timezone_at(pickup.get("lat"), pickup.get("lon"))
    if tz_name is None:
        tz_name = _airport_timezone(pickup)
    assumed = tz_name is None
    if assumed:
        tz_name = settings.TIME_ZONE

    pickup_at = _parse_pickup(pickup.get("time"), tz_name)
    if pickup_at is None:
        alerts.append(ALERT_NO_PICKUP)
    if assumed:
        alerts.append(ALERT_TZ_ASSUMED)

    passengers = payload.get("passengers")
    passengers = passengers if isinstance(passengers, list) else []
    lead = _dict(passengers[0]) if passengers else {}
    name = f"{lead.get('firstName') or ''} {lead.get('lastName') or ''}".strip()

    return Summary(
        vehicle=vehicle,
        gnet_vehicle_type=code,
        hourly=is_hourly(payload),
        pickup_at=pickup_at,
        timezone=tz_name,
        timezone_assumed=assumed,
        pickup_summary=pickup.get("address") or "",
        dropoff_summary=dropoff.get("address") or "",
        passenger_name=name,
        passenger_count=_passenger_count(payload.get("passengerCount"), len(passengers) or 1),
        alerts=alerts,
    )


def is_hourly(payload: dict) -> bool:
    """Whether the payload's runType says an hourly (as-directed) booking."""
    return "hour" in str(payload.get("runType", "")).lower()


def price_request(vehicle: VehicleType | None, *, hourly: bool) -> Decimal | None:
    """Rate-card price: rate x the applicable minimum hours; None when unpriceable."""
    if vehicle is None or vehicle.rate <= 0:
        return None
    hours = vehicle.hourly_min_hours if hourly else vehicle.transfer_min_hours
    if hours <= 0:
        return None
    return (vehicle.rate * hours).quantize(_CENT)


def ensure_requester(payload: dict) -> tuple[Company, bool]:
    """Find or create the requesting operator's Company; call inside transaction.atomic()."""
    raw = _dict(payload.get("affiliateReservation")).get("requesterId")
    if isinstance(raw, bool) or not isinstance(raw, str | int):
        raise ValueError("requesterId missing")
    rid = str(raw).strip()
    if not rid:
        raise ValueError("requesterId missing")
    if len(rid) > 64:
        raise ValueError("requesterId too long")
    company = Company.objects.select_for_update().filter(gnet_grid_id=rid).first()
    if company:
        return company, False
    company = Company.objects.filter(name__iexact=rid, gnet_grid_id__isnull=True).first()
    if company:
        company.gnet_grid_id = rid
        company.is_affiliate_account = True
        company.save()
        return company, False
    company = Company.objects.create(
        name=rid,
        gnet_grid_id=rid,
        is_affiliate_account=True,
        notes="Auto-created from GNet farm-in",
    )
    Contact.objects.create(name=rid, company=company, channel=Channel.GNET)
    return company, True


def stored_response(req: FarmInRequest) -> dict:
    """The body GNet gets for a stored request; `totalAmount` only once a price exists."""
    body = {"reservationId": req.reservation_id_token}
    if req.quoted_amount is not None:
        body["totalAmount"] = f"{req.quoted_amount:.2f}"
    return body


def _clip(field_name: str, value: object) -> str:
    """``value`` cut to the column's length: payload text is untrusted and unbounded."""
    return str(value)[: FarmInRequest._meta.get_field(field_name).max_length]


ALERT_SEP = " · "


def _add_alert(req: FarmInRequest, text: str) -> None:
    """Append one segment to the request's " · "-joined alert (not saved; never twice)."""
    segments = [a for a in req.alert.split(ALERT_SEP) if a and a != text]
    req.alert = _clip("alert", ALERT_SEP.join([*segments, text]))


def _remove_alert(req: FarmInRequest, prefix: str) -> None:
    """Drop every segment of the request's alert that starts with `prefix` (not saved)."""
    req.alert = ALERT_SEP.join(
        a for a in req.alert.split(ALERT_SEP) if a and not a.startswith(prefix)
    )


def _gnet_status(payload: dict) -> str:
    """The payload's GNet status: top-level, else the one in `affiliateReservation`."""
    raw = payload.get("status") or _dict(payload.get("affiliateReservation")).get("status")
    return _clip("gnet_status", str(raw or ""))


def _payload_fields(payload: dict, *, delivery_id: str, now: datetime) -> dict:
    """Row fields copied from the delivery itself, refreshed on every event."""
    res_no = _dict(payload.get("affiliateReservation")).get("requesterResNo")
    return {
        "payload": payload,
        "delivery_id": _clip("delivery_id", delivery_id),
        "gnet_status": _gnet_status(payload),
        "requester_res_no": _clip("requester_res_no", str(res_no or "")),
        "last_event_at": now,
    }


def _summary_fields(summary: Summary) -> dict:
    """Row fields derived from a `Summary` (everything but price and alert)."""
    return {
        "vehicle": summary.vehicle,
        "gnet_vehicle_type": _clip("gnet_vehicle_type", summary.gnet_vehicle_type),
        "pickup_at": summary.pickup_at,
        "timezone": summary.timezone,
        "timezone_assumed": summary.timezone_assumed,
        "pickup_summary": _clip("pickup_summary", summary.pickup_summary),
        "dropoff_summary": _clip("dropoff_summary", summary.dropoff_summary),
        "passenger_name": _clip("passenger_name", summary.passenger_name),
        "passenger_count": min(summary.passenger_count, _MAX_SMALL_INT),
    }


def _notify_new(req_pk: int) -> None:
    """Tell the desk a request arrived; a failure is logged, never raised into GNet's reply."""
    try:
        req = FarmInRequest.objects.select_related("company").get(pk=req_pk)
        Notification.notify(
            None,
            Notification.Kind.FARM_IN_NEW,
            title=f"GNet request from {req.company.name}",
            detail=f"{req.pickup_summary} → {req.dropoff_summary}",
            farm_in_request=req,
        )
    except Exception:
        logger.exception("farm-in: new-request notification failed for request %s", req_pk)


def _notify_updated(req_pk: int, title: str) -> None:
    """Tell the desk the requester changed an accepted trip; failures are logged only."""
    try:
        req = FarmInRequest.objects.get(pk=req_pk)
        Notification.notify(
            None,
            Notification.Kind.FARM_IN_UPDATED,
            title=title,
            detail=f"{req.pickup_summary} → {req.dropoff_summary}",
            farm_in_request=req,
        )
    except Exception:
        logger.exception("farm-in: update notification failed for request %s", req_pk)


def handle_quote(payload: dict, *, transaction_id: str) -> dict:
    """Price a quote request from the rate card. Persists nothing, not even the company."""
    summary = summarize(payload)
    amount = price_request(summary.vehicle, hourly=summary.hourly)
    if amount is None:
        return {"error": f"No rate on file for {summary.gnet_vehicle_type}"}
    return {"reservationId": transaction_id, "totalAmount": f"{amount:.2f}"}


class _RequesterRejected(Exception):
    """`ensure_requester` refused the payload; the message is GNet's error text."""


def _insert_new(payload: dict, *, transaction_id: str, delivery_id: str) -> FarmInRequest:
    """Create the requester (if new) and the request in one savepoint; may raise IntegrityError."""
    with transaction.atomic():
        try:
            company, company_created = ensure_requester(payload)
        except ValueError as exc:
            raise _RequesterRejected(str(exc)) from exc
        summary = summarize(payload)
        amount = price_request(summary.vehicle, hourly=summary.hourly)
        now = timezone.now()
        req = FarmInRequest.objects.create(
            transaction_id=transaction_id,
            requester_grid_id=company.gnet_grid_id,
            company=company,
            company_created=company_created,
            quoted_amount=amount,
            pricing_source=FarmInRequest.PricingSource.RATE_CARD if amount is not None else "",
            alert=_clip("alert", " · ".join(summary.alerts)),
            received_at=now,
            **_payload_fields(payload, delivery_id=delivery_id, now=now),
            **_summary_fields(summary),
        )
        req_pk = req.pk
        transaction.on_commit(lambda: _notify_new(req_pk))
    return req


def handle_new(payload: dict, *, transaction_id: str, delivery_id: str) -> dict:
    """Store a new farm-in request once per transaction id and answer with its token.

    A re-delivery replays the stored response without re-pricing; one that says CANCEL is
    not applied but flags the row for review (cancels belong on `farm_in.update`). Concurrent first
    deliveries are settled by unique indexes: the savepoint covers the requester company and
    the request insert, so a loser rolls back only its savepoint. If it collided on its own
    transaction id, the winner's row is that request and is replayed. If it collided on the
    company's griddID (a different transaction from the same never-seen requester), no row
    exists for its id yet, so the savepoint runs once more: the company now exists and
    `ensure_requester` finds it under lock. `ensure_requester` runs first so that, when the
    company exists, its row lock queues same-requester deliveries.
    """
    existing = FarmInRequest.objects.filter(transaction_id=transaction_id).first()
    if existing:
        if _is_cancel(payload) and existing.status != FarmInRequest.Status.CANCELED:
            _flag_cancel_as_new(existing, transaction_id)
        return stored_response(existing)
    try:
        req = _insert_or_replay(payload, transaction_id=transaction_id, delivery_id=delivery_id)
    except _RequesterRejected as exc:
        return {"error": str(exc)}
    return stored_response(req)


def _is_cancel(payload: dict) -> bool:
    """Whether any of the payload's status fields says CANCEL (any case)."""
    affiliate = _dict(payload.get("affiliateReservation"))
    values = (payload.get("status"), affiliate.get("action"), affiliate.get("status"))
    return any(str(v or "").strip().upper() == GNET_CANCEL for v in values)


def _flag_cancel_as_new(req: FarmInRequest, transaction_id: str) -> None:
    """A cancel sent as `farm_in.new` is not applied; the desk is asked to review the row."""
    req.alert = ALERT_CANCEL_AS_NEW
    req.last_event_at = timezone.now()
    req.save(update_fields=["alert", "last_event_at"])
    logger.warning("farm-in: cancel received as a new delivery for transaction %s", transaction_id)


def _insert_or_replay(
    payload: dict, *, transaction_id: str, delivery_id: str, retry: bool = True
) -> FarmInRequest:
    """Insert, or on a unique-index collision return this transaction's row; retry once."""
    try:
        return _insert_new(payload, transaction_id=transaction_id, delivery_id=delivery_id)
    except IntegrityError:
        req = FarmInRequest.objects.filter(transaction_id=transaction_id).first()
        if req is not None:
            return req
        if not retry:
            raise
    return _insert_or_replay(
        payload, transaction_id=transaction_id, delivery_id=delivery_id, retry=False
    )


def _store_delivery(req: FarmInRequest, payload: dict, *, delivery_id: str) -> None:
    """Overwrite the row's copy of the delivery with this event's."""
    fields = _payload_fields(payload, delivery_id=delivery_id, now=timezone.now())
    for name, value in fields.items():
        setattr(req, name, value)


def _cancel(req: FarmInRequest) -> None:
    """The requester canceled an open row; an accepted trip is flagged, its order kept."""
    if req.status == FarmInRequest.Status.ACCEPTED:
        req.updated_after_accept = True
        req.alert = ALERT_CANCELED
        req_pk = req.pk
        transaction.on_commit(lambda: _notify_updated(req_pk, "GNet requester canceled"))
    req.status = FarmInRequest.Status.CANCELED


def _rederive(req: FarmInRequest, summary: Summary) -> None:
    """Re-run the summary and rate-card price on a pending row; a manual price stands."""
    for name, value in _summary_fields(summary).items():
        setattr(req, name, value)
    if req.pricing_source != FarmInRequest.PricingSource.MANUAL:
        amount = price_request(summary.vehicle, hourly=summary.hourly)
        req.quoted_amount = amount
        req.pricing_source = FarmInRequest.PricingSource.RATE_CARD if amount is not None else ""
    req.alert = _clip("alert", " · ".join([ALERT_UPDATED, *summary.alerts]))


def handle_update(payload: dict, *, transaction_id: str, delivery_id: str) -> dict:
    """Apply a requester's change or cancellation to the stored request (spec §5.4).

    Closed rows refuse every update, a cancel included, except that a repeated cancel on a
    canceled row is answered as a success and changes nothing.
    """
    with transaction.atomic():
        rows = FarmInRequest.objects.select_for_update()
        req = rows.filter(transaction_id=transaction_id).first()
        if req is None:
            return {"error": "unknown transaction"}
        is_cancel = _gnet_status(payload) == GNET_CANCEL
        if is_cancel and req.status == FarmInRequest.Status.CANCELED:
            return {"reservationId": req.reservation_id_token}
        if req.status in _CLOSED:
            return {"error": f"request is {req.status}"}
        if is_cancel:
            _cancel(req)
            _store_delivery(req, payload, delivery_id=delivery_id)
            req.save()
            return {"reservationId": req.reservation_id_token}
        if req.status == FarmInRequest.Status.PENDING:
            _rederive(req, summarize(payload))
        else:
            req.updated_after_accept = True
            req.alert = ALERT_UPDATED_AFTER_ACCEPT
            req_pk = req.pk
            transaction.on_commit(
                lambda: _notify_updated(req_pk, "GNet requester updated an accepted trip")
            )
        _store_delivery(req, payload, delivery_id=delivery_id)
        req.save()
        return stored_response(req)


def receive(event: str, payload: dict, *, transaction_id: str | int, delivery_id: str) -> dict:
    """Route one gateway delivery to its handler and return the body GNet gets back."""
    transaction_id = str(transaction_id or "").strip()
    if event not in (EVENT_QUOTE, EVENT_NEW, EVENT_UPDATE):
        return {"error": f"unsupported event {event}"}
    if not transaction_id:
        return {"error": "transaction id missing"}
    if event == EVENT_QUOTE:
        return handle_quote(payload, transaction_id=transaction_id)
    if event == EVENT_NEW:
        return handle_new(payload, transaction_id=transaction_id, delivery_id=delivery_id)
    return handle_update(payload, transaction_id=transaction_id, delivery_id=delivery_id)
