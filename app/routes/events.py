from flask import (Blueprint, render_template, redirect,
                        url_for, session, flash, request)
from app.firebase_config import db
from app.decorators import login_required, role_required
from app.utils.validators import validate_event
from datetime import datetime, timezone
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
import requests
import secrets
import cloudinary.uploader
from reportlab.lib.utils import ImageReader
import textwrap
from flask import abort
from app.utils.email_utils import send_ticket_email, send_email_with_attachment
from app.utils.notification_utils import create_notification
from app.utils.razorpay_utils import create_refund
from app.utils.event_utils import is_event_over
 
events_bp = Blueprint('events', __name__, url_prefix='/organizer/events')
 
FMT = '%Y-%m-%dT%H:%M'  # datetime-local HTML input format

DEFAULT_REFUND_POLICY = {
    'full_refund_days': 7,
    'partial_refund_days': 3,
    'partial_refund_percent': 50,
}

DEFAULT_CERT_BODY = ("This is to certify that {{attendee_name}} has successfully "
                      "attended {{event_name}} held on {{event_date}}.")

CRITICAL_FIELDS = ['start_datetime', 'end_datetime', 'venue_id']


def notify_event_change(event_id, event_name, changes):
    """changes: dict of field -> (old_value, new_value). Emails + notifies every
    confirmed/checked-in attendee, and writes an audit log entry per field."""
    regs = (db.collection('registrations')
            .where('event_id', '==', event_id)
            .where('status', 'in', ['confirmed', 'checked_in']).stream())

    change_lines = ''.join(
        f"<li><strong>{f.replace('_', ' ').title()}:</strong> {o} &rarr; {n}</li>"
        for f, (o, n) in changes.items()
    )

    for r in regs:
        reg = r.to_dict()
        create_notification(
            reg['attendee_uid'], f"{event_name} has been updated",
            f"Details changed: {', '.join(changes.keys())}. Please check the new event page.",
            event_id=event_id, notif_type='event_update'
        )
        try:
            send_ticket_email(
                to_email=reg.get('attendee_email'),
                subject=f"Important update: {event_name}",
                html_content=f"""
                    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
                        <h2 style="color:#dc3545;">Event Details Have Changed</h2>
                        <p>Hi {reg.get('attendee_name', 'Attendee')},</p>
                        <p>The organizer of <strong>{event_name}</strong> has made the following
                           change(s) to an event you're registered for:</p>
                        <ul>{change_lines}</ul>
                        <p>Please review your registration and make sure the new details still work for you.</p>
                    </div>
                """
            )
        except Exception as e:
            print(f"[ERROR] Change notification email failed for {reg.get('attendee_email')}: {e}")

    for field, (old_val, new_val) in changes.items():
        db.collection('events').document(event_id).collection('change_log').add({
            'field':      field,
            'old_value':  str(old_val),
            'new_value':  str(new_val),
            'changed_at': SERVER_TIMESTAMP,
            'changed_by': session.get('uid'),
        })


