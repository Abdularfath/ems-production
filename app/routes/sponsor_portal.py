from flask import Blueprint, render_template, session, redirect, url_for, flash
from app.firebase_config import db
from app.decorators import login_required, role_required

sponsor_portal_bp = Blueprint('sponsor_portal', __name__, url_prefix='/sponsor')

@sponsor_portal_bp.route('/dashboard')
@login_required
@role_required('sponsor')
def dashboard():
    """
    Sponsor's main dashboard.
    Shows all events where this sponsor's email matches a sponsor record.
    """
    sponsor_email = session.get('email')

    # Search all events for sponsor records matching this email
    all_events = db.collection('events').where('status', '==', 'published').stream()

    my_sponsorships = []

    for event_doc in all_events:
        event_data = {**event_doc.to_dict(), 'id': event_doc.id}

        # Check if this sponsor email exists in this event's sponsors
        matching = db.collection('events').document(event_doc.id)\
                     .collection('sponsors')\
                     .where('email', '==', sponsor_email)\
                     .limit(1).stream()

        matching_list = list(matching)
        if matching_list:
            sponsor_record = {**matching_list[0].to_dict(), 'id': matching_list[0].id}

            # Fetch deliverables
            deliverables_docs = db.collection('events').document(event_doc.id)\
                                  .collection('sponsors').document(sponsor_record['id'])\
                                  .collection('deliverables').stream()
            deliverables = [d.to_dict() for d in deliverables_docs]

            total_d     = len(deliverables)
            completed_d = sum(1 for d in deliverables if d.get('status') == 'completed')
            progress    = int((completed_d / total_d) * 100) if total_d > 0 else 0

            my_sponsorships.append({
                'event':       event_data,
                'sponsor':     sponsor_record,
                'deliverables': deliverables,
                'total_d':     total_d,
                'completed_d': completed_d,
                'progress':    progress
            })

    return render_template('sponsor/dashboard.html',
                           my_sponsorships=my_sponsorships)

@sponsor_portal_bp.route('/event/<event_id>')
@login_required
@role_required('sponsor')
def event_detail(event_id):
    """
    Sponsor's detailed view for a specific event sponsorship.
    """
    sponsor_email = session.get('email')

    event_doc = db.collection('events').document(event_id).get()
    if not event_doc.exists:
        flash('Event not found.', 'danger')
        return redirect(url_for('sponsor_portal.dashboard'))

    event_data = {**event_doc.to_dict(), 'id': event_doc.id}

    # Find this sponsor's record
    matching = db.collection('events').document(event_id)\
                 .collection('sponsors')\
                 .where('email', '==', sponsor_email)\
                 .limit(1).stream()

    matching_list = list(matching)
    if not matching_list:
        flash('You are not a sponsor for this event.', 'danger')
        return redirect(url_for('sponsor_portal.dashboard'))

    sponsor_record = {**matching_list[0].to_dict(), 'id': matching_list[0].id}

    # Fetch deliverables
    deliverables_docs = db.collection('events').document(event_id)\
                          .collection('sponsors').document(sponsor_record['id'])\
                          .collection('deliverables').stream()
    deliverables = [{**d.to_dict(), 'id': d.id} for d in deliverables_docs]

    total_d     = len(deliverables)
    completed_d = sum(1 for d in deliverables if d.get('status') == 'completed')
    progress    = int((completed_d / total_d) * 100) if total_d > 0 else 0

    return render_template('sponsor/event_detail.html',
                           event=event_data,
                           event_id=event_id,
                           sponsor=sponsor_record,
                           deliverables=deliverables,
                           total_d=total_d,
                           completed_d=completed_d,
                           progress=progress)