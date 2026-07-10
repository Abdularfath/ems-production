from datetime import datetime, timezone


def is_event_over(event_data):
    """
    Single source of truth for "has this event concluded?"

    True when EITHER:
      - the organizer has manually marked the event 'completed' (lets an
        organizer close out an event early, e.g. cut a 3-day conference short), OR
      - the event's real end_datetime has actually passed (automatic, no
        manual step required).

    Used by: certificate generation eligibility, the attendee Past/Upcoming
    tab split, and the "Generate Certificates for All" button visibility.
    Keeping this in one function guarantees all three always agree.
    """
    if event_data.get('status') == 'completed':
        return True

    end_dt = event_data.get('end_datetime')
    if end_dt:
        if getattr(end_dt, 'tzinfo', None) is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > end_dt

    return False