def notify_event_cancelled_and_refund(event_id, event_name):
    """Cancels every confirmed/checked-in registration, auto-refunds paid ones in full,
    and emails everyone affected."""
    regs = (db.collection('registrations')
            .where('event_id', '==', event_id)
            .where('status', 'in', ['confirmed', 'checked_in']).stream())

    for r in regs:
        reg = r.to_dict()
        reg_id = r.id
        refund_amount = reg.get('total_amount', 0)
        refund_ok = True

        if refund_amount > 0 and reg.get('razorpay_payment_id'):
            refund_result = create_refund(reg['razorpay_payment_id'])  # no amount = full refund
            refund_ok = refund_result.get('success', False)
            if not refund_ok:
                print(f"[ERROR] Auto-refund failed for reg {reg_id}: {refund_result.get('error')}")

        db.collection('registrations').document(reg_id).update({
            'status':        'cancelled',
            'cancelled_at':  SERVER_TIMESTAMP,
            'refund_amount': refund_amount if refund_ok else 0,
            'refund_status': 'processing' if (refund_amount > 0 and refund_ok) else 'not_applicable',
        })

        create_notification(
            reg['attendee_uid'], f"{event_name} has been cancelled",
            "The organizer has cancelled this event. Any payment will be fully refunded.",
            event_id=event_id, notif_type='event_cancelled'
        )
        try:
            send_ticket_email(
                to_email=reg.get('attendee_email'),
                subject=f"{event_name} has been cancelled",
                html_content=f"""
                    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
                        <h2 style="color:#dc3545;">Event Cancelled</h2>
                        <p>Hi {reg.get('attendee_name', 'Attendee')},</p>
                        <p><strong>{event_name}</strong> has been cancelled by the organizer.</p>
                        {"<p>A full refund of ₹" + str(refund_amount) + " is being processed to your original payment method.</p>" if refund_amount > 0 else ""}
                        <p>We're sorry for the inconvenience.</p>
                    </div>
                """
            )
        except Exception as e:
            print(f"[ERROR] Cancellation email failed: {e}")

def notify_new_event_published(organizer_uid, event_id, event_name):
    """In-app notification to anyone who has previously attended (confirmed/
    checked-in) an event by this same organizer, when they publish a new one."""
    organizer_event_docs = (
        db.collection('events')
        .where('organizer_uid', '==', organizer_uid)
        .stream()
    )
    past_event_ids = [d.id for d in organizer_event_docs if d.id != event_id]
    if not past_event_ids:
        return

    notified_uids = set()
    # Firestore 'in' queries cap at 30 values — chunk if this organizer has more events
    for i in range(0, len(past_event_ids), 30):
        chunk = past_event_ids[i:i + 30]
        reg_docs = (
            db.collection('registrations')
            .where('event_id', 'in', chunk)
            .where('status', 'in', ['confirmed', 'checked_in'])
            .stream()
        )
        for r in reg_docs:
            uid = r.to_dict().get('attendee_uid')
            if uid and uid not in notified_uids:
                notified_uids.add(uid)
                create_notification(
                    uid, 'New event from an organizer you know!',
                    f"{event_name} was just published — check it out.",
                    event_id=event_id, notif_type='new_event'
                )
 
 
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
            'refund_policy': {
                'full_refund_days':      int(form_data.get('full_refund_days', 7)),
                'partial_refund_days':   int(form_data.get('partial_refund_days', 3)),
                'partial_refund_percent': int(form_data.get('partial_refund_percent', 50)),
            },
            'created_at':     SERVER_TIMESTAMP,
            'published_at':   SERVER_TIMESTAMP if status=='published' else None,
        })
 
        if status == 'published':
            notify_new_event_published(session['uid'], ref.id, form_data['name'].strip())

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
 
        # Detect critical-field changes BEFORE writing the update (data['start_datetime']
        # / data['end_datetime'] were already converted to FMT strings above for form
        # pre-fill, so they're directly comparable to form_data's string values here).
        changes = {}
        if data.get('start_datetime') != form_data['start_datetime']:
            changes['start_datetime'] = (data.get('start_datetime'), form_data['start_datetime'])
        if data.get('end_datetime') != form_data['end_datetime']:
            changes['end_datetime'] = (data.get('end_datetime'), form_data['end_datetime'])
        if data.get('venue_id') != form_data['venue_id']:
            changes['venue_id'] = (data.get('venue_id'), form_data['venue_id'])

        db.collection('events').document(event_id).update({
            'name':           form_data['name'].strip(),
            'description':    form_data['description'].strip(),
            'start_datetime': start_dt,
            'end_datetime':   end_dt,
            'venue_id':       form_data['venue_id'],
            'event_type':     form_data.get('event_type','physical'),
            'updated_at':     SERVER_TIMESTAMP,
        })

        if changes and data.get('total_registrations', 0) > 0:
            notify_event_change(event_id, form_data['name'].strip(), changes)
            flash(f"Event updated — {len(changes)} registered attendee(s) notified of the change.", 'info')

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

    notify_new_event_published(session['uid'], event_id, data['name'])

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

    notify_event_cancelled_and_refund(event_id, data['name'])

    flash(f"Event '{data['name']}' has been cancelled. Registered attendees have been notified and refunded.", 'info')
    return redirect(url_for('events.list_events'))

