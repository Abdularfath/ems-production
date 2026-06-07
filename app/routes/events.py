from flask import (Blueprint, render_template, redirect,
                        url_for, session, flash, request)
from app.firebase_config import db
from app.decorators import login_required, role_required
from app.utils.validators import validate_event
from datetime import datetime
from google.cloud.firestore import SERVER_TIMESTAMP
import csv
from io import StringIO
from flask import Response
from google.cloud.firestore import Increment
from io import BytesIO
from flask import make_response
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib import colors
from google.cloud.firestore import Query
 
events_bp = Blueprint('events', __name__, url_prefix='/organizer/events')
 
FMT = '%Y-%m-%dT%H:%M'  # datetime-local HTML input format
 
 
# ── Helper: get event + verify ownership ─────────────────────────────
def get_event_or_403(event_id):
    doc = db.collection('events').document(event_id).get()
    if not doc.exists:
        return None, None
    data = doc.to_dict()
    if data.get('organizer_uid') != session.get('uid'):
        return None, 'forbidden'
    return doc, data
 
 
# ── Helper: get organizer's venues for dropdown ───────────────────────
def get_my_venues():
    docs = (
        db.collection('venues')
        .where('organizer_uid','==',session['uid'])
        .order_by('name')
        .stream()
    )
    return [{**d.to_dict(),'id':d.id} for d in docs]
 
 
# ── LIST organizer's events ───────────────────────────────────────────
@events_bp.route('/')
@login_required
@role_required('organizer')
def list_events():
    docs = (
        db.collection('events')
        .where('organizer_uid','==',session['uid'])
        .order_by('created_at')
        .stream()
    )
    events = [{**d.to_dict(),'id':d.id} for d in docs]
    return render_template('organizer/events/list.html',events=events)
 
 
# ── CREATE event ──────────────────────────────────────────────────────
@events_bp.route('/create',methods=['GET','POST'])
@login_required
@role_required('organizer')
def create_event():
    venues = get_my_venues()
 
    if request.method == 'POST':
        form_data = request.form.to_dict()
        action    = form_data.pop('action','draft')  # 'draft' or 'publish'
        errors    = validate_event(form_data)
 
        if errors:
            for e in errors: flash(e,'danger')
            return render_template('organizer/events/form.html',
                               form_data=form_data,venues=venues,action='create')
 
        status = 'published' if action == 'publish' else 'draft'
        start_dt = datetime.strptime(form_data['start_datetime'],FMT)
        end_dt   = datetime.strptime(form_data['end_datetime'],FMT)
 
        _,ref = db.collection('events').add({
            'name':           form_data['name'].strip(),
            'description':    form_data['description'].strip(),
            'start_datetime': start_dt,
            'end_datetime':   end_dt,
            'venue_id':       form_data['venue_id'],
            'event_type':     form_data.get('event_type','physical'),
            'status':         status,
            'organizer_uid':  session['uid'],
            'total_registrations': 0,
            'total_revenue':        0,
            'total_checkins':       0,
            'created_at':     SERVER_TIMESTAMP,
            'published_at':   SERVER_TIMESTAMP if status=='published' else None,
        })
 
        flash(f"Event '{form_data['name']}' {status}!",'success')
        return redirect(url_for('events.list_events'))
 
    return render_template('organizer/events/form.html',
                           form_data={},venues=venues,action='create')
 
 
# ── EDIT event ────────────────────────────────────────────────────────
@events_bp.route('/<event_id>/edit',methods=['GET','POST'])
@login_required
@role_required('organizer')
def edit_event(event_id):
    doc,data = get_event_or_403(event_id)
    if doc is None:
        flash('Event not found or access denied.','danger')
        return redirect(url_for('events.list_events'))
 
    venues = get_my_venues()
 
    # Convert timestamps to string for form pre-fill
    if data.get('start_datetime'):
        data['start_datetime'] = data['start_datetime'].strftime(FMT)
    if data.get('end_datetime'):
        data['end_datetime'] = data['end_datetime'].strftime(FMT)
 
    if request.method == 'POST':
        form_data = request.form.to_dict()
        form_data.pop('action',None)
        errors = validate_event(form_data)
 
        if errors:
            for e in errors: flash(e,'danger')
            return render_template('organizer/events/form.html',
                               form_data=form_data,venues=venues,
                               action='edit',event_id=event_id)
 
        start_dt = datetime.strptime(form_data['start_datetime'],FMT)
        end_dt   = datetime.strptime(form_data['end_datetime'],FMT)
 
        db.collection('events').document(event_id).update({
            'name':           form_data['name'].strip(),
            'description':    form_data['description'].strip(),
            'start_datetime': start_dt,
            'end_datetime':   end_dt,
            'venue_id':       form_data['venue_id'],
            'event_type':     form_data.get('event_type','physical'),
            'updated_at':     SERVER_TIMESTAMP,
        })
        flash(f"Event '{form_data['name']}' updated!",'success')
        return redirect(url_for('events.list_events'))
 
    return render_template('organizer/events/form.html',
                           form_data=data,venues=venues,
                           action='edit',event_id=event_id)
 
 
