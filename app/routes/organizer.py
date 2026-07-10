from flask import Blueprint, render_template, session, request, flash, redirect, url_for
from app.firebase_config import db
from app.decorators import login_required, role_required
from datetime import datetime, timezone
from google.cloud.firestore import SERVER_TIMESTAMP

organizer_bp = Blueprint('organizer', __name__, url_prefix='/organizer')

@organizer_bp.route('/dashboard')
@login_required
@role_required('organizer')
def dashboard():
    """Displays the organizer dashboard with high-level analytics."""
    uid = session.get('uid')
    
    # 1. Fetch all events created by this organizer
    events_ref = db.collection('events').where('organizer_uid', '==', uid).stream()
    events = [{**e.to_dict(), 'id': e.id} for e in events_ref]
    
    # 2. Calculate Dashboard Metrics
    total_events = len(events)
    total_revenue = sum(e.get('total_revenue', 0) for e in events)
    total_tickets = sum(e.get('total_registrations', 0) for e in events)
    total_checkins = sum(e.get('total_checkins', 0) for e in events)
    
    # 3. Fetch Recent Registrations (Activity Feed) - No index required!
    event_ids = [e['id'] for e in events]
    recent_regs = []
    
    if event_ids:
        chunks = [event_ids[i:i + 10] for i in range(0, len(event_ids), 10)]
        for chunk in chunks:
            # Only do a simple query to avoid the index error
            regs_query = db.collection('registrations').where('event_id', 'in', chunk).stream()
            
            # Filter in Python instead of Firestore
            for r in regs_query:
                reg_data = r.to_dict()
                if reg_data.get('status') in ['confirmed', 'checked_in']:
                    recent_regs.append({**reg_data, 'id': r.id})
            
        # Sort chronologically in Python
        # Use a real timezone-aware datetime object instead of a string to prevent type errors!
        fallback_date = datetime(1970, 1, 1, tzinfo=timezone.utc)
        recent_regs.sort(key=lambda x: x.get('created_at') or fallback_date, reverse=True)
        recent_regs = recent_regs[:5] # Keep only the top 5 newest

    return render_template(
        'organizer/dashboard.html', 
        total_events=total_events,
        total_revenue=total_revenue,
        total_tickets=total_tickets,
        total_checkins=total_checkins,
        recent_regs=recent_regs
    )


@organizer_bp.route('/platform-feedback', methods=['GET', 'POST'])
@login_required
@role_required('organizer')
def platform_feedback():
    """Lets an organizer rate/comment on the EMS platform itself (not a
    specific event). Visible to Super Admin only — this is product feedback,
    not an event review."""
    uid = session.get('uid')

    if request.method == 'POST':
        rating = int(request.form.get('rating', 5))
        comments = request.form.get('comments', '').strip()

        db.collection('platform_feedback').add({
            'organizer_uid':  uid,
            'organizer_name': session.get('name', ''),
            'organizer_email': session.get('email', ''),
            'rating':         rating,
            'comments':       comments,
            'created_at':     SERVER_TIMESTAMP,
        })

        flash('Thank you for your feedback! It helps us improve EMS.', 'success')
        return redirect(url_for('organizer.dashboard'))

    return render_template('organizer/platform_feedback.html')