import os, secrets
from flask import (Blueprint, render_template, redirect,
                        url_for, session, flash, request, jsonify, abort)
from app.firebase_config import db
from app.decorators import login_required
from app.utils.qr_utils import generate_and_upload_qr
from datetime import datetime, timezone
from google.cloud.firestore import SERVER_TIMESTAMP, Increment
from app.utils.event_utils import is_event_over
from dotenv import load_dotenv
from app.utils.razorpay_utils import create_refund
from app.utils.notification_utils import create_notification

load_dotenv()

registration_bp = Blueprint('registration', __name__, url_prefix='/attendee/registration')


# ── Helper: fetch event + ticket type + verify availability ──────────
def get_event_and_ticket(event_id, ticket_type_id):
    """Returns (event_data, ticket_data) or aborts with 404."""
    event_doc = db.collection('events').document(event_id).get()
    if not event_doc.exists or event_doc.to_dict().get('status') != 'published':
        abort(404)

    tt_doc = (
        db.collection('events')
        .document(event_id)
        .collection('ticket_types')
        .document(ticket_type_id)
        .get()
    )
    if not tt_doc.exists:
        abort(404)

    event  = {**event_doc.to_dict(), 'id': event_doc.id}
    ticket = {**tt_doc.to_dict(),  'id': tt_doc.id}
    return event, ticket


# ── Helper: fetch ALL active ticket types for an event (used by cart) ─
def get_event_and_all_tickets(event_id):
    event_doc = db.collection('events').document(event_id).get()
    if not event_doc.exists or event_doc.to_dict().get('status') != 'published':
        abort(404)

    event = {**event_doc.to_dict(), 'id': event_doc.id}
    tt_docs = db.collection('events').document(event_id).collection('ticket_types').stream()
    tickets = {}
    for d in tt_docs:
        t = {**d.to_dict(), 'id': d.id}
        tickets[d.id] = t
    return event, tickets


# ── Helper: apply promo code discount ────────────────────────────────
def apply_promo(event_id, code, subtotal):
    """Returns (discount_amount, promo_doc_id, error_msg)."""
    doc_ref = db.collection('events').document(event_id).collection('promo_codes').document(code.upper())
    doc = doc_ref.get()

    if not doc.exists:
        return 0, None, 'Invalid promo code.'

    promo = doc.to_dict()

    if not promo.get('active', True):
        return 0, None, 'This promo code is no longer active.'

    used = promo.get('current_uses', 0)
    limit = promo.get('max_uses', 0)
    if limit > 0 and used >= limit:
        return 0, None, 'Promo code usage limit reached.'

    dval = promo.get('discount_percentage', 0)
    discount = round(subtotal * dval / 100, 2)

    return discount, doc.id, None


