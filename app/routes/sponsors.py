from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from app.firebase_config import db
from app.decorators import login_required, role_required
from app.routes.events import get_event_or_403
from google.cloud.firestore import SERVER_TIMESTAMP
import cloudinary.uploader

sponsors_bp = Blueprint('sponsors', __name__, url_prefix='/organizer/events/<event_id>/sponsors')

@sponsors_bp.route('/')
@login_required
@role_required('organizer')
def list_sponsors(event_id):
    doc, event_data = get_event_or_403(event_id)
    if not doc:
        return redirect(url_for('events.list_events'))

    sponsors_docs = db.collection('events').document(event_id).collection('sponsors').stream()
    sponsors = [{**s.to_dict(), 'id': s.id} for s in sponsors_docs]

    total_amount = sum(s.get('amount', 0) for s in sponsors)

    return render_template(
        'organizer/sponsors/list.html',
        event=event_data,
        event_id=event_id,
        sponsors=sponsors,
        total_amount=total_amount
    )

@sponsors_bp.route('/add', methods=['GET', 'POST'])
@login_required
@role_required('organizer')
def add_sponsor(event_id):
    doc, event_data = get_event_or_403(event_id)
    if not doc:
        return redirect(url_for('events.list_events'))

    if request.method == 'POST':
        company_name   = request.form.get('company_name', '').strip()
        contact_person = request.form.get('contact_person', '').strip()
        email          = request.form.get('email', '').strip()
        phone          = request.form.get('phone', '').strip()
        tier           = request.form.get('tier', 'Bronze')
        amount         = float(request.form.get('amount', 0))
        logo_file      = request.files.get('logo')

        logo_url = ''
        if logo_file and logo_file.filename:
            upload_result = cloudinary.uploader.upload(
                logo_file,
                folder='ems_sponsors',
                width=300,
                height=300,
                crop='fill',
                gravity='center'
            )
            logo_url = upload_result.get('secure_url', '')
            sponsor_data = {
                'company_name':     company_name,
                'contact_person':   contact_person,
                'email':            email,
                'phone':            phone,
                'tier':             tier,
                'amount':           amount,
                'logo_url':         logo_url,
                'organizer_uid':    session.get('uid'),
                'created_at':       SERVER_TIMESTAMP,
                'is_exhibitor':     False,
                'booth_number':     '',
                'booth_description': '',
                'team_members':     ''
                }

        db.collection('events').document(event_id).collection('sponsors').add(sponsor_data)
        flash(f'Sponsor {company_name} added successfully!', 'success')
        return redirect(url_for('sponsors.list_sponsors', event_id=event_id))

    return render_template('organizer/sponsors/form.html',
                           event=event_data,
                           event_id=event_id,
                           sponsor=None)

@sponsors_bp.route('/<sponsor_id>/edit', methods=['GET', 'POST'])
@login_required
@role_required('organizer')
def edit_sponsor(event_id, sponsor_id):
    doc, event_data = get_event_or_403(event_id)
    if not doc:
        return redirect(url_for('events.list_events'))

    sponsor_ref = db.collection('events').document(event_id).collection('sponsors').document(sponsor_id)
    sponsor_doc = sponsor_ref.get()
    if not sponsor_doc.exists:
        flash('Sponsor not found.', 'danger')
        return redirect(url_for('sponsors.list_sponsors', event_id=event_id))

    sponsor_data = {**sponsor_doc.to_dict(), 'id': sponsor_doc.id}

    if request.method == 'POST':
        company_name   = request.form.get('company_name', '').strip()
        contact_person = request.form.get('contact_person', '').strip()
        email          = request.form.get('email', '').strip()
        phone          = request.form.get('phone', '').strip()
        tier           = request.form.get('tier', 'Bronze')
        amount         = float(request.form.get('amount', 0))
        logo_file      = request.files.get('logo')

        booth_number      = request.form.get('booth_number', '').strip()
        booth_description = request.form.get('booth_description', '').strip()
        team_members      = request.form.get('team_members', '').strip()
        
        update_data = {
            'company_name':     company_name,
            'contact_person':   contact_person,
            'email':            email,
            'phone':            phone,
            'tier':             tier,
            'amount':           amount,
            'booth_number':     booth_number,
            'booth_description': booth_description,
            'team_members':     team_members
            }

        if logo_file and logo_file.filename:
            upload_result = cloudinary.uploader.upload(
                logo_file,
                folder='ems_sponsors',
                width=300,
                height=300,
                crop='fill',
                gravity='center'
            )
            update_data['logo_url'] = upload_result.get('secure_url', '')

        sponsor_ref.update(update_data)
        flash(f'Sponsor {company_name} updated successfully!', 'success')
        return redirect(url_for('sponsors.list_sponsors', event_id=event_id))

    return render_template('organizer/sponsors/form.html',
                           event=event_data,
                           event_id=event_id,
                           sponsor=sponsor_data)

@sponsors_bp.route('/<sponsor_id>/delete', methods=['POST'])
@login_required
@role_required('organizer')
def delete_sponsor(event_id, sponsor_id):
    db.collection('events').document(event_id).collection('sponsors').document(sponsor_id).delete()
    flash('Sponsor removed.', 'info')
    return redirect(url_for('sponsors.list_sponsors', event_id=event_id))

@sponsors_bp.route('/<sponsor_id>/toggle_exhibitor', methods=['POST'])
@login_required
@role_required('organizer')
def toggle_exhibitor(event_id, sponsor_id):
    """Toggle sponsor's exhibitor status."""
    sponsor_ref = db.collection('events').document(event_id)\
                    .collection('sponsors').document(sponsor_id)
    sponsor_doc = sponsor_ref.get()

    if not sponsor_doc.exists:
        flash('Sponsor not found.', 'danger')
        return redirect(url_for('sponsors.list_sponsors', event_id=event_id))

    current = sponsor_doc.to_dict().get('is_exhibitor', False)
    sponsor_ref.update({'is_exhibitor': not current})

    if not current:
        flash('Sponsor marked as Exhibitor!', 'success')
    else:
        flash('Exhibitor status removed.', 'info')

    return redirect(url_for('sponsors.list_sponsors', event_id=event_id))


@sponsors_bp.route('/exhibitors')
@login_required
@role_required('organizer')
def list_exhibitors(event_id):
    """Show all exhibitors for this event."""
    doc, event_data = get_event_or_403(event_id)
    if not doc:
        return redirect(url_for('events.list_events'))

    sponsors_docs = db.collection('events').document(event_id)\
                      .collection('sponsors')\
                      .stream()

    exhibitors = []
    for s in sponsors_docs:
        data = {**s.to_dict(), 'id': s.id}
        if data.get('is_exhibitor', False):
            exhibitors.append(data)

    return render_template(
        'organizer/sponsors/exhibitors.html',
        event=event_data,
        event_id=event_id,
        exhibitors=exhibitors
    )