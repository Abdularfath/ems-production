import os
import json
import firebase_admin
from firebase_admin import credentials, firestore, auth, storage

# ── Module-level singletons ─────────────────────────────────────────────
_app  = None
db    = None
bucket= None

def init_firebase():
    global _app, db, bucket

    if firebase_admin._apps:
        db     = firestore.client()
        bucket = storage.bucket()
        return

    # Try environment variable first (Render), fall back to file (local)
    firebase_credentials_json = os.getenv('FIREBASE_CREDENTIALS_JSON')
    
    if firebase_credentials_json:
        # Render: load from environment variable
        cred_dict = json.loads(firebase_credentials_json)
        cred = credentials.Certificate(cred_dict)
    else:
        # Local: load from file
        key_path = os.getenv('FIREBASE_CREDENTIALS_PATH', 'secrets/firebase-key.json')
        cred = credentials.Certificate(key_path)

    _app = firebase_admin.initialize_app(cred, {
        'projectId':     os.getenv('FIREBASE_PROJECT_ID'),
        'storageBucket': os.getenv('FIREBASE_STORAGE_BUCKET')
    })

    db     = firestore.client()
    bucket = storage.bucket()
    print('[Firebase] Initialised successfully',
          f'Project: {os.getenv("FIREBASE_PROJECT_ID")}')