@events_bp.route('/<event_id>/mark-completed', methods=['POST'])
@login_required
@role_required('organizer')
def mark_event_completed(event_id):
    """Lets an organizer explicitly close out an event before its scheduled
    end_datetime has passed (e.g. ending a multi-day event early)."""
    doc, data = get_event_or_403(event_id)
    if doc is None:
        flash('Event not found or access denied.', 'danger')
        return redirect(url_for('events.list_events'))

    if data.get('status') == 'cancelled':
        flash('A cancelled event cannot be marked as completed.', 'warning')
        return redirect(url_for('events.event_attendees', event_id=event_id))

    db.collection('events').document(event_id).update({
        'status':        'completed',
        'completed_at':  SERVER_TIMESTAMP,
    })

    flash(f"'{data['name']}' has been marked as completed. Certificates can now be generated.", 'success')
    return redirect(url_for('events.event_attendees', event_id=event_id))
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

    return render_template('organizer/events/attendees.html',
                            event=data, attendees=attendees, event_id=event_id,
                            event_is_over=is_event_over(data))

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
def _build_certificate_pdf(attendee_name, event_name, date_str, template):
    """Pure PDF-drawing logic — takes plain values so it can be reused for both
    real certificates and the organizer's sample preview."""
    body_text = template.get('body_text') or DEFAULT_CERT_BODY
    body_text = (body_text
                 .replace('{{attendee_name}}', attendee_name)
                 .replace('{{event_name}}', event_name)
                 .replace('{{event_date}}', date_str))

    signatures = template.get('signatures', [])

    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=landscape(letter))
    width, height = landscape(letter)

    p.setStrokeColor(colors.HexColor('#0d6efd'))
    p.setLineWidth(10)
    p.rect(20, 20, width - 40, height - 40)

    if template.get('logo_url'):
        try:
            img_data = requests.get(template['logo_url'], timeout=5).content
            logo_img = ImageReader(BytesIO(img_data))
            p.drawImage(logo_img, width / 2 - 40, height - 95, width=80, height=80,
                        mask='auto', preserveAspectRatio=True)
        except Exception as e:
            print(f"[WARN] Could not load certificate logo: {e}")

    p.setFont("Helvetica-Bold", 36)
    p.setFillColor(colors.HexColor('#333333'))
    p.drawCentredString(width / 2, height - 150, "CERTIFICATE OF ATTENDANCE")

    p.setFont("Helvetica-Bold", 30)
    p.setFillColor(colors.black)
    p.drawCentredString(width / 2, height - 210, attendee_name.upper())

    p.setFont("Helvetica", 15)
    p.setFillColor(colors.HexColor('#444444'))
    wrapped_lines = textwrap.wrap(body_text, width=85)
    text_y = height - 260
    for line in wrapped_lines:
        p.drawCentredString(width / 2, text_y, line)
        text_y -= 22

    p.setFont("Helvetica", 12)
    p.setFillColor(colors.gray)
    p.drawString(80, 70, f"Date: {date_str}")

    if signatures:
        n = len(signatures)
        block_width = 160
        total_width = n * block_width
        start_x = (width - total_width) / 2
        sig_y = 55

        for i, sig in enumerate(signatures):
            cx = start_x + i * block_width + block_width / 2

            if sig.get('signature_url'):
                try:
                    img_data = requests.get(sig['signature_url'], timeout=5).content
                    sig_img = ImageReader(BytesIO(img_data))
                    p.drawImage(sig_img, cx - 60, sig_y + 25, width=120, height=40,
                                mask='auto', preserveAspectRatio=True)
                except Exception as e:
                    print(f"[WARN] Could not load signature image {i}: {e}")

            p.setLineWidth(1)
            p.setStrokeColor(colors.gray)
            p.line(cx - 60, sig_y + 22, cx + 60, sig_y + 22)

            if sig.get('signer_name'):
                p.setFont("Helvetica-Bold", 10)
                p.setFillColor(colors.black)
                p.drawCentredString(cx, sig_y + 8, sig['signer_name'])
            if sig.get('signer_title'):
                p.setFont("Helvetica", 8)
                p.setFillColor(colors.gray)
                p.drawCentredString(cx, sig_y - 4, sig['signer_title'])

    p.showPage()
    p.save()
    pdf_out = buffer.getvalue()
    buffer.close()
    return pdf_out


