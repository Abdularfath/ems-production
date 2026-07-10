"""
Payment routes for Razorpay integration.
Handles order creation, payment capture, and success/failure flows.

Now order-based: one Razorpay payment can cover multiple ticket types
(multiple `registrations` docs) grouped under a single `orders` doc.
Old links that pass a registration_id instead of an order_id still work —
see resolve_order().
"""

from flask import Blueprint, render_template, request, jsonify, redirect, url_for, flash, session
from app.firebase_config import db, auth
from app.decorators import login_required
from app.utils.razorpay_utils import (
    create_razorpay_order,
    verify_payment_signature,
    get_payment_details
)
import os
from dotenv import load_dotenv
from google.cloud.firestore import Increment
from app.utils.qr_utils import generate_and_upload_qr
from app.utils.email_utils import send_ticket_email

load_dotenv()

payment_bp = Blueprint('payment', __name__, url_prefix='/attendee/payment')


# ── HELPER: resolve an id to an order, accepting legacy registration ids ──
def resolve_order(id_value, user_uid):
    """
    Returns (True, order_id, order_data) or (False, None, error_message).

    Accepts either:
      - an `orders` document id (new flow), or
      - a `registrations` document id (legacy links) — resolved via its
        order_id field.
    """
    order_doc = db.collection('orders').document(id_value).get()

    if not order_doc.exists:
        # Maybe this is a legacy registration id — look up its order_id.
        reg_doc = db.collection('registrations').document(id_value).get()
        if not reg_doc.exists:
            return False, None, "Order not found"
        reg_order_id = reg_doc.to_dict().get('order_id')
        if not reg_order_id:
            return False, None, "This order predates the current checkout flow and can't be resumed."
        order_doc = db.collection('orders').document(reg_order_id).get()
        if not order_doc.exists:
            return False, None, "Order not found"

    order_data = order_doc.to_dict()
    if order_data.get('attendee_uid') != user_uid:
        return False, None, "Unauthorized"

    return True, order_doc.id, order_data


# ── CREATE ORDER: Called when user clicks "Pay Now" ──────────────────
@payment_bp.route('/<order_id>/create-order', methods=['POST'])
@login_required
def create_order(order_id):
    """
    Create a Razorpay order for this EMS order (may contain several
    ticket-type line items). Called via AJAX from order_summary.html
    """
    user_uid = session.get('uid')

    ok, resolved_order_id, result = resolve_order(order_id, user_uid)
    if not ok:
        return jsonify({'success': False, 'error': result}), 403

    order_data = result
    attendee_email = order_data.get('attendee_email', '')
    total_amount = order_data.get('total_amount', 0)

    amount_in_paise = int(total_amount * 100)

    order_result = create_razorpay_order(
        amount_in_paise,
        resolved_order_id,
        attendee_email
    )

    if not order_result['success']:
        return jsonify({'success': False, 'error': order_result['error']}), 500

    return jsonify({
        'success':   True,
        'order_id':  order_result['order_id'],
        'amount':    order_result['amount'],
        'currency':  order_result['currency'],
        'key_id':    os.getenv('RAZORPAY_KEY_ID'),
        'ems_order_id': resolved_order_id,
        'email':     attendee_email
    })