# ── PUBLISH event ─────────────────────────────────────────────────────
@events_bp.route('/<event_id>/publish',methods=['POST'])
@login_required
@role_required('organizer')
def publish_event(event_id):
    doc,data = get_event_or_403(event_id)
    if doc is None:
        flash('Event not found.','danger')
        return redirect(url_for('events.list_events'))
 
    db.collection('events').document(event_id).update({
        'status':       'published',
        'published_at': SERVER_TIMESTAMP
    })
    flash(f"Event '{data['name']}' is now published!",'success')
    return redirect(url_for('events.list_events'))
 
 
# ── DELETE event (soft delete — sets status to cancelled) ─────────────
@events_bp.route('/<event_id>/delete',methods=['POST'])
@login_required
@role_required('organizer')
def delete_event(event_id):
    doc,data = get_event_or_403(event_id)
    if doc is None:
        flash('Event not found.','danger')
        return redirect(url_for('events.list_events'))
 
    # Soft delete: mark as cancelled (preserves registration records)
    db.collection('events').document(event_id).update({
        'status': 'cancelled'
    })
    flash(f"Event '{data['name']}' has been cancelled.",'info')
    return redirect(url_for('events.list_events'))
# ── ATTENDEE LIST ───────────────────────────────────────────────────────
@events_bp.route('/<event_id>/attendees')
@login_required
@role_required('organizer')
def event_attendees(event_id):
    """Displays a table of all registrations for a specific event."""
    doc, data = get_event_or_403(event_id)
    if doc is None:
        flash('Event not found or access denied.', 'danger')
        return redirect(url_for('events.list_events'))

    # Fetch all registrations for this event
    regs = db.collection('registrations').where('event_id', '==', event_id).stream()
    attendees = [{**r.to_dict(), 'id': r.id} for r in regs]
    
    # Sort attendees by name in Python (avoids needing a complex Firestore index)
    attendees.sort(key=lambda x: x.get('attendee_name', '').lower())

    # FIXED: Added 'return' and changed 'event=event' to 'event=data'
    return render_template('organizer/events/attendees.html', event=data, attendees=attendees, event_id=event_id)

# ── EXPORT CSV ──────────────────────────────────────────────────────────
@events_bp.route('/<event_id>/export')
@login_required
@role_required('organizer')
def export_attendees(event_id):
    """Generates and downloads a CSV file of all attendees."""
    doc, data = get_event_or_403(event_id)
    if doc is None:
        return "Unauthorized", 403

    regs = db.collection('registrations').where('event_id', '==', event_id).stream()
    
    # Create an in-memory string buffer to build the CSV
    si = StringIO()
    # Add UTF-8 BOM so Microsoft Excel reads the Rupee symbol correctly
    si.write('\ufeff')
    cw = csv.writer(si)
    
    # Write the CSV Header row
    cw.writerow(['Order ID', 'Attendee Name', 'Email', 'Ticket Type', 'Quantity', 'Amount Paid', 'Status', 'Date'])
    
    # Write the data rows
    for r in regs:
        reg = r.to_dict()
        
        # Format the timestamp if it exists
        created_at = reg.get('created_at')
        date_str = created_at.strftime('%Y-%m-%d %H:%M') if created_at else 'N/A'
        
        cw.writerow([
            r.id,
            reg.get('attendee_name', ''),
            reg.get('attendee_email', ''),
            reg.get('ticket_type_name', ''),
            reg.get('quantity', 1),
            f"₹{reg.get('total_amount', 0)}",
            reg.get('status', '').upper(),
            date_str
        ])
        
    output = si.getvalue()
    
    # Return the CSV as a downloadable file
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=attendees_{event_id}.csv"}
    )