def _generate_certificate_pdf(event_id, reg_id):
    """Fetches event/registration/template data and builds the certificate PDF.
    Returns (pdf_bytes, reg_data, event_data, error)."""
    event_doc = db.collection('events').document(event_id).get()
    reg_doc = db.collection('registrations').document(reg_id).get()

    if not event_doc.exists or not reg_doc.exists:
        return None, None, None, 'Data not found.'

    event_data = event_doc.to_dict()
    reg_data = reg_doc.to_dict()

    if reg_data.get('status') != 'checked_in':
        return None, None, None, 'Certificates are only available for attendees who checked in.'

    template_doc = (db.collection('events').document(event_id)
                     .collection('certificate_template').document('settings').get())
    template = template_doc.to_dict() if template_doc.exists else {}

    date_str = reg_data['created_at'].strftime('%B %d, %Y') if reg_data.get('created_at') else "2026"
    attendee_name = reg_data.get('attendee_name', 'Unknown Attendee')
    event_name = event_data.get('name', 'Unknown Event')

    pdf_out = _build_certificate_pdf(attendee_name, event_name, date_str, template)
    return pdf_out, reg_data, event_data, None


@events_bp.route('/<event_id>/certificate/settings/preview')


@events_bp.route('/<event_id>/certificate/settings/preview')
@login_required
@role_required('organizer')
def certificate_settings_preview(event_id):
    """Lets the organizer preview a SAMPLE certificate with their current template
    settings, before running the real bulk generation."""
    doc, data = get_event_or_403(event_id)
    if doc is None:
        abort(403)

    template_doc = (db.collection('events').document(event_id)
                     .collection('certificate_template').document('settings').get())
    template = template_doc.to_dict() if template_doc.exists else {}

    pdf_bytes = _build_certificate_pdf(
        attendee_name='Jane Doe (Sample)',
        event_name=data.get('name', 'Sample Event'),
        date_str=datetime.now().strftime('%B %d, %Y'),
        template=template,
    )

    response = make_response(pdf_bytes)
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = 'inline; filename=certificate_sample.pdf'
    return response


