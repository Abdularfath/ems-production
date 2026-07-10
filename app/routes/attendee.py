

from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from app.firebase_config import db
from app.decorators import login_required, role_required
from datetime import datetime, timezone
from google.cloud.firestore import SERVER_TIMESTAMP
from app.utils.event_utils import is_event_over

# 1. Define the Blueprint (This was missing!)
attendee_bp = Blueprint('attendee', __name__, url_prefix='/attendee')

# 2. The Route
from datetime import datetime, timezone

@attendee_bp.route('/my-events')
@login_required
@role_required('attendee')
def my_events():
    uid = session.get('uid')

    # Fetch all registrations for this attendee
    regs_docs = db.collection('registrations') \
                  .where('attendee_uid', '==', uid) \
                  .where('status', 'in', ['confirmed', 'checked_in']) \
                  .stream()

    upcoming = []
    past = []

    for r in regs_docs:
        reg = {**r.to_dict(), 'id': r.id}

        event_doc = db.collection('events').document(reg.get('event_id')).get()
        if not event_doc.exists:
            continue

        event_data = {**event_doc.to_dict(), 'id': event_doc.id}
        reg['event'] = event_data

        # Same function used for certificate eligibility — Past/Upcoming split
        # now always agrees with whether certificates are available yet.
        if is_event_over(event_data):
            past.append(reg)
        else:
            upcoming.append(reg)

    # Sort using the REAL field name (start_datetime/end_datetime) — the old
    # code sorted on 'start_date'/'end_date', which don't exist on your event
    # documents, so ordering silently did nothing before.
    upcoming.sort(key=lambda x: x['event'].get('start_datetime') or datetime.min.replace(tzinfo=timezone.utc))
    past.sort(key=lambda x: x['event'].get('end_datetime') or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    return render_template('attendee/my_events.html',
                           upcoming=upcoming,
                           past=past)


@attendee_bp.route('/my-certificates')
@login_required
@role_required('attendee')
def my_certificates():
    uid = session.get('uid')
    reg_docs = db.collection('registrations').where('attendee_uid', '==', uid).stream()

    certificates = []
    for r in reg_docs:
        reg = r.to_dict()
        if not reg.get('certificate_url'):
            continue
        event_doc = db.collection('events').document(reg['event_id']).get()
        event_data = event_doc.to_dict() if event_doc.exists else {}
        certificates.append({
            'reg_id':          r.id,
            'event_name':      event_data.get('name', 'Unknown Event'),
            'certificate_url': reg['certificate_url'],
            'generated_at':    reg.get('certificate_generated_at'),
        })

    certificates.sort(
        key=lambda c: c['generated_at'] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True
    )

    return render_template('attendee/my_certificates.html', certificates=certificates)


@attendee_bp.route('/notifications')
@login_required
@role_required('attendee')
def list_notifications():
    """Returns the attendee's most recent in-app notifications as JSON, for the navbar bell."""
    from flask import jsonify
    from google.cloud.firestore import Query

    uid = session.get('uid')
    docs = (db.collection('notifications').document(uid).collection('items')
            .order_by('created_at', direction=Query.DESCENDING)
            .limit(15).stream())

    items = []
    unread_count = 0
    for d in docs:
        n = d.to_dict()
        if not n.get('read'):
            unread_count += 1
        items.append({
            'id':         d.id,
            'title':      n.get('title', ''),
            'message':    n.get('message', ''),
            'type':       n.get('type', 'info'),
            'read':       n.get('read', False),
            'event_id':   n.get('event_id'),
            'created_at': n.get('created_at').strftime('%b %d, %H:%M') if n.get('created_at') else '',
        })

    return jsonify({'notifications': items, 'unread_count': unread_count})


@attendee_bp.route('/notifications/mark-all-read', methods=['POST'])
@login_required
@role_required('attendee')
def mark_all_notifications_read():
    from flask import jsonify

    uid = session.get('uid')
    docs = (db.collection('notifications').document(uid).collection('items')
            .where('read', '==', False).stream())
    for d in docs:
        db.collection('notifications').document(uid).collection('items').document(d.id).update({'read': True})

    return jsonify({'success': True})


@attendee_bp.route('/save_session/<event_id>/<session_id>', methods=['POST'])
@login_required
@role_required('attendee')
def save_session(event_id, session_id):
    """Toggles saving a session to the attendee's personal itinerary."""
    uid = session.get('uid')
    doc_ref = db.collection('attendees').document(uid).collection('saved_sessions').document(session_id)
    
    # Toggle logic: If it exists, delete it (unsave). If it doesn't, create it (save).
    if doc_ref.get().exists:
        doc_ref.delete()
        flash('Session removed from your itinerary.', 'info')
    else:
        doc_ref.set({'event_id': event_id})
        flash('Session saved to your itinerary!', 'success')
        
    # Redirect back to the public event details page
    # IMPORTANT: Change 'public.event_details' if your route is named differently!
    return redirect(url_for('public.event_detail', event_id=event_id))

from google.cloud.firestore import SERVER_TIMESTAMP

@attendee_bp.route('/<event_id>/<ticket_type_id>/waitlist', methods=['POST'])
@login_required
@role_required('attendee')
def join_waitlist(event_id, ticket_type_id):
    uid = session.get('uid')
    email = session.get('email')

    event_doc = db.collection('events').document(event_id).get()
    if not event_doc.exists or is_event_over(event_doc.to_dict()):
        flash('This event has already ended — waitlist is closed.', 'warning')
        return redirect(url_for('public.event_detail', event_id=event_id))

    # Check if they are already on the waitlist
    existing = db.collection('events').document(event_id).collection('waitlist') \
                 .where('attendee_uid', '==', uid) \
                 .where('ticket_type_id', '==', ticket_type_id) \
                 .limit(1).stream()
    
    if len(list(existing)) > 0:
        flash('You are already on the waitlist for this ticket!', 'info')
        return redirect(url_for('public.event_detail', event_id=event_id))

    # Add to waitlist
    db.collection('events').document(event_id).collection('waitlist').add({
        'attendee_uid': uid,
        'attendee_email': email,
        'ticket_type_id': ticket_type_id,
        'status': 'waiting',
        'joined_at': SERVER_TIMESTAMP
    })

    flash('You have been added to the waitlist! We will email you if a spot opens up.', 'success')
    return redirect(url_for('public.event_detail', event_id=event_id))



@attendee_bp.route('/feedback/<event_id>/<registration_id>', methods=['GET', 'POST'])
@login_required
@role_required('attendee')
def submit_feedback(event_id, registration_id):
    # Ownership + eligibility check: only the attendee who actually checked
    # in, and only after the event has ended, can leave a review. Prevents
    # reviews from no-shows or before the event has even happened.
    reg_doc = db.collection('registrations').document(registration_id).get()
    if not reg_doc.exists or reg_doc.to_dict().get('attendee_uid') != session.get('uid'):
        flash('Registration not found.', 'danger')
        return redirect(url_for('attendee.my_events'))

    reg_data = reg_doc.to_dict()
    if reg_data.get('status') != 'checked_in':
        flash('Reviews can only be submitted by attendees who checked in.', 'warning')
        return redirect(url_for('attendee.my_events'))

    event_doc_check = db.collection('events').document(event_id).get()
    if not event_doc_check.exists or not is_event_over(event_doc_check.to_dict()):
        flash('You can leave a review once the event has ended.', 'warning')
        return redirect(url_for('attendee.my_events'))

    # Check if feedback already exists to prevent duplicates
    feedback_ref = db.collection('events').document(event_id).collection('feedback').document(registration_id)
    
    if feedback_ref.get().exists:
        flash('You have already submitted feedback for this event. Thank you!', 'info')
        return redirect(url_for('attendee.my_events'))

    if request.method == 'POST':
        rating = int(request.form.get('rating', 5))
        comments = request.form.get('comments', '').strip()
        
        feedback_ref.set({
            'attendee_uid': session.get('uid'),
            'attendee_name': session.get('name'),
            'rating': rating,
            'comments': comments,
            'submitted_at': SERVER_TIMESTAMP
        })
        flash('Thank you for your feedback! Your review has been saved.', 'success')
        return redirect(url_for('attendee.my_events'))
        
    # GET request - show the form
    event_doc = db.collection('events').document(event_id).get()
    event_data = {**event_doc.to_dict(), 'id': event_id} if event_doc.exists else {}
    
    return render_template('attendee/feedback.html', event=event_data, registration_id=registration_id)

@attendee_bp.route('/connect_sponsor/<event_id>/<sponsor_id>', methods=['POST'])
@login_required
@role_required('attendee')
def connect_sponsor(event_id, sponsor_id):
    uid   = session.get('uid')
    email = session.get('email')
    name  = session.get('name', '')

    event_doc = db.collection('events').document(event_id).get()
    if not event_doc.exists or is_event_over(event_doc.to_dict()):
        flash('This event has ended — sponsor connections are closed.', 'warning')
        return redirect(url_for('public.event_detail', event_id=event_id))

    # Check if already connected
    existing = db.collection('events').document(event_id)\
                 .collection('sponsors').document(sponsor_id)\
                 .collection('leads')\
                 .where('attendee_uid', '==', uid)\
                 .limit(1).stream()

    if list(existing):
        flash('You are already connected with this sponsor!', 'info')
        return redirect(url_for('public.event_detail', event_id=event_id))

    # Add lead
    from google.cloud.firestore import SERVER_TIMESTAMP
    db.collection('events').document(event_id)\
      .collection('sponsors').document(sponsor_id)\
      .collection('leads').add({
          'attendee_uid':   uid,
          'attendee_name':  name,
          'attendee_email': email,
          'connected_at':   SERVER_TIMESTAMP
      })

    flash('Successfully connected with sponsor!', 'success')
    return redirect(url_for('public.event_detail', event_id=event_id))