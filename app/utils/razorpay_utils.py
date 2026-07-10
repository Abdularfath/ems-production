"""
Razorpay utility functions for payment processing.
Handles order creation, payment verification, and signature validation.
"""

import razorpay
import os
import hmac
import hashlib
from dotenv import load_dotenv

load_dotenv()

# Initialize Razorpay client
client = razorpay.Client(
    auth=(
        os.getenv('RAZORPAY_KEY_ID'),
        os.getenv('RAZORPAY_KEY_SECRET')
    )
)


def create_razorpay_order(amount_in_paise, registration_id, attendee_email):
    """
    Create a Razorpay order.
    
    Args:
        amount_in_paise: Amount in paise (₹100 = 10000 paise)
        registration_id: Registration document ID
        attendee_email: Attendee email for receipt
    
    Returns:
        dict: Order details including order_id, amount, currency
    """
    try:
        order_data = {
            'amount': amount_in_paise,  # Amount in paise
            'currency': 'INR',
            'receipt': registration_id,  # Use registration ID as receipt
            'notes': {
                'registration_id': registration_id,
                'email': attendee_email
            }
        }
        
        order = client.order.create(data=order_data)
        return {
            'success': True,
            'order_id': order['id'],
            'amount': order['amount'],
            'currency': order['currency'],
            'receipt': order['receipt']
        }
    except Exception as e:
        print(f"Razorpay order creation error: {str(e)}")
        return {
            'success': False,
            'error': str(e)
        }


def verify_payment_signature(razorpay_order_id, razorpay_payment_id, razorpay_signature):
    """
    Verify the Razorpay payment signature.
    This ensures the payment was actually processed by Razorpay.
    
    Args:
        razorpay_order_id: Order ID from Razorpay
        razorpay_payment_id: Payment ID from Razorpay
        razorpay_signature: Signature sent by Razorpay (to verify)
    
    Returns:
        bool: True if signature is valid, False otherwise
    """
    try:
        # Signature verification formula:
        # Expected signature = HMAC-SHA256(order_id|payment_id, key_secret)
        
        body = f"{razorpay_order_id}|{razorpay_payment_id}"
        key_secret = os.getenv('RAZORPAY_KEY_SECRET')
        
        # Create expected signature
        expected_signature = hmac.new(
            key_secret.encode(),
            body.encode(),
            hashlib.sha256
        ).hexdigest()
        
        # Constant-time comparison (prevents timing attacks)
        return hmac.compare_digest(expected_signature, razorpay_signature)
    
    except Exception as e:
        print(f"Signature verification error: {str(e)}")
        return False


def get_payment_details(payment_id):
    """
    Fetch payment details from Razorpay.
    Used to confirm amount, status, etc.
    
    Args:
        payment_id: Razorpay payment ID
    
    Returns:
        dict: Payment details
    """
    try:
        payment = client.payment.fetch(payment_id)
        return {
            'success': True,
            'payment_id': payment['id'],
            'amount': payment['amount'],
            'currency': payment['currency'],
            'status': payment['status'],  # 'captured', 'failed', etc.
            'email': payment.get('email', ''),
            'contact': payment.get('contact', '')
        }
    except Exception as e:
        print(f"Payment fetch error: {str(e)}")
        return {
            'success': False,
            'error': str(e)
        }
    
def create_refund(payment_id, amount_in_paise=None, notes=None):
    """
    Issue a refund for a captured Razorpay payment.
    Pass amount_in_paise=None for a FULL refund; pass a value for a partial refund.
    """
    try:
        data = {}
        if amount_in_paise is not None:
            data['amount'] = amount_in_paise
        if notes:
            data['notes'] = notes

        refund = client.payment.refund(payment_id, data)
        return {
            'success':   True,
            'refund_id': refund['id'],
            'amount':    refund['amount'],
            'status':    refund['status'],
        }
    except Exception as e:
        print(f"Razorpay refund error: {str(e)}")
        return {
            'success': False,
            'error': str(e)
        }
        