@events_bp.route('/<event_id>/waitlist')
@login_required
@role_required('organizer')
def view_waitlist(event_id):
    """View the waitlist for a specific event."""
    # 1. Verify ownership
    event_doc = db.collection('events').document(event_id).get()
    if not event_doc.exists or event_doc.to_dict().get('organizer_uid') != session.get('uid'):
        flash('Event not found or access denied.', 'danger')
        return redirect(url_for('events.list_events'))

    event_data = {**event_doc.to_dict(), 'id': event_doc.id}

    # 2. Fetch ticket types to map IDs to readable names
    tt_docs = db.collection('events').document(event_id).collection('ticket_types').stream()
    ticket_map = {tt.id: tt.to_dict().get('name', 'Unknown') for tt in tt_docs}

    # 3. Fetch the waitlist ordered by who joined first
    waitlist_docs = db.collection('events').document(event_id).collection('waitlist').order_by('joined_at').stream()
    waitlist = []
    for doc in waitlist_docs:
        w_data = doc.to_dict()
        w_data['id'] = doc.id
        w_data['ticket_name'] = ticket_map.get(w_data.get('ticket_type_id'), 'Unknown Ticket')
        waitlist.append(w_data)

    return render_template('organizer/events/waitlist.html', event=event_data, waitlist=waitlist)

@events_bp.route('/<event_id>/scanner')
@login_required
@role_required('organizer')
def event_scanner(event_id):
    """Render the QR code scanner page for an event."""
    event_doc = db.collection('events').document(event_id).get()
    
    if not event_doc.exists or event_doc.to_dict().get('organizer_uid') != session.get('uid'):
        flash('Event not found or access denied.', 'danger')
        return redirect(url_for('events.list_events'))
        
    event_data = {**event_doc.to_dict(), 'id': event_doc.id}
    
    return render_template('organizer/events/scanner.html', event=event_data)

@events_bp.route('/<event_id>/attendees/<reg_id>/toggle-checkin', methods=['POST'])
@login_required
@role_required('organizer')
def toggle_checkin(event_id, reg_id):
    """Manually toggle an attendee's check-in status."""
    reg_ref = db.collection('registrations').document(reg_id)
    reg_doc = reg_ref.get()
    
    if not reg_doc.exists or reg_doc.to_dict().get('event_id') != event_id:
        flash('Registration not found.', 'danger')
        return redirect(url_for('events.event_attendees', event_id=event_id))

    current_status = reg_doc.to_dict().get('status')
    
    # Determine new status and math
    if current_status == 'checked_in':
        new_status = 'confirmed'
        increment_val = -1
    else:
        new_status = 'checked_in'
        increment_val = 1

    # 1. Update the registration document
    reg_ref.update({'status': new_status})

    # 2. Update the event's total_checkins counter
    db.collection('events').document(event_id).update({
        'total_checkins': Increment(increment_val)
    })

    flash(f"Attendee successfully {'checked in' if new_status == 'checked_in' else 'un-checked in'}.", "success")
    return redirect(url_for('events.event_attendees', event_id=event_id))

@events_bp.route('/<event_id>/analytics')
@login_required
@role_required('organizer')
def event_analytics(event_id):
    """View visual analytics (charts) for an event."""
    event_doc = db.collection('events').document(event_id).get()
    if not event_doc.exists or event_doc.to_dict().get('organizer_uid') != session.get('uid'):
        flash('Event not found.', 'danger')
        return redirect(url_for('events.list_events'))

    # Fetch all confirmed or checked-in registrations
    regs = db.collection('registrations')\
             .where('event_id', '==', event_id)\
             .where('status', 'in', ['confirmed', 'checked_in']).stream()
    
    # Group ticket sales by ticket type name
    ticket_sales = {}
    for r in regs:
        data = r.to_dict()
        t_name = data.get('ticket_type_name', 'Unknown')
        qty = data.get('quantity', 1)
        ticket_sales[t_name] = ticket_sales.get(t_name, 0) + qty

    # Prepare data for Chart.js
    chart_labels = list(ticket_sales.keys())
    chart_data = list(ticket_sales.values())

    return render_template('organizer/events/analytics.html', 
                           event={**event_doc.to_dict(), 'id': event_id},
                           chart_labels=chart_labels, 
                           chart_data=chart_data)