# ── CORE: build a pending order (1 or many line items) ────────────────
def _create_pending_order(event, tickets_by_id, cart_items, promo_code, attendee_name):
    """
    cart_items: list of {'ticket_type_id': str, 'quantity': int}
    tickets_by_id: dict of ticket_type_id -> ticket_type data (already fetched)

    Returns (order_id, error_message). On success error_message is None.
    """
    event_id = event['id']
    line_items = []
    subtotal = 0.0

    # ── Validate every line item ──
    for item in cart_items:
        tt_id = item['ticket_type_id']
        qty = item['quantity']

        ticket = tickets_by_id.get(tt_id)
        if not ticket:
            return None, 'One of the selected ticket types is no longer available.'

        if not ticket.get('is_active', True):
            return None, f"{ticket.get('name','This ticket type')} is not currently on sale."

        available = ticket.get('quantity_total', 0) - ticket.get('quantity_sold', 0)
        max_order = ticket.get('max_per_order', 10)

        if qty < 1:
            continue  # user left this ticket type at 0 — skip it, not an error
        if qty > min(max_order, available):
            return None, (f"Only {min(max_order, available)} '{ticket['name']}' "
                           f"ticket(s) can be added to your order.")

        line_subtotal = round(ticket['price'] * qty, 2)
        subtotal += line_subtotal
        line_items.append({
            'ticket_type_id':   tt_id,
            'ticket_type_name': ticket['name'],
            'quantity':         qty,
            'unit_price':       ticket['price'],
            'line_subtotal':    line_subtotal,
        })

    if not line_items:
        return None, 'Please select at least one ticket.'

    subtotal = round(subtotal, 2)

    # ── Promo code (applies once, to the combined cart subtotal) ──
    discount = 0
    promo_id = None
    if promo_code:
        discount, promo_id, err = apply_promo(event_id, promo_code, subtotal)
        if err:
            # Don't hard-fail the whole order over a bad promo — just drop it and warn.
            flash(err, 'warning')
            discount = 0
            promo_id = None
            promo_code = None

    total_amount = max(0, round(subtotal - discount, 2))
    order_number = f'ORD-{secrets.token_hex(4).upper()}'

    # ── Create the order document ──
    _, order_ref = db.collection('orders').add({
        'event_id':        event_id,
        'attendee_uid':    session['uid'],
        'attendee_name':   attendee_name,
        'attendee_email':  session['email'],
        'items':           line_items,
        'subtotal':        subtotal,
        'discount_amount': discount,
        'promo_code_used': promo_code,
        'promo_id':        promo_id,
        'total_amount':    total_amount,
        'status':          'pending',
        'payment_status':  'pending',
        'order_number':    order_number,
        'created_at':      SERVER_TIMESTAMP,
    })
    order_id = order_ref.id

    # ── Create one registration doc per line item (keeps existing schema) ──
    for line in line_items:
        # Allocate the discount proportionally so each line's total_amount is meaningful
        # on its own (used by check-in / attendee list / CSV export elsewhere in the app).
        line_share = (line['line_subtotal'] / subtotal) if subtotal > 0 else 0
        line_discount = round(discount * line_share, 2)
        line_total = max(0, round(line['line_subtotal'] - line_discount, 2))

        db.collection('registrations').add({
            'event_id':         event_id,
            'order_id':         order_id,
            'attendee_uid':     session['uid'],
            'attendee_name':    attendee_name,
            'attendee_email':   session['email'],
            'ticket_type_id':   line['ticket_type_id'],
            'ticket_type_name': line['ticket_type_name'],
            'quantity':         line['quantity'],
            'unit_price':       line['unit_price'],
            'subtotal':         line['line_subtotal'],
            'discount_amount':  line_discount,
            'total_amount':     line_total,
            'promo_code_used':  promo_code,
            'status':           'pending',
            'payment_status':   'pending',
            'order_number':     order_number,
            'created_at':       SERVER_TIMESTAMP,
        })

    # ── Free order (₹0 total): confirm immediately, no Razorpay needed ──
    if total_amount == 0:
        _confirm_free_order(order_id, event_id, promo_id)

    return order_id, None


def _confirm_free_order(order_id, event_id, promo_id):
    """Confirms every registration under a ₹0 order and generates QR codes."""
    reg_docs = db.collection('registrations').where('order_id', '==', order_id).stream()
    reg_count = 0
    for reg_doc in reg_docs:
        reg_id = reg_doc.id
        reg = reg_doc.to_dict()

        # NOTE: previously free tickets got no QR code at all, which meant they
        # could never be scanned at check-in. Fixed here so free tickets behave
        # exactly like paid ones from check-in's point of view.
        qr_url, qr_payload = generate_and_upload_qr(reg_id, event_id)

        db.collection('registrations').document(reg_id).update({
            'status':         'confirmed',
            'payment_status': 'paid',
            'confirmed_at':   SERVER_TIMESTAMP,
            'qr_code_url':    qr_url,
            'qr_payload':     qr_payload,
        })
        db.collection('events').document(event_id).collection('ticket_types').document(
            reg['ticket_type_id']
        ).update({'quantity_sold': Increment(reg['quantity'])})
        reg_count += 1

    db.collection('events').document(event_id).update({
        'total_registrations': Increment(reg_count)
    })
    db.collection('orders').document(order_id).update({
        'status':         'confirmed',
        'payment_status': 'paid',
        'confirmed_at':   SERVER_TIMESTAMP,
    })
    if promo_id:
        db.collection('events').document(event_id).collection('promo_codes').document(promo_id).update({
            'current_uses': Increment(1)
        })


# ── NEW: cart checkout — supports multiple ticket types in one order ──

@registration_bp.route('/<event_id>/cart-checkout', methods=['POST'])
@login_required
def cart_checkout(event_id):
    event, tickets_by_id = get_event_and_all_tickets(event_id)

    if is_event_over(event):
        flash('This event has already ended and is no longer accepting registrations.', 'warning')
        return redirect(url_for('public.event_detail', event_id=event_id))

    cart_items = []
    for key, value in request.form.items():
        if key.startswith('qty_'):
            tt_id = key[len('qty_'):]
            try:
                qty = int(value)
            except ValueError:
                qty = 0
            if qty > 0:
                cart_items.append({'ticket_type_id': tt_id, 'quantity': qty})

    if not cart_items:
        flash('Please select at least one ticket before checking out.', 'warning')
        return redirect(url_for('public.event_detail', event_id=event_id))

    attendee_name = request.form.get('attendee_name', session.get('name', 'Attendee'))
    promo_code = request.form.get('promo_code', '').strip() or None

    order_id, err = _create_pending_order(event, tickets_by_id, cart_items, promo_code, attendee_name)
    if err:
        flash(err, 'danger')
        return redirect(url_for('public.event_detail', event_id=event_id))

    order_doc = db.collection('orders').document(order_id).get().to_dict()
    if order_doc.get('total_amount', 0) == 0:
        flash('Registration confirmed! These are free tickets.', 'success')
        return redirect(url_for('attendee.my_events'))

    return redirect(url_for('registration.order_summary', order_id=order_id))


