import os, requests
from flask import (Blueprint, render_template, redirect,
                        url_for, session, flash, request)
from firebase_admin import auth as fb_auth
from app.firebase_config import db
from datetime import datetime, timezone
 
auth_bp = Blueprint('auth', __name__, url_prefix='/auth')
 
# Firebase Auth REST API base URL
FIREBASE_AUTH_URL = 'https://identitytoolkit.googleapis.com/v1/accounts:'
 
 
def get_api_key():
    return os.getenv('FIREBASE_WEB_API_KEY')
 
 
# ── SIGNUP ─────────────────────────────────────────────────────────
@auth_bp.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        role     = request.form.get('role', 'attendee')
 
        # Basic validation
        if not email or not password:
            flash('Email and password are required.', 'danger')
            return redirect(url_for('auth.signup'))
 
        if len(password) < 6:
            flash('Password must be at least 6 characters.', 'danger')
            return redirect(url_for('auth.signup'))
 
        if role not in ['attendee', 'organizer', 'sponsor']:
            role = 'attendee'
 
        try:
            # Create user in Firebase Auth
            user = fb_auth.create_user(email=email, password=password)
 
            # Save profile to Firestore /users/{uid}
            db.collection('users').document(user.uid).set({
                'uid':              user.uid,
                'name':             name,
                'email':            email,
                'role':             role,
                'is_active':        True,
                'networking_opt_in':False,
                'created_at':       datetime.now(timezone.utc),
            })
 
            # Set session so user is immediately logged in
            session['uid']   = user.uid
            session['email'] = email
            session['role']  = role
            session['name']  = name
 
            flash(f'Welcome to EMS! Your account has been created.', 'success')
 
            # Redirect based on role
            if role == 'organizer':
                return redirect(url_for('organizer.dashboard'))
            return redirect(url_for('attendee.my_events'))
 
        except fb_auth.EmailAlreadyExistsError:
            flash('An account with this email already exists. Please login.', 'warning')
            return redirect(url_for('auth.login'))
        except Exception as e:
            flash(f'Signup failed: {str(e)}', 'danger')
            return redirect(url_for('auth.signup'))
 
    return render_template('auth/signup.html')
 
 
# ── LOGIN ──────────────────────────────────────────────────────────
@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('uid'):
        return redirect(url_for('public.index'))
 
    if request.method == 'POST':
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
 
        try:
            # Call Firebase Auth REST API to verify email + password
            resp = requests.post(
                f'{FIREBASE_AUTH_URL}signInWithPassword?key={get_api_key()}',
                json={'email': email, 'password': password, 'returnSecureToken': True},
                timeout=10
            )
            data = resp.json()
 
            if 'error' in data:
                err = data['error']['message']
                if err in ('EMAIL_NOT_FOUND', 'INVALID_PASSWORD', 'INVALID_LOGIN_CREDENTIALS'):
                    flash('Invalid email or password.', 'danger')
                else:
                    flash(f'Login failed: {err}', 'danger')
                return redirect(url_for('auth.login'))
 
            # Get user profile from Firestore for role
            uid = data['localId']
            user_doc = db.collection('users').document(uid).get()
            role = 'attendee'
            name = 'User'
            if user_doc.exists:
                role = user_doc.to_dict().get('role', 'attendee')
                name = user_doc.to_dict().get('name', 'User')
            
            # Validate selected role matches actual role in Firestore
            selected_role = request.form.get('role_hint', '').strip()
            if selected_role and selected_role != role:
                 flash(f'Incorrect role selected. Please select "{role.capitalize()}" to login.', 'danger')
                 return redirect(url_for('auth.login'))
            # Set session
            session['uid']   = uid
            session['email'] = email
            session['role']  = role
            session['name']  = name
            flash(f'Welcome back!', 'success')
 
            if role == 'organizer':
                return redirect(url_for('organizer.dashboard'))
            if role == 'admin':
                return redirect(url_for('admin.dashboard'))
            if role == 'sponsor':
                return redirect(url_for('sponsor_portal.dashboard'))
            if role == 'super_admin':
                return redirect(url_for('super_admin.dashboard'))
            return redirect(url_for('attendee.my_events'))
 
        except Exception as e:
            flash(f'Login error: {str(e)}', 'danger')
            return redirect(url_for('auth.login'))
 
    return render_template('auth/login.html')
 
 
# ── LOGOUT ─────────────────────────────────────────────────────────
@auth_bp.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('public.index'))
 
 
# ── FORGOT PASSWORD ─────────────────────────────────────────────────
@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    sent = False
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        try:
            requests.post(
                f'{FIREBASE_AUTH_URL}sendOobCode?key={get_api_key()}',
                json={'requestType': 'PASSWORD_RESET', 'email': email},
                timeout=10
            )
            # Always show success (do not reveal if email exists)
            sent = True
        except Exception:
            sent = True  # Still show success to prevent email enumeration
    return render_template('auth/forgot_password.html', sent=sent)