@events_bp.route('/<event_id>/certificate/<reg_id>')
@login_required
def generate_certificate(event_id, reg_id):
    """Generate a PDF Attendance Certificate for a checked-in user."""
    # 1. Fetch Event and Registration data
    event_doc = db.collection('events').document(event_id).get()
    reg_doc = db.collection('registrations').document(reg_id).get()

    if not event_doc.exists or not reg_doc.exists:
        flash('Data not found.', 'danger')
        return redirect(url_for('events.list_events'))

    event_data = event_doc.to_dict()
    reg_data = reg_doc.to_dict()

    # 2. Security Check: Only allow if they actually checked in!
    if reg_data.get('status') != 'checked_in':
        flash('Certificates are only available for attendees who checked in.', 'warning')
        return redirect(url_for('events.list_events'))

    # 3. Security Check: Only the Attendee or the Organizer can download it
    is_organizer = session.get('uid') == event_data.get('organizer_uid')
    is_attendee = session.get('uid') == reg_data.get('user_uid')
    if not is_organizer and not is_attendee:
        flash('Unauthorized.', 'danger')
        return redirect(url_for('events.list_events'))

    # 4. Generate the PDF
    buffer = BytesIO()
    # Create a landscape (horizontal) PDF
    p = canvas.Canvas(buffer, pagesize=landscape(letter))
    width, height = landscape(letter)

    # Draw a fancy border
    p.setStrokeColor(colors.HexColor('#0d6efd')) # Bootstrap primary blue
    p.setLineWidth(10)
    p.rect(20, 20, width-40, height-40)

    # Title
    p.setFont("Helvetica-Bold", 40)
    p.setFillColor(colors.HexColor('#333333'))
    p.drawCentredString(width/2, height - 120, "CERTIFICATE OF ATTENDANCE")

    # Subtitle
    p.setFont("Helvetica", 20)
    p.setFillColor(colors.gray)
    p.drawCentredString(width/2, height - 180, "This is to certify that")

    # Attendee Name
    p.setFont("Helvetica-Bold", 35)
    p.setFillColor(colors.black)
    p.drawCentredString(width/2, height - 240, reg_data.get('attendee_name', 'Unknown Attendee').upper())

    # Event info
    p.setFont("Helvetica", 20)
    p.setFillColor(colors.gray)
    p.drawCentredString(width/2, height - 300, "has successfully attended the event")

    # Event Name
    p.setFont("Helvetica-Bold", 25)
    p.setFillColor(colors.HexColor('#0d6efd'))
    p.drawCentredString(width/2, height - 350, event_data.get('name', 'Unknown Event'))

    # Date
    p.setFont("Helvetica", 14)
    p.setFillColor(colors.gray)
    if reg_data.get('created_at'):
        date_str = reg_data['created_at'].strftime('%B %d, %Y')
    else:
        date_str = "2026"
    p.drawCentredString(width/2, 100, f"Date: {date_str}")

    # Finish saving the PDF
    p.showPage()
    p.save()

    # 5. Send PDF to browser
    pdf_out = buffer.getvalue()
    buffer.close()
    
    response = make_response(pdf_out)
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = f'attachment; filename=Certificate_{reg_data.get("attendee_name", "Attendee")}.pdf'
    
    return response



@events_bp.route('/<event_id>/feedback')
@login_required
@role_required('organizer')
def view_feedback(event_id):
    event_doc = db.collection('events').document(event_id).get()
    if not event_doc.exists or event_doc.to_dict().get('organizer_uid') != session.get('uid'):
        return redirect(url_for('events.list_events'))
        
    event_data = {**event_doc.to_dict(), 'id': event_id}
    
    # Fetch all feedback for this event
    feedback_docs = db.collection('events').document(event_id).collection('feedback').order_by('submitted_at', direction=Query.DESCENDING).stream()
    feedbacks = [{**d.to_dict(), 'id': d.id} for d in feedback_docs]
    
    # Calculate average rating
    avg_rating = round(sum([f['rating'] for f in feedbacks]) / len(feedbacks), 1) if feedbacks else 0
    
    return render_template('organizer/events/feedback.html', event=event_data, feedbacks=feedbacks, avg_rating=avg_rating)

