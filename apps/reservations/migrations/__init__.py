def backfill_flight_direction(*, Reservation, Stop) -> None:
    """First airport stop → arrival, last → departure; middle stops stay blank (a person
    chooses those). Runs on the historical models inside 0010 and on the live ones in tests."""
    ids = Stop.objects.filter(airport__isnull=False).values_list("reservation_id", flat=True)
    for res in Reservation.objects.filter(pk__in=set(ids)):
        stops = list(res.stops.order_by("sequence"))
        for i, stop in enumerate(stops):
            if stop.airport_id is None:
                continue
            if i == 0:
                stop.flight_direction = "arrival"
            elif i == len(stops) - 1:
                stop.flight_direction = "departure"
            else:
                continue
            stop.save(update_fields=["flight_direction"])