# ── CAPTURE PAYMENT: Called after user approves in Razorpay checkout ──
@payment_bp.route('/<order_id>/capture', methods=['POST'])
@login_required
def capture_payment(order_id):
    """
    Capture the payment after user completes Razorpay checkout, then
    confirm every registration line item that belongs to this order.
    """
    user_uid = session.get('uid')
    print(f"[DEBUG CAPTURE] Starting capture for user {user_uid}")

    ok, resolved_order_id, result = resolve_order(order_id, user_uid)
    if not ok:
        return jsonify({'success': False, 'error': result}), 403

    order_data = result

    data = request.get_json()
    razorpay_order_id   = data.get('razorpay_order_id')
    razorpay_payment_id = data.get('razorpay_payment_id')
    razorpay_signature  = data.get('razorpay_signature')

    # Step 1: Verify Razorpay signature
    sig_valid = verify_payment_signature(razorpay_order_id, razorpay_payment_id, razorpay_signature)
    if not sig_valid:
        return jsonify({'success': False, 'error': 'Payment verification failed'}), 400

    # Step 2: Fetch payment details
    payment_details = get_payment_details(razorpay_payment_id)
    if not payment_details['success']:
        return jsonify({'success': False, 'error': 'Could not fetch payment details'}), 500

    # Step 3: Verify amount against the ORDER total (sum of all line items)
    captured_amount = payment_details['amount'] / 100
    expected_amount = order_data.get('total_amount', 0)

    if abs(captured_amount - expected_amount) > 0.01:
        return jsonify({'success': False, 'error': 'Amount mismatch'}), 400

    event_id = order_data.get('event_id')

    # Step 4: Fetch every registration under this order
    reg_docs = list(db.collection('registrations').where('order_id', '==', resolved_order_id).stream())
    if not reg_docs:
        return jsonify({'success': False, 'error': 'No tickets found for this order'}), 500

    confirmed_registrations = []

    for reg_doc in reg_docs:
        reg_id = reg_doc.id
        reg = reg_doc.to_dict()
        ticket_type_id = reg.get('ticket_type_id')
        quantity = int(reg.get('quantity', 1))

        # Best-effort re-check for oversell right before we confirm. Stock isn't
        # reserved at cart time (matches the original single-ticket flow's
        # behaviour), so in the rare case of a race this logs a warning rather
        # than blocking a payment that has already been captured. A production
        # system would reserve inventory with a short-TTL hold at cart time.
        tt_doc = db.collection('events').document(event_id).collection('ticket_types').document(ticket_type_id).get()
        tt = tt_doc.to_dict() if tt_doc.exists else {}
        if tt.get('quantity_sold', 0) + quantity > tt.get('quantity_total', 0):
            print(f"[WARN CAPTURE] Oversold ticket_type {ticket_type_id} on order {resolved_order_id}")

        qr_url, qr_payload = generate_and_upload_qr(reg_id, event_id)

        db.collection('registrations').document(reg_id).update({
            'status':              'confirmed',
            'payment_status':      'paid',
            'razorpay_payment_id': razorpay_payment_id,
            'razorpay_order_id':   razorpay_order_id,
            'qr_code_url':         qr_url,
            'qr_payload':          qr_payload,
        })

        db.collection('events').document(event_id).collection('ticket_types').document(ticket_type_id).update({
            'quantity_sold': Increment(quantity)
        })

        reg['id'] = reg_id
        reg['qr_code_url'] = qr_url
        confirmed_registrations.append(reg)

    # Step 5: Update order + event-level counters
    try:
        db.collection('orders').document(resolved_order_id).update({
            'status':              'confirmed',
            'payment_status':      'paid',
            'razorpay_payment_id': razorpay_payment_id,
            'razorpay_order_id':   razorpay_order_id,
        })
        db.collection('events').document(event_id).update({
            'total_registrations': Increment(len(confirmed_registrations)),
            'total_revenue':       Increment(captured_amount)
        })
        promo_id = order_data.get('promo_id')
        if promo_id:
            db.collection('events').document(event_id).collection('promo_codes').document(promo_id).update({
                'current_uses': Increment(1)
            })
    except Exception as e:
        print(f"[ERROR CAPTURE] Counter update failed: {str(e)}")

    # Step 6: Send ONE confirmation email listing every ticket in the order
    print(f"[DEBUG CAPTURE] Sending confirmation email...")
    try:
        event_doc = db.collection('events').document(event_id).get()
        event_data = event_doc.to_dict() if event_doc.exists else {}

        email_html = render_template(
            'emails/confirmation.html',
            order=order_data,
            order_id=resolved_order_id,
            registrations=confirmed_registrations,
            event=event_data,
        )

        email_sent = send_ticket_email(
            to_email=order_data.get('attendee_email'),
            subject=f"Your Tickets for {event_data.get('name', 'Event')}",
            html_content=email_html
        )
        print(f"[DEBUG CAPTURE] Email sent status: {email_sent}")
    except Exception as e:
        print(f"[ERROR CAPTURE] Failed to send confirmation email: {e}")

    print(f"[DEBUG CAPTURE] Capture complete ✓")
    return jsonify({
        'success':  True,
        'order_id': resolved_order_id,
        'message':  'Payment captured successfully'
    })


# ── PAYMENT SUCCESS PAGE ───────────────────────────────────────────
@payment_bp.route('/success/<order_id>')
@login_required
def payment_success(order_id):
    user_uid = session.get('uid')

    ok, resolved_order_id, result = resolve_order(order_id, user_uid)
    if not ok:
        flash(result, 'error')
        return redirect(url_for('public.index'))

    order_data = result

    event_id = order_data.get('event_id')
    event_doc = db.collection('events').document(event_id).get()
    event_data = event_doc.to_dict() if event_doc.exists else {}
    event_data['id'] = event_doc.id

    reg_docs = db.collection('registrations').where('order_id', '==', resolved_order_id).stream()
    registrations = []
    for d in reg_docs:
        r = d.to_dict()
        r['id'] = d.id
        registrations.append(r)

    return render_template('attendee/order_success.html',
        order=order_data,
        order_id=resolved_order_id,
        registrations=registrations,
        event=event_data,
        order_number=order_data.get('order_number', 'N/A')
    )


# ── PAYMENT FAILURE HANDLER ────────────────────────────────────────
@payment_bp.route('/failed/<order_id>')
@login_required
def payment_failed(order_id):
    user_uid = session.get('uid')

    ok, resolved_order_id, result = resolve_order(order_id, user_uid)
    if not ok:
        flash(result, 'error')
        return redirect(url_for('public.index'))

    try:
        db.collection('orders').document(resolved_order_id).update({'payment_status': 'failed'})
        reg_docs = db.collection('registrations').where('order_id', '==', resolved_order_id).stream()
        for d in reg_docs:
            db.collection('registrations').document(d.id).update({'payment_status': 'failed'})
    except Exception:
        pass

    flash('Payment failed. Please try again.', 'error')
    return redirect(url_for('registration.order_summary', order_id=resolved_order_id))