@events_bp.route('/<event_id>/sponsor_dashboard')
@login_required
@role_required('organizer')
def sponsor_dashboard(event_id):
    doc, event_data = get_event_or_403(event_id)
    if doc is None:
        return redirect(url_for('events.list_events'))

    # Fetch all sponsors
    sponsors_docs = db.collection('events').document(event_id)\
                      .collection('sponsors').stream()
    sponsors = []
    total_amount = 0

    for s in sponsors_docs:
        sponsor = {**s.to_dict(), 'id': s.id}
        total_amount += sponsor.get('amount', 0)

        # Fetch deliverables for each sponsor
        deliverables_docs = db.collection('events').document(event_id)\
                              .collection('sponsors').document(s.id)\
                              .collection('deliverables').stream()
        deliverables = [d.to_dict() for d in deliverables_docs]

        total_d     = len(deliverables)
        completed_d = sum(1 for d in deliverables if d.get('status') == 'completed')
        progress    = int((completed_d / total_d) * 100) if total_d > 0 else 0

        sponsor['total_deliverables']     = total_d
        sponsor['completed_deliverables'] = completed_d
        sponsor['progress_pct']           = progress
        sponsors.append(sponsor)

    # Sort by amount descending
    sponsors.sort(key=lambda x: x.get('amount', 0), reverse=True)

    # Tier breakdown
    tier_counts = {}
    for s in sponsors:
        tier = s.get('tier', 'Custom')
        tier_counts[tier] = tier_counts.get(tier, 0) + 1

    return render_template(
        'organizer/sponsors/dashboard.html',
        event=event_data,
        event_id=event_id,
        sponsors=sponsors,
        total_amount=total_amount,
        tier_counts=tier_counts
    )

@events_bp.route('/<event_id>/sponsors/<sponsor_id>/export_leads')
@login_required
@role_required('organizer')
def export_leads(event_id, sponsor_id):
    doc, data = get_event_or_403(event_id)
    if doc is None:
        return "Unauthorized", 403

    sponsor_doc = db.collection('events').document(event_id)\
                    .collection('sponsors').document(sponsor_id).get()
    if not sponsor_doc.exists:
        return "Sponsor not found", 404

    sponsor_data = sponsor_doc.to_dict()

    leads_docs = db.collection('events').document(event_id)\
                   .collection('sponsors').document(sponsor_id)\
                   .collection('leads').stream()

    si = StringIO()
    si.write('\ufeff')  # UTF-8 BOM for Excel
    cw = csv.writer(si)
    cw.writerow(['Attendee Name', 'Email', 'Connected At'])

    for lead in leads_docs:
        lead_data = lead.to_dict()
        connected_at = lead_data.get('connected_at')
        date_str = connected_at.strftime('%Y-%m-%d %H:%M') if connected_at else 'N/A'
        cw.writerow([
            lead_data.get('attendee_name', ''),
            lead_data.get('attendee_email', ''),
            date_str
        ])

    return Response(
        si.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition':
                 f'attachment;filename=leads_{sponsor_data.get("company_name","sponsor")}.csv'}
    )


@events_bp.route('/<event_id>/sponsor_roi')
@login_required
@role_required('organizer')
def sponsor_roi(event_id):
    doc, event_data = get_event_or_403(event_id)
    if doc is None:
        return redirect(url_for('events.list_events'))

    sponsors_docs = db.collection('events').document(event_id)\
                      .collection('sponsors').stream()
    sponsors = []
    total_raised = 0

    for s in sponsors_docs:
        sponsor = {**s.to_dict(), 'id': s.id}
        total_raised += sponsor.get('amount', 0)

        # Deliverables
        deliverables_docs = db.collection('events').document(event_id)\
                              .collection('sponsors').document(s.id)\
                              .collection('deliverables').stream()
        deliverables  = [d.to_dict() for d in deliverables_docs]
        total_d       = len(deliverables)
        completed_d   = sum(1 for d in deliverables if d.get('status') == 'completed')
        progress      = int((completed_d / total_d) * 100) if total_d > 0 else 0

        # Leads
        leads_docs = db.collection('events').document(event_id)\
                       .collection('sponsors').document(s.id)\
                       .collection('leads').stream()
        leads_count = len(list(leads_docs))

        sponsor['total_d']     = total_d
        sponsor['completed_d'] = completed_d
        sponsor['progress']    = progress
        sponsor['leads_count'] = leads_count
        sponsors.append(sponsor)

    sponsors.sort(key=lambda x: x.get('amount', 0), reverse=True)

    return render_template(
        'organizer/sponsors/roi.html',
        event=event_data,
        event_id=event_id,
        sponsors=sponsors,
        total_raised=total_raised
    )