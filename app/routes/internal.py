import os
from flask import Blueprint, request, jsonify
from datetime import datetime, timedelta, timezone
from app.firebase_config import db
from app.utils.email_utils import send_ticket_email
from app.utils.notification_utils import create_notification

internal_bp = Blueprint('internal', __name__, url_prefix='/internal')


def _check_token():
    expected = os.getenv('INTERNAL_CRON_SECRET')
    provided = request.args.get('token')
    return expected and provided and provided == expected


@internal_bp.route('/send-reminders', methods=['GET', 'POST'])
def send_reminders():
    """
    Call this hourly from an external cron service (e.g. cron-job.org) with
    ?token=<INTERNAL_CRON_SECRET>. Finds events starting in the next 24h and
    emails + notifies every confirmed/checked-in attendee who hasn't already
    been reminded for that registration.
    """
    if not _check_token():
        return jsonify({'error': 'Unauthorized'}), 403

    now = datetime.now(timezone.utc)
    window_end = now + timedelta(hours=24)

    events_docs = (
        db.collection('events')
        .where('status', '==', 'published')
        .where('start_datetime', '>=', now)
        .where('start_datetime', '<=', window_end)
        .stream()
    )

    reminders_sent = 0

    for e_doc in events_docs:
        event = {**e_doc.to_dict(), 'id': e_doc.id}
        event_id = event['id']

        reg_docs = (
            db.collection('registrations')
            .where('event_id', '==', event_id)
            .where('status', 'in', ['confirmed', 'checked_in'])
            .stream()
        )

        for r_doc in reg_docs:
            reg = r_doc.to_dict()
            if reg.get('reminder_sent'):
                continue

            attendee_name = reg.get('attendee_name', 'Attendee')
            start_str = event['start_datetime'].strftime('%B %d, %Y at %I:%M %p')

            try:
                send_ticket_email(
                    to_email=reg.get('attendee_email'),
                    subject=f"Reminder: {event.get('name')} is coming up!",
                    html_content=f"""
                        <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
                            <h2 style="color:#0d6efd;">See you soon!</h2>
                            <p>Hi {attendee_name},</p>
                            <p>This is a reminder that <strong>{event.get('name')}</strong> is happening on
                               <strong>{start_str}</strong> — less than 24 hours from now.</p>
                            <p>Don't forget your QR code for check-in.</p>
                        </div>
                    """
                )
            except Exception as ex:
                print(f"[ERROR] Reminder email failed for {reg.get('attendee_email')}: {ex}")

            create_notification(
                reg['attendee_uid'], f"{event.get('name')} is coming up!",
                f"Starts {start_str}. Don't forget your ticket.",
                event_id=event_id, notif_type='reminder'
            )

            db.collection('registrations').document(r_doc.id).update({'reminder_sent': True})
            reminders_sent += 1

    return jsonify({'success': True, 'reminders_sent': reminders_sent})