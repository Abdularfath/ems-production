import os
from flask import Flask, jsonify, render_template, session, redirect, url_for, flash
from dotenv import load_dotenv
from flask_wtf.csrf import CSRFProtect
from app.firebase_config import init_firebase

 
load_dotenv()
 
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-change-this')
csrf = CSRFProtect(app)
# Allow CSRF token in request headers (needed for fetch() AJAX calls)
app.config['WTF_CSRF_CHECK_DEFAULT'] = False
  # scanner JS sends token in header, not form

 
# Initialise Firebase
init_firebase()
 
# ── Register blueprints ─────────────────────────────────────────────
from app.routes.test import test_bp
from app.routes.auth import auth_bp
from app.routes.public import public_bp
from app.routes.organizer import organizer_bp
from app.routes.attendee import attendee_bp
from app.routes.admin import admin_bp
from app.routes.venues import venues_bp
from app.routes.events import events_bp
from app.routes.tickets import tickets_bp
from app.routes.checkin import checkin_bp
from app.routes.registration import registration_bp
from app.routes.payment import payment_bp
from app.routes.sessions import sessions_bp 
from app.routes.speakers import speakers_bp
from app.routes.promos import promos_bp
from app.routes.sponsors import sponsors_bp
from app.routes.deliverables import deliverables_bp
from app.routes.sponsor_portal import sponsor_portal_bp

 
app.register_blueprint(test_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(public_bp)
app.register_blueprint(organizer_bp)
app.register_blueprint(venues_bp)
app.register_blueprint(attendee_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(events_bp)
app.register_blueprint(tickets_bp)
app.register_blueprint(checkin_bp)
app.register_blueprint(registration_bp)
app.register_blueprint(payment_bp)
app.register_blueprint(sessions_bp)
app.register_blueprint(speakers_bp)
app.register_blueprint(promos_bp)
app.register_blueprint(sponsors_bp)
app.register_blueprint(deliverables_bp)
app.register_blueprint(sponsor_portal_bp)



csrf.exempt(checkin_bp)
 # scanner JS sends token in header, not form
csrf.exempt(registration_bp)  # validate-promo uses fetch() not form POST
# csrf.exempt(payment_bp)  # PayPal JS SDK sends JSON, not form POST


# @app.context_processor
# def inject_paypal_client_id():
#     return dict(paypal_client_id=os.getenv('PAYPAL_CLIENT_ID',''))


# ... your blueprints like app.register_blueprint(admin_bp) ...

# Paste them right here!
@app.errorhandler(404)
def page_not_found(e):
    return render_template('errors/404.html'), 404

@app.errorhandler(500)
def internal_server_error(e):
    return render_template('errors/500.html'), 500

@app.errorhandler(403)
def forbidden(e):
    return render_template('errors/403.html'), 403

# ...
# Do NOT put them below this line:
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)


    # ==========================================
    # CUSTOM ERROR HANDLERS (Day 4 Polish)
    # ==========================================
    @app.errorhandler(403)
    def forbidden(e):
        """Custom 403 Forbidden page."""
        return render_template('errors/403.html'), 403