# ── LEGACY: single ticket-type registration (still used by direct links) ──
@registration_bp.route('/<event_id>/<ticket_type_id>', methods=['GET', 'POST'])
@login_required
def register(event_id, ticket_type_id):
    event, ticket = get_event_and_ticket(event_id, ticket_type_id)

    if is_event_over(event):
        flash('This event has already ended and is no longer accepting registrations.', 'warning')
        return redirect(url_for('public.event_detail', event_id=event_id))

    available = ticket.get('quantity_total', 0) - ticket.get('quantity_sold', 0)
    if available <= 0:
        flash('Sorry, this ticket type is sold out.', 'warning')
        return redirect(url_for('public.event_detail', event_id=event_id))

    if request.method == 'POST':
        quantity = int(request.form.get('quantity', '1'))
        promo_code = request.form.get('promo_code', '').strip() or None

        if not promo_code and 'applied_promo' in session:
            if session['applied_promo'].get('event_id') == event_id:
                promo_code = session['applied_promo'].get('code')

        attendee_name = request.form.get('attendee_name', session.get('name', 'Attendee'))

        order_id, err = _create_pending_order(
            event,
            {ticket_type_id: ticket},
            [{'ticket_type_id': ticket_type_id, 'quantity': quantity}],
            promo_code,
            attendee_name,
        )
        if err:
            flash(err, 'danger')
            return redirect(request.url)

        session.pop('applied_promo', None)

        order_doc = db.collection('orders').document(order_id).get().to_dict()
        if order_doc.get('total_amount', 0) == 0:
            flash('Registration confirmed! This is a free ticket.', 'success')
            return redirect(url_for('attendee.my_events'))

        return redirect(url_for('registration.order_summary', order_id=order_id))

    return render_template('attendee/register.html',
                            event=event, ticket=ticket, available=available)


# ── PROMO CODE VALIDATION API — unchanged, works for cart subtotal too ──
@registration_bp.route('/validate-promo', methods=['POST'])
@login_required
def validate_promo():
    data     = request.json
    event_id = data.get('event_id', '')
    code     = data.get('code', '').strip()
    subtotal = float(data.get('subtotal', 0))

    if not code:
        return jsonify({'valid': False, 'message': 'Enter a promo code.'})

    discount, _, err = apply_promo(event_id, code, subtotal)
    if err:
        return jsonify({'valid': False, 'message': err})

    return jsonify({
        'valid':     True,
        'message':   f'Promo applied! You save ₹{discount}',
        'discount':  discount,
        'new_total': round(max(0, subtotal - discount), 2)
    })


# ── ORDER SUMMARY PAGE (replaces payment_summary for multi-item orders) ──
@registration_bp.route('/order-summary/<order_id>')
@login_required
def order_summary(order_id):
    user_uid = session.get('uid')

    order_doc = db.collection('orders').document(order_id).get()
    if not order_doc.exists:
        flash('Order not found', 'error')
        return redirect(url_for('public.index'))

    order_data = order_doc.to_dict()
    if order_data.get('attendee_uid') != user_uid:
        flash('Unauthorized access', 'error')
        return redirect(url_for('public.index'))

    event_id = order_data.get('event_id')
    event_doc = db.collection('events').document(event_id).get()
    event_data = event_doc.to_dict() if event_doc.exists else {}
    event_data['id'] = event_doc.id

    return render_template('attendee/order_summary.html',
                            order=order_data,
                            order_id=order_id,
                            event=event_data)


# ── LEGACY REDIRECT: old payment_summary(registration_id) links still work ──
@registration_bp.route('/payment-summary/<registration_id>')
@login_required
def payment_summary(registration_id):
    reg_doc = db.collection('registrations').document(registration_id).get()
    if not reg_doc.exists:
        flash('Registration not found', 'error')
        return redirect(url_for('public.index'))

    order_id = reg_doc.to_dict().get('order_id')
    if not order_id:
        # Registration created before this update has no order_id — cannot
        # be resumed through the new flow.
        flash('This order is out of date. Please start your registration again.', 'warning')
        return redirect(url_for('public.index'))

    return redirect(url_for('registration.order_summary', order_id=order_id))



