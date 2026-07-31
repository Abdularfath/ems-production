from flask import Blueprint, render_template, session, redirect, url_for, flash, request, jsonify
from app.firebase_config import db
from app.decorators import login_required, role_required
from app.utils.qr_utils import verify_qr_payload
from google.cloud.firestore import SERVER_TIMESTAMP

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

    is_exhibitor = sponsor_record.get('is_exhibitor', False)

    # Leads + ROI — the sponsor's own performance view, not organizer-mediated
    from google.cloud.firestore import Query
    leads_docs = (
        db.collection('events').document(event_id)
        .collection('sponsors').document(sponsor_record['id'])
        .collection('leads')
        .order_by('connected_at', direction=Query.DESCENDING)
        .stream()
    )
    leads = [{**l.to_dict(), 'id': l.id} for l in leads_docs]
    leads_count = len(leads)

    amount_paid = sponsor_record.get('amount', 0)
    cost_per_lead = round(amount_paid / leads_count, 2) if leads_count > 0 else None

    return render_template('sponsor/event_detail.html',
                           event=event_data,
                           event_id=event_id,
                           sponsor=sponsor_record,
                           deliverables=deliverables,
                           total_d=total_d,
                           completed_d=completed_d,
                           progress=progress,
                           is_exhibitor=is_exhibitor,
                           leads=leads,
                           leads_count=leads_count,
                           cost_per_lead=cost_per_lead)

@sponsor_portal_bp.route('/event/<event_id>/scanner')
@login_required
@role_required('sponsor')
def lead_scanner(event_id):
    """QR scanner page — sponsor scans an attendee's ticket QR at their booth
    to auto-capture a lead, instead of relying on the attendee to click Connect."""
    sponsor_email = session.get('email')

    event_doc = db.collection('events').document(event_id).get()
    if not event_doc.exists:
        flash('Event not found.', 'danger')
        return redirect(url_for('sponsor_portal.dashboard'))

    matching = db.collection('events').document(event_id)\
                 .collection('sponsors')\
                 .where('email', '==', sponsor_email)\
                 .limit(1).stream()
    matching_list = list(matching)
    if not matching_list:
        flash('You are not a sponsor for this event.', 'danger')
        return redirect(url_for('sponsor_portal.dashboard'))

    sponsor_record = {**matching_list[0].to_dict(), 'id': matching_list[0].id}

    return render_template('sponsor/scanner.html',
                           event=event_doc.to_dict(), event_id=event_id,
                           sponsor=sponsor_record)


@sponsor_portal_bp.route('/event/<event_id>/scan-lead', methods=['POST'])
@login_required
@role_required('sponsor')
def scan_lead(event_id):
    """Verifies a scanned attendee ticket QR and records a lead — same HMAC
    verification your organizer check-in scanner already uses."""
    sponsor_email = session.get('email')

    matching = db.collection('events').document(event_id)\
                 .collection('sponsors')\
                 .where('email', '==', sponsor_email)\
                 .limit(1).stream()
    matching_list = list(matching)
    if not matching_list:
        return jsonify({'success': False, 'message': 'Unauthorized.'}), 403

    sponsor_id = matching_list[0].id

    data = request.get_json()
    payload = data.get('qr_payload')
    if not payload:
        return jsonify({'success': False, 'message': 'No QR code payload provided.'}), 400

    is_valid, reg_id, payload_event_id = verify_qr_payload(payload)
    if not is_valid:
        return jsonify({'success': False, 'message': '❌ Invalid or forged QR Code!'}), 400

    if payload_event_id != event_id:
        return jsonify({'success': False, 'message': '❌ This ticket is for a different event!'}), 400

    reg_doc = db.collection('registrations').document(reg_id).get()
    if not reg_doc.exists:
        return jsonify({'success': False, 'message': '❌ Registration not found.'}), 404

    reg_data = reg_doc.to_dict()

    if reg_data.get('status') not in ('confirmed', 'checked_in'):
        return jsonify({'success': False, 'message': '❌ This ticket is not a valid active registration.'}), 400

    leads_ref = (
        db.collection('events').document(event_id)
        .collection('sponsors').document(sponsor_id)
        .collection('leads')
    )

    existing = leads_ref.where('attendee_uid', '==', reg_data.get('attendee_uid')).limit(1).stream()
    if list(existing):
        return jsonify({
            'success': True,
            'already_captured': True,
            'message': f"⚠️ {reg_data.get('attendee_name')} is already one of your leads.",
            'attendee_name': reg_data.get('attendee_name'),
        }), 200

    leads_ref.add({
        'attendee_uid':   reg_data.get('attendee_uid'),
        'attendee_name':  reg_data.get('attendee_name'),
        'attendee_email': reg_data.get('attendee_email'),
        'connected_at':   SERVER_TIMESTAMP,
        'source':         'qr_scan',
    })

    return jsonify({
        'success': True,
        'already_captured': False,
        'message': f"✅ Lead captured: {reg_data.get('attendee_name')}",
        'attendee_name': reg_data.get('attendee_name'),
    }), 200