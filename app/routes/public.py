from flask import (Blueprint, render_template, request, redirect, jsonify,
                        abort, flash, redirect, url_for, session)
from app.firebase_config import db
from app.utils.event_utils import is_event_over
from google.cloud.firestore import Query
 
public_bp = Blueprint('public', __name__)
 
 
# ── PUBLIC HOME — event listing ─────────────────────────────────────
@public_bp.route('/')
def index():
    search = request.args.get('q', '').strip().lower()
 
    # Fetch all published events ordered by start date
    docs = (
        db.collection('events')
        .where('status', '==', 'published')
        .order_by('start_datetime')
        .stream()
    )
 
    events = []
    for doc in docs:
        data = {**doc.to_dict(), 'id': doc.id}

        # Ended events are no longer discoverable through browsing — the detail
        # page/registration links still work for anyone who already has them
        # (receipts, certificates, "view my event"), but they don't surface
        # here for new visitors to find and register for.
        if is_event_over(data):
            continue
 
        # Get venue name for display on the card
        venue_name = 'Venue TBD'
        if data.get('venue_id'):
            v = db.collection('venues').document(data['venue_id']).get()
            if v.exists:
                venue_name = v.to_dict().get('name', 'Unknown Venue')
        data['venue_name'] = venue_name
 
        # Client-side keyword search filter
        if search:
            searchable = f"{data.get('name','')} {venue_name}".lower()
            if search not in searchable:
                continue
 
        events.append(data)
 
    return render_template('public/index.html',
                           events=events, search=search)
 
 
# ── EVENT DETAIL PAGE ───────────────────────────────────────────────
@public_bp.route('/events/<event_id>')
def event_detail(event_id):
    # Fetch the event document
    doc = db.collection('events').document(event_id).get()
    if not doc.exists:
        abort(404)
 
    event = {**doc.to_dict(), 'id': doc.id}
 
    # Show published events, AND completed ones (an event manually marked
    # "completed" or whose date has simply passed must still be reachable —
    # attendees need this page for their certificates, ticket history, etc.).
    # Draft and cancelled events stay hidden from the public.
    if event.get('status') not in ('published', 'completed'):
        abort(404)

    event_is_over = is_event_over(event)
 
    # Fetch venue details
    venue = None
    if event.get('venue_id'):
        v = db.collection('venues').document(event['venue_id']).get()
        if v.exists:
            venue = v.to_dict()
 
    # Fetch ticket types from subcollection
    ticket_docs = (
        db.collection('events')
        .document(event_id)
        .collection('ticket_types')
        .where('is_active', '==', True)
        .stream()
    )
    ticket_types = [{**t.to_dict(), 'id': t.id} for t in ticket_docs]
 
    # Calculate availability for each ticket type
    for tt in ticket_types:
        available = tt.get('quantity_total', 0) - tt.get('quantity_sold', 0)
        tt['available'] = max(0, available)
        tt['sold_out']  = available <= 0

         # ADD THIS DEBUG LINE:
    print(f"[DEBUG] Event object keys: {event.keys()}")
    print(f"[DEBUG] Event ID from object: {event.get('id', 'NOT FOUND')}")

        # 1. Fetch Agenda / Sessions
    sessions_ref = db.collection('events').document(event_id).collection('sessions').order_by('date').order_by('start_time').stream()
    agenda = []
    
    for s in sessions_ref:
        s_dict = s.to_dict()
        s_dict['id'] = s.id
        
        # 2. Fetch assigned speakers for this session
        speakers = []
        sp_docs = s.reference.collection('session_speakers').stream()
        for sp in sp_docs:
            sp_id = sp.to_dict().get('speaker_id')
            speaker_doc = db.collection('speakers').document(sp_id).get()
            if speaker_doc.exists:
                speakers.append({**speaker_doc.to_dict(), 'id': speaker_doc.id})
        
        s_dict['speakers'] = speakers
        agenda.append(s_dict)

    # 3. Fetch user's saved sessions (if logged in as attendee)
    saved_session_ids = []
    if session.get('uid') and session.get('role') == 'attendee':
        # We will store saved sessions using the session_id as the document ID
        saved_docs = db.collection('attendees').document(session.get('uid')).collection('saved_sessions').where('event_id', '==', event_id).stream()
        saved_session_ids = [d.id for d in saved_docs]

    # Fetch sponsors and exhibitors for public display
    sponsors_docs = db.collection('events').document(event_id)\
                      .collection('sponsors').stream()
    all_sponsors = [{**s.to_dict(), 'id': s.id} for s in sponsors_docs]

    # Separate into sponsors and exhibitors
    sponsors   = [s for s in all_sponsors if not s.get('is_exhibitor', False)]
    exhibitors = [s for s in all_sponsors if s.get('is_exhibitor', False)]

    # Public-facing rating: average + count only, individual comments stay
    # organizer-only (attendees never explicitly consented to public display
    # of their written feedback).
    feedback_docs = db.collection('events').document(event_id).collection('feedback').stream()
    ratings = [f.to_dict().get('rating', 0) for f in feedback_docs]
    avg_rating = round(sum(ratings) / len(ratings), 1) if ratings else None
    review_count = len(ratings)

    return render_template('public/event_detail.html',
                           event=event,
                           venue=venue,
                           ticket_types=ticket_types,
                           agenda=agenda,
                           saved_session_ids=saved_session_ids,
                           sponsors=sponsors,
                           exhibitors=exhibitors,
                           event_is_over=event_is_over,
                           avg_rating=avg_rating,
                           review_count=review_count)
@public_bp.route('/api/events/<event_id>/validate_promo/<code>', methods=['GET'])
def validate_promo(event_id, code):
    """API Endpoint to validate a promo code via AJAX."""
    code = code.upper().strip()
    doc_ref = db.collection('events').document(event_id).collection('promo_codes').document(code)
    doc = doc_ref.get()

    if not doc.exists:
        return jsonify({"valid": False, "message": "Invalid promo code."})

    promo = doc.to_dict()
    if not promo.get('active', True):
        return jsonify({"valid": False, "message": "This promo code is no longer active."})

    max_uses = promo.get('max_uses', 0)
    current_uses = promo.get('current_uses', 0)

    if max_uses > 0 and current_uses >= max_uses:
        return jsonify({"valid": False, "message": "This promo code has reached its usage limit."})

    # Save the valid promo in the Flask session so the checkout page can automatically apply it later!
    session['applied_promo'] = {
        'code': code,
        'discount_percentage': promo.get('discount_percentage'),
        'event_id': event_id
    }

    return jsonify({
        "valid": True,
        "message": f"Success! {promo.get('discount_percentage')}% discount will be applied at checkout."
    })