@events_bp.route('/<event_id>/certificates/generate-all', methods=['POST'])
@login_required
@role_required('organizer')
def generate_certificates_bulk(event_id):
    """Generates + stores a certificate for every checked-in attendee of this
    event, and notifies each one in-app that it's ready to download."""
    doc, data = get_event_or_403(event_id)
    if doc is None:
        flash('Event not found or access denied.', 'danger')
        return redirect(url_for('events.list_events'))

    if not is_event_over(data):
        flash('Certificates can only be generated after the event has ended.', 'warning')
        return redirect(url_for('events.event_attendees', event_id=event_id))

    regs = (db.collection('registrations')
            .where('event_id', '==', event_id)
            .where('status', '==', 'checked_in').stream())

    generated = 0
    failed = 0

    for r in regs:
        reg_id = r.id
        pdf_bytes, reg_data, event_data, error = _generate_certificate_pdf(event_id, reg_id)
        if error:
            failed += 1
            continue

        try:
            upload_result = cloudinary.uploader.upload(
                BytesIO(pdf_bytes),
                folder='ems_certificates_generated',
                public_id=f'cert_{event_id}_{reg_id}',
                resource_type='raw',
                format='pdf',
                overwrite=True
            )
            certificate_url = upload_result.get('secure_url')
        except Exception as e:
            print(f"[ERROR] Certificate upload failed for {reg_id}: {e}")
            failed += 1
            continue

        db.collection('registrations').document(reg_id).update({
            'certificate_url':         certificate_url,
            'certificate_generated_at': SERVER_TIMESTAMP,
        })

        create_notification(
            reg_data['attendee_uid'], 'Your certificate is ready!',
            f"Your certificate for {event_data.get('name', 'the event')} is ready to download from My Certificates.",
            event_id=event_id, notif_type='certificate_ready'
        )
        generated += 1

    msg = f'Certificates generated for {generated} attendee(s).'
    if failed:
        msg += f' {failed} could not be generated.'
    flash(msg, 'success' if generated else 'warning')
    return redirect(url_for('events.event_attendees', event_id=event_id))

@events_bp.route('/<event_id>/certificate/settings', methods=['GET', 'POST'])
@login_required
@role_required('organizer')
def certificate_settings(event_id):
    doc, data = get_event_or_403(event_id)
    if doc is None:
        flash('Event not found or access denied.', 'danger')
        return redirect(url_for('events.list_events'))

    settings_ref = (db.collection('events').document(event_id)
                     .collection('certificate_template').document('settings'))
    settings_doc = settings_ref.get()
    settings = settings_doc.to_dict() if settings_doc.exists else {}

    if request.method == 'POST':
        update_data = {
            'body_text':  request.form.get('body_text', '').strip() or DEFAULT_CERT_BODY,
            'updated_at': SERVER_TIMESTAMP,
        }

        logo_file = request.files.get('logo_file')
        if logo_file and logo_file.filename:
            upload = cloudinary.uploader.upload(
                logo_file, folder='ems_certificates',
                public_id=f'logo_{event_id}', overwrite=True
            )
            update_data['logo_url'] = upload.get('secure_url')
        elif settings.get('logo_url'):
            update_data['logo_url'] = settings['logo_url']  # keep old logo if no new one uploaded

        # ── Rebuild the signatures array from however many blocks were submitted ──
        signatures = []
        index = 0
        while f'signer_name_{index}' in request.form:
            signer_name  = request.form.get(f'signer_name_{index}', '').strip()
            signer_title = request.form.get(f'signer_title_{index}', '').strip()
            existing_url = request.form.get(f'existing_signature_url_{index}', '')
            sig_file     = request.files.get(f'signature_file_{index}')

            signature_url = existing_url
            if sig_file and sig_file.filename:
                upload = cloudinary.uploader.upload(
                    sig_file, folder='ems_certificates',
                    public_id=f'signature_{event_id}_{index}_{secrets.token_hex(3)}',
                    overwrite=True
                )
                signature_url = upload.get('secure_url')

            if signer_name or signature_url:
                signatures.append({
                    'signer_name':   signer_name,
                    'signer_title':  signer_title,
                    'signature_url': signature_url,
                })
            index += 1

        update_data['signatures'] = signatures

        # Full overwrite (not merge) so a removed signature block actually disappears
        settings_ref.set(update_data, merge=False)
        flash('Certificate template updated!', 'success')
        return redirect(url_for('events.certificate_settings', event_id=event_id))

    return render_template('organizer/events/certificate_settings.html',
                            event=data, event_id=event_id, settings=settings,
                            default_body=DEFAULT_CERT_BODY)


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