DEFAULT_REFUND_POLICY = {
    'full_refund_days': 7,
    'partial_refund_days': 3,
    'partial_refund_percent': 50,
}


def calculate_refund_amount(event, registration):
    """Returns (refund_amount, refund_percent) based on the event's refund policy."""
    now = datetime.now(timezone.utc)
    start = event.get('start_datetime')
    if start and getattr(start, 'tzinfo', None) is None:
        start = start.replace(tzinfo=timezone.utc)

    if not start:
        return 0, 0

    days_before = (start - now).total_seconds() / 86400
    policy = event.get('refund_policy', DEFAULT_REFUND_POLICY)
    total_paid = registration.get('total_amount', 0)

    if days_before >= policy.get('full_refund_days', 7):
        return total_paid, 100
    elif days_before >= policy.get('partial_refund_days', 3):
        pct = policy.get('partial_refund_percent', 50)
        return round(total_paid * pct / 100, 2), pct
    else:
        return 0, 0


def _promote_waitlist(event_id, ticket_type_id):
    """Notifies the next waiting attendee that a spot opened up."""
    wl_docs = (
        db.collection('events').document(event_id).collection('waitlist')
        .where('ticket_type_id', '==', ticket_type_id)
        .where('status', '==', 'waiting')
        .order_by('joined_at').limit(1).stream()
    )
    for wl in wl_docs:
        wl_data = wl.to_dict()
        db.collection('events').document(event_id).collection('waitlist').document(wl.id).update({
            'status': 'notified'
        })
        create_notification(
            wl_data['attendee_uid'], 'A spot opened up!',
            "A ticket just became available for an event you were waitlisted for. Register soon before it's gone.",
            event_id=event_id, notif_type='waitlist_promoted'
        )
        try:
            send_ticket_email(
                to_email=wl_data.get('attendee_email'),
                subject="A ticket is now available!",
                html_content=(
                    "<p>Good news — a spot just opened up for an event you waitlisted for. "
                    f"<a href='{url_for('public.event_detail', event_id=event_id, _external=True)}'>"
                    "Register now</a> before it's gone.</p>"
                )
            )
        except Exception as e:
            print(f"[ERROR] Waitlist promotion email failed: {e}")


@registration_bp.route('/<reg_id>/cancel', methods=['GET', 'POST'])
@login_required
def cancel_registration(reg_id):
    reg_doc = db.collection('registrations').document(reg_id).get()
    if not reg_doc.exists:
        flash('Registration not found.', 'danger')
        return redirect(url_for('attendee.my_events'))

    reg = {**reg_doc.to_dict(), 'id': reg_doc.id}
    if reg.get('attendee_uid') != session.get('uid'):
        flash('Unauthorized.', 'danger')
        return redirect(url_for('attendee.my_events'))

    if reg.get('status') not in ('confirmed', 'checked_in'):
        flash('This ticket cannot be cancelled.', 'warning')
        return redirect(url_for('attendee.my_events'))

    event_doc = db.collection('events').document(reg['event_id']).get()
    event = {**event_doc.to_dict(), 'id': event_doc.id}

    refund_amount, refund_pct = calculate_refund_amount(event, reg)

    if request.method == 'POST':
        if refund_amount > 0 and reg.get('razorpay_payment_id'):
            refund_result = create_refund(
                reg['razorpay_payment_id'],
                amount_in_paise=int(refund_amount * 100)
            )
            if not refund_result.get('success'):
                flash(f"Refund could not be processed: {refund_result.get('error')}. "
                      f"Please contact support.", 'danger')
                return redirect(url_for('registration.cancel_registration', reg_id=reg_id))

        db.collection('registrations').document(reg_id).update({
            'status':         'cancelled',
            'cancelled_at':   SERVER_TIMESTAMP,
            'refund_amount':  refund_amount,
            'refund_percent': refund_pct,
            'refund_status':  'processing' if refund_amount > 0 else 'not_applicable',
        })

        db.collection('events').document(reg['event_id']).collection('ticket_types').document(
            reg['ticket_type_id']
        ).update({'quantity_sold': Increment(-reg.get('quantity', 1))})

        db.collection('events').document(reg['event_id']).update({
            'total_registrations': Increment(-1)
        })

        _promote_waitlist(reg['event_id'], reg['ticket_type_id'])

        flash(f'Registration cancelled. Refund of ₹{refund_amount} ({refund_pct}%) will be processed.', 'success')
        return redirect(url_for('attendee.my_events'))

    policy = event.get('refund_policy', DEFAULT_REFUND_POLICY)

    return render_template('attendee/cancel_confirm.html',
                            registration=reg, event=event,
                            refund_amount=refund_amount, refund_percent=refund_pct,
                            policy=policy)