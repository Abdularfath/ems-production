from app.firebase_config import db
from google.cloud.firestore import SERVER_TIMESTAMP


def create_notification(uid, title, message, event_id=None, notif_type='info'):
    """Writes an in-app notification for a user. Displayed later by a notification bell UI."""
    try:
        db.collection('notifications').document(uid).collection('items').add({
            'title':      title,
            'message':    message,
            'event_id':   event_id,
            'type':       notif_type,
            'read':       False,
            'created_at': SERVER_TIMESTAMP,
        })
    except Exception as e:
        print(f"[ERROR] Failed to write notification for {uid}: {e}")